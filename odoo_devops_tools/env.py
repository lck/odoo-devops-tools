#!/usr/bin/env python3
"""
odt-env

Provision an Odoo workspace from a single project file
"""

from __future__ import annotations

import argparse
import configparser
import io
import json
import logging
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse, urlunparse
from urllib.request import Request, urlopen

from . import __version__

_logger = logging.getLogger("odt-env")

_GIT_INI_PREFIX = "git::"
_URL_INI_TIMEOUT_SECONDS = 30
_URL_INI_MAX_BYTES = 1024 * 1024

_DEFAULT_PROJECT_INI_NAME = "odoo-project.ini"

_DEFAULT_PROJECT_INI_TEMPLATE = r"""
[virtualenv]
managed_python = true
python_version =
build_constraints =
requirements =
requirements_ignore =

[odoo]
version = 19.0
repo = https://github.com/odoo/odoo.git
branch = ${odoo:version}
commit =
shallow = true

[docker]
target_image = local/odoo:${odoo:version}
base_image = odoo:${odoo:version}
addons_mode = deploy
compose_project_name =
db_service = db
odoo_service = odoo

[config]
"""

_IMPLICIT_LOCAL_CONFIG_DEFAULTS = {
    "db_host": "127.0.0.1",
    "db_name": "odoo",
    "db_user": "odoo",
    "db_password": "odoo",
}

_DEFAULT_REQUIREMENTS = [
    "pip",
    "setuptools",
    "wheel",
    "click-odoo-contrib",
]

_DEFAULT_DOCKER_REQUIREMENTS = [
    "click-odoo-contrib",
]

_DEFAULT_DOCKER_DB_SERVICE = "db"
_DEFAULT_DOCKER_ODOO_SERVICE = "odoo"
_DEFAULT_DOCKER_COMPOSE_PROJECT_NAME = None
_DEFAULT_DOCKER_HTTP_CONTAINER_PORT = 8069
_DEFAULT_DOCKER_GEVENT_CONTAINER_PORT = 8072
_DEFAULT_DOCKER_ADDONS_MODE = "deploy"
_DOCKER_ADDONS_MODE_DEPLOY = "deploy"
_DOCKER_ADDONS_MODE_DEV = "dev"
_DOCKER_ADDONS_MODES = {_DOCKER_ADDONS_MODE_DEPLOY, _DOCKER_ADDONS_MODE_DEV}
_DOCKER_ADDONS_CONTAINER_ROOT = PurePosixPath("/mnt/extra-addons")
_DOCKER_HOST_PORT_CONFIG_KEYS = {"http_port", "gevent_port", "longpolling_port"}

_DEFAULT_ODOO_REPO = "https://github.com/odoo/odoo.git"

_ODOO_VENV_SETTINGS = {
    # odoo_ver: (min_py_ver, max_py_ver, build_constraints, requirements)
    12: ("3.5", "3.8", ["setuptools<58"], ["setuptools<58"]),
    13: ("3.6", "3.8", ["setuptools<58"], ["setuptools<58"]),
    14: ("3.6", "3.10", ["setuptools<82"], ["setuptools<82"]),
    15: ("3.7", "3.10", ["setuptools<82"], ["setuptools<82"]),
    16: ("3.7", "3.10", ["setuptools<82"], ["setuptools<82"]),
    17: ("3.10", "3.12", ["setuptools<82"], ["setuptools<82"]),
    18: ("3.10", "3.12", ["setuptools<82"], ["setuptools<82"]),
    19: ("3.10", "3.12", [], []),
}

_SENSITIVE_KEYS = ("password", "passwd", "secret", "token", "api_key", "apikey", "private_key")


# -----------------------------
# Data models
# -----------------------------

@dataclass(frozen=True)
class OdooSpec:
    version: str
    repo: str
    branch: str
    commit: Optional[str] = None
    path: Optional[str] = None
    shallow: bool = True

    @property
    def is_local(self) -> bool:
        return bool((self.path or "").strip())


@dataclass(frozen=True)
class AddonSpec:
    repo: Optional[str] = None
    branch: Optional[str] = None
    commit: Optional[str] = None
    path: Optional[str] = None
    shallow: bool = True

    @property
    def is_local(self) -> bool:
        return bool((self.path or "").strip())


@dataclass(frozen=True)
class GitIniSource:
    repo: str
    path: PurePosixPath
    ref: Optional[str] = None


@dataclass(frozen=True)
class UrlIniSource:
    url: str
    original_url: str


@dataclass(frozen=True)
class VirtualenvConfig:
    python_version: str
    build_constraints: list[str]
    requirements: list[str]
    requirements_ignore: list[str]
    explicit_requirements: list[str]
    explicit_requirements_ignore: list[str]
    managed_python: bool = True


@dataclass(frozen=True)
class DockerConfig:
    target_image: Optional[str] = None
    base_image: Optional[str] = None
    addons_mode: str = _DEFAULT_DOCKER_ADDONS_MODE
    compose_project_name: Optional[str] = _DEFAULT_DOCKER_COMPOSE_PROJECT_NAME
    db_service: str = _DEFAULT_DOCKER_DB_SERVICE
    odoo_service: str = _DEFAULT_DOCKER_ODOO_SERVICE


@dataclass(frozen=True)
class ProjectConfig:
    virtualenv: VirtualenvConfig
    odoo: OdooSpec
    addons: Dict[str, AddonSpec]
    config: Dict[str, Any]
    docker: DockerConfig


@dataclass(frozen=True)
class Layout:
    root: Path
    odoo_dir: Path
    addons_root: Path
    backups_dir: Path
    configs_dir: Path
    conf_path: Path
    data_dir: Path
    scripts_dir: Path
    wheelhouse_dir: Path
    docker_dir: Path

    @staticmethod
    def from_root(root: Path) -> "Layout":
        odoo_dir = root / "odoo"
        addons_root = root / "odoo-addons"
        backups_dir = root / "odoo-backups"
        configs_dir = root / "odoo-configs"
        conf_path = configs_dir / "odoo-server.conf"
        data_dir = root / "odoo-data"
        scripts_dir = root / "odoo-scripts"
        wheelhouse_dir = root / "wheelhouse"
        docker_dir = root / "odoo-docker"
        return Layout(
            root=root,
            odoo_dir=odoo_dir,
            addons_root=addons_root,
            backups_dir=backups_dir,
            configs_dir=configs_dir,
            conf_path=conf_path,
            data_dir=data_dir,
            scripts_dir=scripts_dir,
            wheelhouse_dir=wheelhouse_dir,
            docker_dir=docker_dir,
        )

    def script(self, name: str, ext: str) -> Path:
        return self.scripts_dir / f"{name}.{ext}"


# -----------------------------
# Helpers: validation & parsing
# -----------------------------

def _format_cmd(cmd: list[str]) -> str:
    return shlex.join(str(part) for part in cmd)


def _handle_process_output(p: subprocess.CompletedProcess[str], err_msg: str) -> None:
    if p.stdout:
        _logger.info(p.stdout)
    if p.stderr:
        _logger.warning(p.stderr)
    if p.returncode != 0:
        raise Exception(err_msg)


def _run_checked(
        cmd: list[str],
        cwd: Optional[Path] = None,
        err_msg: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
    )
    if err_msg is None:
        failure_message = "Command failed"
    else:
        failure_message = err_msg
    _handle_process_output(
        p,
        f"{failure_message}\n"
        f"Command: {_format_cmd(cmd)}\n"
        f"{p.stdout}\n{p.stderr}",
    )
    return p


def _ensure_command(name: str) -> None:
    if shutil.which(name) is None:
        raise Exception(f"Required command not found in PATH: {name}")


def _chmod_private(path: Path) -> None:
    if sys.platform.startswith("win"):
        return
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _make_executable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _write_text_file(
        path: Path,
        content: str,
        *,
        private: bool = False,
        executable: bool = False,
        atomic: bool = False,
        crlf: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content.replace("\n", "\r\n") if crlf else content
    target = path.with_suffix(path.suffix + ".tmp") if atomic else path
    target.write_text(text, encoding="utf-8")
    if atomic:
        os.replace(target, path)
    if private:
        _chmod_private(path)
    if executable:
        _make_executable(path)


def _write_script(layout: Layout, name: str, ext: str, content: str) -> Path:
    path = layout.script(name, ext)
    _write_text_file(path, content, executable=(ext == "sh"), crlf=(ext == "bat"))
    return path


def _rmtree(path: Path) -> None:
    """Remove a directory tree (best-effort handling for read-only files on Windows)."""
    if not path.exists():
        return

    def onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            raise

    shutil.rmtree(path, onerror=onerror)


def _require_table(d: Dict[str, Any], key: str) -> Dict[str, Any]:
    v = d.get(key)
    if not isinstance(v, dict):
        raise Exception(f"Missing or invalid [{key}] table in INI.")
    return v


def _require_str(d: Dict[str, Any], key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise Exception(f"Missing or invalid '{key}' (expected non-empty string).")
    return v


def _require_int(d: Dict[str, Any], key: str) -> int:
    v = d.get(key)
    if not isinstance(v, int):
        raise Exception(f"Missing or invalid '{key}' (expected integer).")
    return v


def _require_list_str(d: Dict[str, Any], key: str) -> list[str]:
    v = d.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or any((not isinstance(x, str) or not x.strip()) for x in v):
        raise Exception(f"Missing or invalid '{key}' (expected list of non-empty strings).")
    return [x.strip() for x in v]


def _render_resolved_ini(cp: configparser.ConfigParser, *, redact: bool = False) -> str:
    """Render an interpolated INI copy. ConfigParser does not preserve comments."""
    rendered = configparser.ConfigParser(interpolation=None)

    for section in cp.sections():
        rendered.add_section(section)
        for option in cp._sections.get(section, {}).keys():
            value = cp.get(section, option, raw=False)
            if redact and _is_sensitive_key(option):
                value = "******"
            rendered.set(section, option, value)

    buf = io.StringIO()
    rendered.write(buf)
    return buf.getvalue()


def _ini_for_audit_log(cp: configparser.ConfigParser) -> str:
    """Return resolved INI content suitable for audit logs."""
    return _render_resolved_ini(cp, redact=True)


def _ini_for_effective_file(cp: configparser.ConfigParser) -> str:
    """Return resolved INI content suitable for saving as the effective project file."""
    return _render_resolved_ini(cp, redact=False)


def _ini_for_merged_source_file(cp: configparser.ConfigParser) -> str:
    """Return merged INI content without resolving interpolation placeholders."""
    rendered = configparser.ConfigParser(interpolation=None)

    for section in cp.sections():
        rendered.add_section(section)
        for option in cp._sections.get(section, {}).keys():
            rendered.set(section, option, cp.get(section, option, raw=True))

    buf = io.StringIO()
    rendered.write(buf)
    return buf.getvalue()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_run_id() -> str:
    timestamp = _utc_now_iso().replace(":", "-")
    return f"{timestamp}-{os.getpid()}"


def _is_sensitive_key(key: str) -> bool:
    key_l = key.lower()
    return any(marker in key_l for marker in _SENSITIVE_KEYS)


def _redact_url_secret(value: str) -> str:
    try:
        parsed = urlparse(value)
    except Exception:
        return value

    if not parsed.scheme or not parsed.netloc or parsed.password is None:
        return value

    username = unquote(parsed.username or "")
    hostname = parsed.hostname or ""
    host = hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"

    netloc = f"{username}:******@{host}" if username else f"******@{host}"
    return urlunparse(parsed._replace(netloc=netloc))


def _redact_value(key: str, value: Any) -> Any:
    if _is_sensitive_key(key):
        return "******"
    if isinstance(value, str):
        return _redact_url_secret(value)
    return value


def _redact_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact_value(str(k), _redact_mapping(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_mapping(v) for v in value]
    if isinstance(value, tuple):
        return [_redact_mapping(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, PurePosixPath):
        return value.as_posix()
    return value


def _redact_cli_assignment(raw: str, sectioned: bool = False) -> str:
    """Redact the value part of KEY=VALUE or SECTION:KEY=VALUE when KEY is sensitive."""
    if "=" not in raw:
        return _redact_url_secret(raw)

    target, value = raw.split("=", 1)
    key = target.split(":", 1)[1] if sectioned and ":" in target else target
    if _is_sensitive_key(key):
        return f"{target}=******"
    return f"{target}={_redact_url_secret(value)}"


def _redact_cli_args(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next_key: Optional[str] = None

    for arg in argv:
        if redact_next_key is not None:
            if redact_next_key in {"-e", "--extra-var"}:
                redacted.append(_redact_cli_assignment(arg))
            elif redact_next_key in {"-S", "--set"}:
                redacted.append(_redact_cli_assignment(arg, sectioned=True))
            elif _is_sensitive_key(redact_next_key):
                redacted.append("******")
            else:
                redacted.append(_redact_url_secret(arg))
            redact_next_key = None
            continue

        if arg in {"-e", "--extra-var"}:
            redacted.append(arg)
            redact_next_key = arg
            continue

        if arg in {"-S", "--set"}:
            redacted.append(arg)
            redact_next_key = arg
            continue

        if arg.startswith("--extra-var="):
            raw = arg.split("=", 1)[1]
            redacted.append(f"--extra-var={_redact_cli_assignment(raw)}")
            continue

        if arg.startswith("--set="):
            raw = arg.split("=", 1)[1]
            redacted.append(f"--set={_redact_cli_assignment(raw, sectioned=True)}")
            continue

        # argparse accepts the short option value as either '-S value' or '-Svalue'.
        if arg.startswith("-S") and arg != "-S":
            raw = arg[2:]
            redacted.append(f"-S{_redact_cli_assignment(raw, sectioned=True)}")
            continue

        if arg.startswith("--") and "=" in arg:
            option, raw_value = arg.split("=", 1)
            redacted.append(f"{option}=******" if _is_sensitive_key(option) else f"{option}={_redact_url_secret(raw_value)}")
            continue

        if arg.startswith("--") and _is_sensitive_key(arg):
            redacted.append(arg)
            redact_next_key = arg
            continue

        redacted.append(_redact_url_secret(arg))

    return redacted


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, PurePosixPath):
        return value.as_posix()
    return str(value)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_text_file(
        path,
        json.dumps(payload, indent=2, sort_keys=False, default=_json_default) + "\n",
        private=True,
        atomic=True,
    )


def _provisioning_paths(layout: Layout, run_id: str) -> tuple[Path, Path, Path, Path]:
    base_dir = layout.root / ".odt-env"
    run_path = base_dir / "history" / f"{run_id}-provisioning.json"
    source_ini_path = base_dir / "history" / f"{run_id}.source-project.ini"
    resolved_ini_path = base_dir / "history" / f"{run_id}.resolved-project.ini"
    last_path = base_dir / "last-provisioning.json"
    return run_path, source_ini_path, resolved_ini_path, last_path


def _write_provisioning_record(layout: Layout, run_id: str, record: dict[str, Any]) -> None:
    run_path, _source_ini_path, _resolved_ini_path, last_path = _provisioning_paths(layout, run_id)
    _write_json_atomic(run_path, record)
    _write_json_atomic(last_path, record)


def _apply_missing_config_defaults(
        cp: configparser.ConfigParser,
        defaults: Optional[Dict[str, str]],
        log_defaults: bool = True,
) -> bool:
    """Apply missing [config] defaults and return True if the INI changed."""
    if not defaults:
        return False

    changed = False
    if not cp.has_section("config"):
        cp.add_section("config")
        changed = True

    for key, value in defaults.items():
        if cp.has_option("config", key):
            continue
        if log_defaults:
            log_value = "******" if _is_sensitive_key(key) else value
            _logger.info("Applying implicit default to [config].%s=%s", key, log_value)
        cp.set("config", key, value)
        changed = True

    return changed


def _write_default_project_ini_template(ini_path: Path) -> None:
    """Create the bundled default project INI template at ``ini_path``."""
    if ini_path.exists():
        if not ini_path.is_file():
            raise Exception(f"Default INI path exists but is not a file: {ini_path}")
        return

    _write_text_file(
        ini_path,
        _DEFAULT_PROJECT_INI_TEMPLATE,
        private=True,
        atomic=True,
    )

    _logger.info("Created default project INI template: %s", ini_path)


def _implicit_ini_needs_effective_save(
        ini_path: Path,
        vars_overrides: Optional[Dict[str, str]],
        ini_overrides: Optional[Dict[str, Dict[str, str]]],
        config_defaults: Optional[Dict[str, str]],
        force: bool = False,
) -> bool:
    """Return True when implicit-INI mode should persist an effective project file."""
    if force or vars_overrides or ini_overrides:
        return True
    if not config_defaults:
        return False

    cp = _read_ini(
        ini_path,
        vars_overrides=None,
        ini_overrides=None,
        log_overrides=False,
    )
    if not cp.has_section("config"):
        return True
    return any(not cp.has_option("config", key) for key in config_defaults)


def _resolved_ini_for_manifest(
        ini_path: Path,
        vars_overrides: Optional[Dict[str, str]],
        ini_overrides: Optional[Dict[str, Dict[str, str]]] = None,
) -> Optional[str]:
    try:
        cp = _read_ini(
            ini_path,
            vars_overrides=vars_overrides,
            ini_overrides=ini_overrides,
            log_overrides=False,
        )
        return _ini_for_audit_log(cp)
    except Exception as e:
        _logger.warning("Failed to render resolved INI for provisioning manifest: %s", e)
        return None


def _source_ini_for_manifest(layout: Layout, ini_path: Path) -> Optional[Path]:
    """Return the best source INI to copy into the per-run provisioning audit."""
    source_project_ini_path = layout.root / ".odt-env" / "last-source-project.ini"
    resolved_project_ini_path = layout.root / ".odt-env" / "last-resolved-project.ini"

    # When a remote/implicit project file was materialized, the workspace-root
    # INI is rewritten to its effective resolved form and the original source is
    # kept separately. Use that source only when it clearly matches the current
    # effective project file; otherwise fall back to the INI path passed to this
    # run to avoid copying a stale last-source-project.ini from a previous run.
    if source_project_ini_path.is_file() and resolved_project_ini_path.is_file() and ini_path.is_file():
        try:
            ini_parent = ini_path.resolve().parent
        except Exception:
            ini_parent = ini_path.absolute().parent
        try:
            if ini_parent == layout.root.resolve() and ini_path.read_bytes() == resolved_project_ini_path.read_bytes():
                return source_project_ini_path
        except OSError:
            pass

    if ini_path.is_file():
        return ini_path
    if source_project_ini_path.is_file():
        return source_project_ini_path
    return None


def _copy_source_ini_for_manifest(layout: Layout, run_id: str, ini_path: Path) -> Optional[Path]:
    source_path = _source_ini_for_manifest(layout, ini_path)
    if source_path is None:
        _logger.warning("Failed to locate source INI for provisioning manifest: %s", ini_path)
        return None

    _run_path, source_ini_path, _resolved_ini_path, _last_path = _provisioning_paths(layout, run_id)
    source_ini_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, source_ini_path)
    _chmod_private(source_ini_path)

    return source_ini_path


def _save_remote_ini_source_copy(ini_path: Path) -> Path:
    """Keep the original downloaded/copied remote INI under ROOT/.odt-env."""
    root = ini_path.parent
    source_ini_path = root / ".odt-env" / "last-source-project.ini"
    source_ini_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ini_path, source_ini_path)
    return source_ini_path


def _save_resolved_project_ini_copy(ini_path: Path, resolved_ini: str) -> Path:
    """Keep the effective resolved project INI under ROOT/.odt-env."""
    root = ini_path.parent
    resolved_ini_path = root / ".odt-env" / "last-resolved-project.ini"
    _write_text_file(
        resolved_ini_path,
        resolved_ini,
        private=True,
        atomic=True,
    )

    return resolved_ini_path


def _save_effective_ini_copy(
    ini_path: Path,
    vars_overrides: Optional[Dict[str, str]],
    ini_overrides: Optional[Dict[str, Dict[str, str]]] = None,
    config_defaults: Optional[Dict[str, str]] = None,
) -> None:
    """
    Rewrite a workspace-root INI with the values actually used after CLI
    overrides and optional implicit defaults.

    The original template/source file is kept under ROOT/.odt-env/last-source-project.ini,
    and the resolved/effective copy is kept under ROOT/.odt-env/last-resolved-project.ini.
    """
    cp = _read_ini(
        ini_path,
        vars_overrides=vars_overrides,
        ini_overrides=ini_overrides,
        log_overrides=False,
    )
    _apply_missing_config_defaults(cp, config_defaults, log_defaults=False)
    source_ini_path = _save_remote_ini_source_copy(ini_path)
    resolved_ini = _ini_for_effective_file(cp)

    _write_text_file(
        ini_path,
        resolved_ini,
        private=True,
        atomic=True,
    )

    resolved_project_ini_path = _save_resolved_project_ini_copy(ini_path, resolved_ini)

    _logger.info(
        "Saved effective INI to workspace root: %s (original source/template: %s, resolved copy: %s)",
        ini_path,
        source_ini_path,
        resolved_project_ini_path,
    )


def _collect_provisioning_details(
        cfg: ProjectConfig,
        source_ini_path: Optional[Path],
        resolved_ini_path: Optional[Path],
) -> dict[str, Any]:
    return {
        "config": _redact_mapping(asdict(cfg)),
        "artifacts": {
            "source_ini": str(source_ini_path) if source_ini_path and source_ini_path.exists() else None,
            "resolved_ini": str(resolved_ini_path) if resolved_ini_path and resolved_ini_path.exists() else None,
        },
    }


# -----------------------------
# INI loading
# -----------------------------

def _split_cli_assignment(item: str, option: str, expected: str) -> tuple[str, str]:
    raw_item = (item or "").strip()
    if not raw_item or "=" not in raw_item:
        raise Exception(f"Invalid {option} value '{item}' (expected format {expected}).")

    target, value = raw_item.split("=", 1)
    target = target.strip()
    if not target:
        raise Exception(f"Invalid {option} value '{item}' (expected non-empty {expected}).")
    return target, value.strip()


def _parse_cli_vars(extra_vars: list[str]) -> Dict[str, str]:
    overrides: Dict[str, str] = {}

    for item in extra_vars:
        key, value = _split_cli_assignment(item, "-e/--extra-var", "KEY=VALUE")
        if key in overrides:
            _logger.warning("CLI override for [vars].%s redefined; using last value.", key)
        overrides[key] = value

    return overrides


def _parse_cli_ini_overrides(raw_overrides: list[str]) -> Dict[str, Dict[str, str]]:
    """Parse -S/--set overrides in SECTION:KEY=VALUE format."""
    overrides: Dict[str, Dict[str, str]] = {}

    for item in raw_overrides:
        target, value = _split_cli_assignment(item, "-S/--set", "SECTION:KEY=VALUE")
        if ":" not in target:
            raise Exception(f"Invalid -S/--set value '{item}' (expected format SECTION:KEY=VALUE).")

        section, key = target.split(":", 1)
        section = section.strip()
        key = key.strip().lower()
        if not section or not key:
            raise Exception(f"Invalid -S/--set value '{item}' (expected non-empty SECTION and KEY).")

        section_overrides = overrides.setdefault(section, {})
        if key in section_overrides:
            _logger.warning("CLI override for [%s].%s redefined; using last value.", section, key)
        section_overrides[key] = value

    return overrides


def _validate_ini_overrides_exist(
        cp: configparser.ConfigParser,
        ini_overrides: Dict[str, Dict[str, str]],
) -> None:
    """Require --set targets to exist, except new [config] options.

    The [config] section maps directly to generated Odoo configuration options,
    so allowing new keys there keeps reusable templates small while preserving
    strict validation for odt-env's own structured sections.
    """
    for section, options in ini_overrides.items():
        if not cp.has_section(section):
            if section == "config":
                continue
            raise Exception(
                f"Invalid -S/--set override: section [{section}] does not exist in the INI file. "
                "--set can only create new options in [config]."
            )

        for key in options:
            if section == "config":
                continue

            if not cp.has_option(section, key):
                raise Exception(
                    f"Invalid -S/--set override: option '{key}' does not exist in section [{section}] "
                    "in the INI file. --set can only create new options in [config]."
                )


def _read_ini(
        entry_ini: Path,
        vars_overrides: Optional[Dict[str, str]] = None,
        ini_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        log_overrides: bool = True,
) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())
    read_ok = cp.read(entry_ini, encoding="utf-8")
    if not read_ok:
        raise Exception(f"Failed to read INI config: {entry_ini}")

    # Validate --set against the original INI content before -e/--extra-var can
    # inject anything into [vars]. New options are allowed only in [config].
    if ini_overrides:
        _validate_ini_overrides_exist(cp, ini_overrides)
        if "config" in ini_overrides and not cp.has_section("config"):
            cp.add_section("config")

    if vars_overrides:
        if not cp.has_section("vars"):
            cp.add_section("vars")

        for key, value in vars_overrides.items():
            opt_l = key.lower()
            if log_overrides:
                log_value = "******" if any(k in opt_l for k in _SENSITIVE_KEYS) else value
                _logger.info("Applying CLI override to [vars].%s=%s", key, log_value)
            cp.set("vars", key, value)

    if ini_overrides:
        for section, options in ini_overrides.items():
            for key, value in options.items():
                if log_overrides:
                    log_value = "******" if _is_sensitive_key(key) else value
                    _logger.info("Applying CLI override to [%s].%s=%s", section, key, log_value)
                cp.set(section, key, value)

    return cp


def _parse_odoo_version(odoo_version: str) -> int:
    raw_value = (odoo_version or "").strip()
    match = re.fullmatch(r"(\d+)\.0", raw_value)
    if not match:
        raise Exception(
            "Invalid option 'version' in section [odoo] "
            "(expected format 'X.0', for example '12.0' or '13.0')."
        )
    return int(match.group(1))


def _validate_docker_service_name(value: str, option: str) -> str:
    service_name = (value or "").strip()
    if not service_name:
        raise Exception(
            f"Invalid option '{option}' in section [docker] "
            "(expected non-empty Docker Compose service name)."
        )

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service_name):
        raise Exception(
            f"Invalid option '{option}' in section [docker]: '{service_name}'. "
            "Use only letters, digits, dots, underscores, or hyphens, and start with a letter or digit."
        )

    return service_name


def _validate_docker_compose_project_name(value: Optional[str], option: str) -> Optional[str]:
    compose_project_name = (value or "").strip()
    if not compose_project_name:
        return None

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", compose_project_name):
        raise Exception(
            f"Invalid option '{option}' in section [docker]: '{compose_project_name}'. "
            "Use only lowercase letters, digits, underscores, or hyphens, "
            "and start with a lowercase letter or digit."
        )

    return compose_project_name


def _validate_docker_image(value: str, option: str) -> str:
    image = (value or "").strip()
    if not image:
        raise Exception(
            f"Invalid option '{option}' in section [docker] "
            "(expected non-empty Docker image name/tag)."
        )

    return image


def _validate_docker_addons_mode(value: str, option: str) -> str:
    addons_mode = (value or "").strip().lower() or _DEFAULT_DOCKER_ADDONS_MODE
    if addons_mode not in _DOCKER_ADDONS_MODES:
        supported = ", ".join(sorted(_DOCKER_ADDONS_MODES))
        raise Exception(
            f"Invalid option '{option}' in section [docker]: '{addons_mode}'. "
            f"Supported values: {supported}."
        )
    return addons_mode


def _get_default_virtualenv_settings(odoo_version: str) -> tuple[str, list[str], list[str]]:
    odoo_major_version = _parse_odoo_version(odoo_version)

    if odoo_major_version not in _ODOO_VENV_SETTINGS:
        supported_versions = ", ".join(f"{version}.0" for version in sorted(_ODOO_VENV_SETTINGS))
        raise Exception(
            f"Unsupported Odoo version '{odoo_version}' in section [odoo]. "
            f"Supported versions: {supported_versions}"
        )

    _min_python_version, max_python_version, build_constraints, requirements = _ODOO_VENV_SETTINGS[odoo_major_version]
    return max_python_version, list(build_constraints), list(requirements)


def _require_ini_option(cp: configparser.ConfigParser, section: str, option: str) -> str:
    if not cp.has_section(section):
        raise Exception(f"Missing INI section: [{section}]")
    if not cp.has_option(section, option):
        raise Exception(f"Missing option '{option}' in section [{section}]")
    return cp.get(section, option)


def _get_ini_list(cp: configparser.ConfigParser, section: str, option: str) -> list[str]:
    if not cp.has_section(section) or not cp.has_option(section, option):
        return []
    # Multi-line INI values represent lists. Empty value means empty list.
    return [line.strip() for line in cp.get(section, option).splitlines() if line.strip()]


def _get_ini_bool(
        cp: configparser.ConfigParser,
        section: str,
        option: str,
        default: bool = False,
) -> bool:
    if not cp.has_section(section) or not cp.has_option(section, option):
        return default
    try:
        return cp.getboolean(section, option)
    except ValueError as e:
        raise Exception(
            f"Invalid value for option '{option}' in section [{section}] (expected a boolean like true/false)."
        ) from e


def load_project_config(
        ini_path: Path,
        vars_overrides: Optional[Dict[str, str]] = None,
        ini_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        log_resolved: bool = True,
        log_overrides: bool = True,
) -> ProjectConfig:
    if not ini_path.exists():
        raise Exception(f"INI config not found: {ini_path}")

    cp = _read_ini(
        ini_path,
        vars_overrides=vars_overrides,
        ini_overrides=ini_overrides,
        log_overrides=log_overrides,
    )

    if log_resolved:
        _logger.info("Loaded INI (resolved) from %s:\n\n%s", ini_path, _ini_for_audit_log(cp))

    # Sections expected:
    #   [virtualenv] (optional)
    #   [odoo]
    #   [addons.<name>] for each addon (optional)
    #   [docker] (optional)
    #   [config] (optional)

    odoo_version = _require_ini_option(cp, "odoo", "version").strip()
    default_python_version, default_build_constraints, default_requirements = _get_default_virtualenv_settings(
        odoo_version
    )

    python_version = cp.get("virtualenv", "python_version", fallback="").strip()
    if not python_version:
        python_version = default_python_version

    ini_build_constraints = _get_ini_list(cp, "virtualenv", "build_constraints")
    ini_requirements = _get_ini_list(cp, "virtualenv", "requirements")

    build_constraints = list(dict.fromkeys([
        *default_build_constraints,
        *ini_build_constraints,
    ]))
    requirements = list(dict.fromkeys([
        *default_requirements,
        *ini_requirements,
    ]))

    explicit_requirements = list(ini_requirements)
    explicit_requirements_ignore = _get_ini_list(cp, "virtualenv", "requirements_ignore")

    requirements_ignore = list(explicit_requirements_ignore)
    for spec in requirements:
        name = _extract_req_name_from_spec(spec)
        if name and name not in requirements_ignore:
            requirements_ignore.append(name)

    venv = VirtualenvConfig(
        python_version=python_version,
        build_constraints=build_constraints,
        requirements=requirements,
        requirements_ignore=requirements_ignore,
        explicit_requirements=explicit_requirements,
        explicit_requirements_ignore=explicit_requirements_ignore,
        managed_python=_get_ini_bool(cp, "virtualenv", "managed_python", default=True),
    )

    has_odoo_path = cp.has_option("odoo", "path")
    has_odoo_repo = cp.has_option("odoo", "repo")
    has_odoo_branch = cp.has_option("odoo", "branch")
    has_odoo_commit = cp.has_option("odoo", "commit")
    has_odoo_shallow = cp.has_option("odoo", "shallow")

    odoo_path = cp.get("odoo", "path", fallback="").strip() if has_odoo_path else ""
    if has_odoo_path:
        if not odoo_path:
            raise Exception(
                "Invalid option 'path' in section [odoo] (expected non-empty string)."
            )
        if has_odoo_repo or has_odoo_branch or has_odoo_commit or has_odoo_shallow:
            raise Exception(
                "Invalid Odoo source in section [odoo]: "
                "use either 'path' only for a local Odoo source, or Git settings "
                "('repo', 'branch', optional 'commit' and 'shallow') for a Git Odoo source."
            )
        odoo = OdooSpec(
            repo=_DEFAULT_ODOO_REPO,
            branch=odoo_version,
            version=odoo_version,
            path=odoo_path,
        )
    else:
        odoo_repo = cp.get("odoo", "repo", fallback="").strip() or _DEFAULT_ODOO_REPO
        odoo_branch = cp.get("odoo", "branch", fallback="").strip() or odoo_version
        odoo_commit = cp.get("odoo", "commit", fallback="").strip() or None

        odoo = OdooSpec(
            repo=odoo_repo,
            branch=odoo_branch,
            commit=odoo_commit,
            version=odoo_version,
            shallow=_get_ini_bool(cp, "odoo", "shallow", default=True),
        )

    # Addons are optional. If there are no [addons.<name>] sections, keep addons empty.
    addons: Dict[str, AddonSpec] = {}
    for sec in cp.sections():
        if sec.startswith("addons."):
            name = sec.split(".", 1)[1]

            has_path = cp.has_option(sec, "path")
            has_repo = cp.has_option(sec, "repo")
            has_branch = cp.has_option(sec, "branch")
            has_commit = cp.has_option(sec, "commit")
            has_shallow = cp.has_option(sec, "shallow")

            path_value = cp.get(sec, "path", fallback="").strip() if has_path else ""
            if has_path:
                if not path_value:
                    raise Exception(
                        f"Invalid option 'path' in section [{sec}] (expected non-empty string)."
                    )
                if has_repo or has_branch or has_commit or has_shallow:
                    raise Exception(
                        f"Invalid addon source in section [{sec}]: "
                        "use either 'path' only for a local addon, or 'repo' + 'branch' "
                        "(+ optional 'commit' and 'shallow') for a git addon."
                    )
                addons[name] = AddonSpec(path=path_value)
                continue

            addons[name] = AddonSpec(
                repo=_require_ini_option(cp, sec, "repo"),
                branch=_require_ini_option(cp, sec, "branch"),
                commit=cp.get(sec, "commit", fallback="").strip() or None,
                shallow=_get_ini_bool(cp, sec, "shallow", default=True),
            )

    docker = DockerConfig(base_image=f"odoo:{odoo_version}")
    if cp.has_section("docker"):
        supported_docker_options = {"target_image", "base_image", "addons_mode", "compose_project_name", "db_service", "odoo_service"}
        for key in cp._sections.get("docker", {}).keys():
            if key not in supported_docker_options:
                raise Exception(
                    f"Unsupported option '{key}' in section [docker]. "
                    "Supported options: target_image, base_image, addons_mode, compose_project_name, db_service, odoo_service."
                )

        target_image = (
            _validate_docker_image(cp.get("docker", "target_image"), "target_image")
            if cp.has_option("docker", "target_image")
            else None
        )
        base_image = (
            _validate_docker_image(cp.get("docker", "base_image"), "base_image")
            if cp.has_option("docker", "base_image")
            else f"odoo:{odoo_version}"
        )
        docker_addons_mode = _validate_docker_addons_mode(
            cp.get("docker", "addons_mode", fallback=_DEFAULT_DOCKER_ADDONS_MODE),
            "addons_mode",
        )
        compose_project_name = _validate_docker_compose_project_name(
            cp.get("docker", "compose_project_name", fallback=""),
            "compose_project_name",
        )
        db_service = _validate_docker_service_name(
            cp.get("docker", "db_service", fallback=_DEFAULT_DOCKER_DB_SERVICE),
            "db_service",
        )
        odoo_service = _validate_docker_service_name(
            cp.get("docker", "odoo_service", fallback=_DEFAULT_DOCKER_ODOO_SERVICE),
            "odoo_service",
        )

        if db_service == odoo_service:
            raise Exception(
                "Invalid [docker] section: db_service and odoo_service must be different."
            )

        docker = DockerConfig(
            target_image=target_image,
            base_image=base_image,
            addons_mode=docker_addons_mode,
            compose_project_name=compose_project_name,
            db_service=db_service,
            odoo_service=odoo_service,
        )

    config: Dict[str, Any] = {}
    # [config] is optional. Only include keys explicitly defined in [config]
    # (exclude DEFAULT values).
    for key in cp._sections.get("config", {}).keys():
        if key == "addons_path":
            raise Exception(
                "Option 'addons_path' in section [config] is not allowed. "
                "addons_path is always generated automatically; add addons only via [addons.<name>] sections."
            )
        config[key] = cp.get("config", key)

    return ProjectConfig(virtualenv=venv, odoo=odoo, addons=addons, config=config, docker=docker)


def require_venv(
        layout: Layout,
        python_version: str,
        reuse_wheelhouse: bool = False,
        managed_python: bool = True,
) -> None:
    venv_dir = layout.root / "venv"

    if not (python_version or "").strip():
        raise Exception("Missing required uv python version (python_version).")

    _ensure_command("uv")

    if venv_dir.exists() and not venv_dir.is_dir():
        raise Exception(f"venv path exists but is not a directory: {venv_dir}")

    if not venv_dir.exists():
        # Install managed python
        if managed_python:
            is_windows = sys.platform.startswith("win")
            if is_windows:
                cpy_tag = f"cpython-{python_version}-windows-x86_64-none"
            else:
                cpy_tag = f"cpython-{python_version}-linux-x86_64-gnu"
            cmd = ["uv", "python", "install", cpy_tag]
            _logger.info(f"Installing managed python {python_version} (x64) with uv: {cpy_tag}")
            _run_checked(
                cmd,
                cwd=layout.root,
                err_msg=f"Failed to install managed python {python_version} (x64) with uv: {cpy_tag}",
            )

        # Create virtualenv
        _logger.info("Creating virtualenv with uv: %s (python=%s)", venv_dir, python_version)
        cmd = [
            "uv", "venv",
            "-p", python_version,
            str(venv_dir),
        ]
        if not managed_python:
            cmd.extend([
                "--no-managed-python",
            ])
        _run_checked(
            cmd,
            cwd=layout.root,
            err_msg=f"Failed to create virtualenv at: {venv_dir}",
        )

        # Install seed packages into virtualenv
        if not reuse_wheelhouse:
            venv_py = venv_dir / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
            if not venv_py.exists():
                raise Exception(f"venv python not found at expected path: {venv_py}")
            seed_packages = [
                "pip",
                "setuptools",
                "wheel",
            ]
            _logger.info("Installing seed packages into venv: %s", venv_dir)
            cmd = ["uv", "pip", "install", "-p", str(venv_py), *seed_packages]
            _run_checked(
                cmd,
                cwd=layout.root,
                err_msg="Failed to install seed packages into venv.",
            )


# -----------------------------
# Requirements filtering helpers
# -----------------------------

def _canonicalize_project_name(name: str) -> str:
    """Canonicalize a Python distribution name similar to packaging.utils.canonicalize_name."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _strip_inline_comment(line: str) -> str:
    """Remove trailing comments (a '#' preceded by whitespace)."""
    m = re.search(r"\s+#", line)
    return line[: m.start()].rstrip() if m else line.rstrip()


def _extract_req_name_from_spec(spec: str) -> Optional[str]:
    """Best-effort extraction of a requirement project name from a requirement spec line."""
    s = spec.strip()
    if not s:
        return None

    # VCS/URL requirement like: git+...#egg=foo
    if "egg=" in s:
        m = re.search(r"[#&]egg=([^&]+)", s)
        if m:
            return _canonicalize_project_name(m.group(1))

    # Direct reference: name @ https://...
    if "@" in s:
        left, right = s.split("@", 1)
        if left.strip() and right.strip():
            return _canonicalize_project_name(left.strip())

    # Standard requirement: name[extra] >= 1.0 ; markers
    m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", s)
    if m:
        return _canonicalize_project_name(m.group(1))

    return None


def _filter_requirements_file(
        req_path: Path,
        ignore_names: set[str],
        visited: set[Path],
) -> list[str]:
    """Return requirements file content with ignored packages removed. Supports nested -r includes."""
    out_lines: list[str] = []

    try:
        raw_lines = req_path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise Exception(f"Failed to read requirements file: {req_path} ({e})") from e

    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(raw)
            continue

        no_comment = _strip_inline_comment(raw)

        # Include other requirement files (inline them so ignore works recursively).
        if no_comment.startswith(("-r ", "--requirement ")):
            parts = no_comment.split(maxsplit=1)
            if len(parts) == 2:
                include_rel = parts[1].strip()
                include_path = (req_path.parent / include_rel).resolve()

                out_lines.append(f"# odt-env: begin include {include_rel}")
                if include_path in visited:
                    out_lines.append(f"# odt-env: skipped recursive include {include_rel}")
                else:
                    visited.add(include_path)
                    out_lines.extend(_filter_requirements_file(include_path, ignore_names, visited=visited))
                out_lines.append(f"# odt-env: end include {include_rel}")
                continue

        # Editable installs: -e <spec> / --editable <spec>
        spec = no_comment.strip()
        if spec.startswith(("-e ", "--editable ")):
            parts = spec.split(maxsplit=1)
            spec = parts[1] if len(parts) == 2 else ""

        name = _extract_req_name_from_spec(spec)
        if name and name in ignore_names:
            out_lines.append(f"# odt-env: skipped (ignored package '{name}'): {raw}")
            continue

        out_lines.append(raw)

    return out_lines


def _requirements_ignore_set(requirements_ignore: list[str]) -> set[str]:
    ignore_set: set[str] = set()
    for spec in requirements_ignore or []:
        spec = spec.strip()
        if not spec:
            continue
        ignore_set.add(_extract_req_name_from_spec(spec) or _canonicalize_project_name(spec))
    return ignore_set


def _path_label(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return str(path)


def _collect_requirement_file_lines(
        workspace_root: Path,
        requirement_files: list[Path],
        ignore_set: set[str],
) -> list[str]:
    lines: list[str] = []
    for req_path in requirement_files:
        if not req_path.exists():
            # Skip silently; callers may include optional files.
            continue

        resolved_req_path = req_path.resolve()
        lines.append(f"# --- from {_path_label(resolved_req_path, workspace_root)} ---")
        lines.extend(
            _filter_requirements_file(
                resolved_req_path,
                ignore_set,
                visited={resolved_req_path},
            )
        )
        lines.append("")
    return lines


def _write_requirements_input(path: Path, lines: list[str]) -> None:
    _write_text_file(path, "\n".join(lines).rstrip("\n") + "\n")


def _add_requirements_section(lines: list[str], title: str, requirements: list[str]) -> None:
    if not requirements:
        return
    lines.append(title)
    lines.extend(requirements)
    lines.append("")


def _add_build_constraints(cmd: list[str], build_constraints_path: Path) -> None:
    if build_constraints_path.is_file():
        cmd.extend(["--build-constraints", str(build_constraints_path)])


def compile_all_requirements_lock(
        venv_python: Path,
        workspace_root: Path,
        requirement_files: list[Path],
        base_requirements: list[str],
        requirements_ignore: list[str],
        output_lock_path: Path,
        wheelhouse_dir: Path,
        build_constraints_path: Path,
) -> Path:
    """Compile a single lock file from all Python requirement sources."""
    _ensure_command("uv")
    wheelhouse_dir.mkdir(parents=True, exist_ok=True)
    in_path = wheelhouse_dir / "all-requirements.in.txt"

    lines: list[str] = [
        "# This file is generated by odt-env (DO NOT EDIT).",
        "# Source: Odoo + addon repository requirements, plus [virtualenv].requirements and odt-env defaults.",
        "",
    ]
    _add_requirements_section(
        lines,
        "# --- base requirements (from INI + odt-env defaults) ---",
        base_requirements,
    )
    lines.extend(
        _collect_requirement_file_lines(
            workspace_root,
            requirement_files,
            _requirements_ignore_set(requirements_ignore),
        )
    )
    _write_requirements_input(in_path, lines)

    cmd = [
        "uv", "pip", "compile",
        "-p", str(venv_python),
        str(in_path),
        "-o", str(output_lock_path),
    ]
    _add_build_constraints(cmd, build_constraints_path)

    _logger.info("Compiling lock file with uv: %s -> %s", in_path, output_lock_path)
    _run_checked(
        cmd,
        cwd=workspace_root,
        err_msg=(
            "Failed to compile requirements lock file.\n"
            f"Input: {in_path}\n"
            f"Output: {output_lock_path}"
        ),
    )
    return output_lock_path


def build_wheelhouse_from_requirements(
        venv_python: Path,
        workspace_root: Path,
        requirements_path: Path,
        wheelhouse_dir: Path,
        build_constraints_path: Path,
        clear_pip_wheel_cache: bool = True,
) -> None:
    """Build an offline wheelhouse from a requirements lock file."""
    if not requirements_path.exists():
        raise Exception(f"Requirements file not found: {requirements_path}")

    _ensure_command("uv")
    wheelhouse_dir.mkdir(parents=True, exist_ok=True)

    if clear_pip_wheel_cache:
        _logger.info("Clearing pip's wheel cache")
        _run_checked(
            [str(venv_python), "-m", "pip", "cache", "purge"],
            cwd=workspace_root,
            err_msg="Failed to clear pip's wheel cache.",
        )

    if build_constraints_path.is_file():
        _logger.info("Installing build constraints to virtualenv: %s", build_constraints_path)
        _run_checked(
            ["uv", "pip", "install", "-p", str(venv_python), "-U", "-r", str(build_constraints_path)],
            cwd=workspace_root,
            err_msg=f"Failed to install build constraints to virtualenv: {build_constraints_path}",
        )

    cmd = [
        str(venv_python), "-m", "pip", "wheel",
        "-r", str(requirements_path),
        "-w", str(wheelhouse_dir),
        "--no-deps",
    ]
    _logger.info("Creating wheelhouse: %s -> %s", requirements_path, wheelhouse_dir)
    _run_checked(cmd, cwd=workspace_root, err_msg="Failed to create wheelhouse.")


def pip_install_requirements_file(
        venv_python: Path,
        workspace_root: Path,
        requirements_path: Path,
        wheelhouse_dir: Path,
) -> None:
    """Install a requirements lock from the local wheelhouse using uv pip sync."""
    if not requirements_path.exists():
        raise Exception(f"Requirements file not found: {requirements_path}")

    _ensure_command("uv")
    cmd = [
        "uv", "pip", "sync", "-p", str(venv_python),
        "--offline", "--no-index",
        "-f", str(wheelhouse_dir),
        str(requirements_path),
    ]

    _logger.info("Installing requirements from wheelhouse: %s", requirements_path)
    _run_checked(cmd, cwd=workspace_root, err_msg="Failed to install requirements from wheelhouse.")


# -----------------------------
# Git operations
# -----------------------------

def _run(cmd: list[str], cwd: Optional[Path] = None) -> str:
    # Log every git command we execute (stdout only; configured in main()).
    if cmd and cmd[0] == "git":
        _logger.info("git: %s (cwd=%s)", " ".join(cmd), str(cwd) if cwd else "<cwd>")

    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
    )

    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if cmd and cmd[0] == "git":
        if out:
            _logger.info("git stdout: %s", out)
        if err:
            _logger.info("git stderr: %s", err)
    if p.returncode != 0:
        raise Exception(f"Command failed: {' '.join(cmd)} {p.stdout} {p.stderr}")
    return out


def assert_clean_worktree(repo_dir: Path) -> None:
    _logger.info("assert_clean_worktree: %s", repo_dir)
    out = _run(["git", "status", "--porcelain"], cwd=repo_dir)
    if out.strip():
        raise Exception(
            f"Local changes detected in repository: {repo_dir}\n"
            "You must commit and push your local changes (or clean the working tree) before syncing.\n"
            "Hint: `git status` to inspect, then commit/push or stash/clean as appropriate."
        )


def _is_shallow_repo(repo_dir: Path) -> bool:
    """Return True if the repository is shallow.

    We primarily rely on the presence of `.git/shallow` because it is stable
    across git versions.
    """
    return (repo_dir / ".git" / "shallow").exists()


def _ensure_full_origin_refspec(repo_dir: Path) -> None:
    """Ensure origin is configured to fetch all branches.

    Repos cloned with `--single-branch` may have a restricted refspec. When the
    user switches Odoo to a full clone/fetch, we widen origin's fetch refspec so
    a subsequent `git fetch --all` can actually bring all remote branches.
    """
    wildcard = "+refs/heads/*:refs/remotes/origin/*"

    p = subprocess.run(
        ["git", "config", "--get-all", "remote.origin.fetch"],
        cwd=str(repo_dir),
        text=True,
        capture_output=True,
    )
    existing = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
    if wildcard in existing:
        return

    # Replace whatever refspec was there with a wildcard.
    subprocess.run(
        ["git", "config", "--unset-all", "remote.origin.fetch"],
        cwd=str(repo_dir),
        text=True,
        capture_output=True,
    )
    _run(["git", "config", "--add", "remote.origin.fetch", wildcard], cwd=repo_dir)


def _unshallow_if_needed(repo_dir: Path) -> None:
    """Convert a shallow repo into a full-history repo (if needed)."""
    if not _is_shallow_repo(repo_dir):
        return

    _logger.info("Repository is shallow; converting to full history: %s", repo_dir)
    # `--unshallow` turns the repo into a full clone; safe because we check first.
    _run(["git", "fetch", "--unshallow", "--tags", "origin"], cwd=repo_dir)


def _fetch_branch(repo_dir: Path, branch: Optional[str], depth: Optional[int] = None) -> None:
    """Fetch only the requested branch from origin.

    When `depth` is provided, the fetch stays shallow. When omitted, the fetch uses
    full history for the requested branch only.
    """
    fetch_cmd: list[str] = ["git", "fetch", "--prune"]
    if depth is not None:
        fetch_cmd += ["--depth", str(depth)]
    fetch_cmd += ["origin"]
    if branch:
        fetch_cmd += [branch]
    _run(fetch_cmd, cwd=repo_dir)


def ensure_repo(
        repo_url: str,
        dest: Path,
        branch: Optional[str] = None,
        depth: Optional[int] = None,
        single_branch: bool = False,
        fetch_all: bool = True,
) -> None:
    """
    Ensure a git repository exists at `dest`.

    - If the repo does not exist, it is cloned.
      * If `branch` is provided, the clone will initially checkout that branch.
      * If `single_branch` is True, only that branch will be fetched/kept.
      * If `depth` is provided, the clone will be shallow (depth=N).

    - If the repo exists, it is fetched/updated according to the chosen strategy:
      * fetch_all=True  -> `git fetch --all --prune`
      * fetch_all=False -> fetch only `branch` from origin (optionally shallow)
    """
    _logger.info("ensure_repo: %s -> %s (branch=%s, depth=%s, single_branch=%s, fetch_all=%s)",
                 repo_url, dest, branch, depth, single_branch, fetch_all)

    if dest.exists() and (dest / ".git").exists():
        assert_clean_worktree(dest)

        if fetch_all:
            # If the caller wants a full clone/fetch and the repo is currently shallow
            # or restricted to a single branch, convert it.
            if depth is None:
                _ensure_full_origin_refspec(dest)
                _unshallow_if_needed(dest)
            _run(["git", "fetch", "--all", "--tags", "--prune"], cwd=dest)
            return

        # Fetch only the required branch (useful for shallow/single-branch workflows).
        if depth is None and _is_shallow_repo(dest):
            _unshallow_if_needed(dest)

        _fetch_branch(dest, branch=branch, depth=depth)
        return

    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = ["git", "clone"]
    if depth is not None:
        cmd += ["--depth", str(depth)]
    if branch is not None:
        cmd += ["--branch", branch]
    if single_branch:
        cmd += ["--single-branch"]
    cmd += [repo_url, str(dest)]
    _run(cmd)


def checkout_branch(dest: Path, branch: str, fetch_all: bool = True, depth: Optional[int] = None) -> None:
    """
    Checkout the requested `branch` in an existing repo.

    - fetch_all=True (default): do a broad fetch (all remotes/branches + tags), then checkout.
      This matches the previous behavior and is suitable for full clones (e.g. addons).

    - fetch_all=False: fetch ONLY `origin/<branch>` (optionally shallow via depth),
      then force local branch `<branch>` to match `origin/<branch>`.
      This is intended for the Odoo repo where we want single-branch + shallow clones.
    """
    _logger.info("checkout_branch: %s @ %s (fetch_all=%s, depth=%s)", dest, branch, fetch_all, depth)
    assert_clean_worktree(dest)

    if fetch_all:
        # If the caller wants a full clone/fetch and the repo is currently shallow
        # or restricted to a single branch, convert it.
        if depth is None:
            _ensure_full_origin_refspec(dest)
            _unshallow_if_needed(dest)
        _run(["git", "fetch", "--all", "--tags", "--prune"], cwd=dest)

        try:
            _run(["git", "rev-parse", "--verify", f"origin/{branch}"], cwd=dest)
            _run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=dest)
            assert_clean_worktree(dest)
            _run(["git", "pull", "--ff-only"], cwd=dest)
            return
        except:
            pass

        _run(["git", "checkout", branch], cwd=dest)
        return

    # Narrow fetch: only the needed branch, optionally shallow.
    fetch_cmd: list[str] = ["git", "fetch", "--prune"]
    if depth is not None:
        fetch_cmd += ["--depth", str(depth)]
    fetch_cmd += ["origin", branch]
    _run(fetch_cmd, cwd=dest)

    _run(["git", "rev-parse", "--verify", f"origin/{branch}"], cwd=dest)
    _run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=dest)
    # Ensure the working tree exactly matches the remote branch (no pull, no extra refs).
    _run(["git", "reset", "--hard", f"origin/{branch}"], cwd=dest)
    assert_clean_worktree(dest)


def _has_commit(repo_dir: Path, commit: str) -> bool:
    try:
        _run(["git", "rev-parse", "--verify", f"{commit}^{{commit}}"], cwd=repo_dir)
        return True
    except Exception:
        return False


def checkout_commit(
        dest: Path,
        commit: str,
        branch: Optional[str] = None,
        fetch_all: bool = True,
        depth: Optional[int] = None,
) -> None:
    """Fetch and checkout a specific commit in detached HEAD state.

    Fast path for commit-based sync:
    - start from a shallow fetch of the requested branch
    - if the commit is not reachable yet, deepen the same branch incrementally
    - only fall back to full history for that branch when needed

    This keeps recent commit checkouts close to the speed of the normal shallow
    branch workflow while still supporting older commits.
    """
    _logger.info(
        "checkout_commit: %s @ %s (branch=%s, fetch_all=%s, depth=%s)",
        dest, commit, branch, fetch_all, depth,
    )
    assert_clean_worktree(dest)

    if fetch_all:
        if depth is None:
            _ensure_full_origin_refspec(dest)
            _unshallow_if_needed(dest)
        _run(["git", "fetch", "--all", "--tags", "--prune"], cwd=dest)
    else:
        if branch:
            _fetch_branch(dest, branch=branch, depth=depth)

    if not _has_commit(dest, commit) and not fetch_all and branch and depth is not None:
        for deepen_by in (50, 200, 1000):
            _logger.info(
                "Commit %s not reachable yet on origin/%s; deepening shallow history by %s.",
                commit,
                branch,
                deepen_by,
            )
            _run([
                "git", "fetch", "--prune", "--deepen", str(deepen_by), "origin", branch,
            ], cwd=dest)
            if _has_commit(dest, commit):
                break

    if not _has_commit(dest, commit) and not fetch_all and branch:
        _logger.info(
            "Commit %s still not reachable on origin/%s; fetching full history for that branch.",
            commit,
            branch,
        )
        if _is_shallow_repo(dest):
            _run(["git", "fetch", "--unshallow", "--prune", "origin", branch], cwd=dest)
        else:
            _fetch_branch(dest, branch=branch, depth=None)

    if not _has_commit(dest, commit):
        try:
            fallback_cmd = ["git", "fetch", "--prune", "origin", commit]
            if depth is not None:
                fallback_cmd[2:2] = ["--depth", str(depth)]
            _run(fallback_cmd, cwd=dest)
        except Exception:
            pass

    _run(["git", "rev-parse", "--verify", f"{commit}^{{commit}}"], cwd=dest)
    _run(["git", "checkout", "--detach", commit], cwd=dest)
    _run(["git", "reset", "--hard", commit], cwd=dest)
    assert_clean_worktree(dest)


# -----------------------------
# Odoo config generation
# -----------------------------

def _format_conf_value(value: Any) -> str:
    # Render INI-parsed values into an Odoo .conf compatible scalar.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(_format_conf_value(v) for v in value)
    return str(value)


def _resolve_workspace_path(layout: Layout, raw_path: str, fallback: Path) -> Path:
    path_text = (raw_path or "").strip()
    if not path_text:
        return fallback

    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = layout.root / path
    try:
        return path.resolve()
    except Exception:
        return path.absolute()


def _validate_existing_dir(path: Path, label: str) -> Path:
    if not path.exists():
        raise Exception(f"{label} does not exist: {path}")
    if not path.is_dir():
        raise Exception(f"{label} is not a directory: {path}")
    return path


def _resolve_odoo_path(layout: Layout, spec: OdooSpec) -> Path:
    return _resolve_workspace_path(layout, spec.path or "", layout.odoo_dir)


def _validate_local_odoo_path(layout: Layout, spec: OdooSpec) -> Path:
    """Validate a local Odoo path lazily, right before it is used."""
    odoo_path = _resolve_odoo_path(layout, spec)
    if not spec.is_local:
        return odoo_path
    return _validate_existing_dir(odoo_path, "Local Odoo path for [odoo]")


def _resolve_addon_path(layout: Layout, addon_name: str, spec: AddonSpec) -> Path:
    return _resolve_workspace_path(layout, spec.path or "", layout.addons_root / addon_name)


def _validate_local_addon_path(layout: Layout, addon_name: str, spec: AddonSpec) -> Path:
    """Validate one local addon path lazily, right before that addon is processed."""
    addon_path = _resolve_addon_path(layout, addon_name, spec)
    if not spec.is_local:
        return addon_path
    return _validate_existing_dir(addon_path, f"Local addon path for [addons.{addon_name}]")


def render_odoo_conf(cfg: Dict[str, Any], layout: Layout, addon_paths: list[Path]) -> str:
    odoo_addons_candidates = [
        layout.odoo_dir / "addons",
        layout.odoo_dir / "odoo" / "addons",
    ]

    # addons_path is always generated from Odoo core addons + every configured [addons.<name>] source.
    merged: list[str] = []
    seen: set[str] = set()
    for p in [*odoo_addons_candidates, *addon_paths]:
        s = str(p)
        if s in seen:
            continue
        seen.add(s)
        merged.append(s)
    merged_addons_path = ",".join(merged)

    lines: list[str] = ["[options]"]

    # Write every key from [config] (dynamic; no fixed schema), except values generated by odt-env.
    for key, value in cfg.items():
        if key == "data_dir":
            continue
        lines.append(f"{key} = {_format_conf_value(value)}")

    # Always write generated addons_path.
    lines.append(f"addons_path = {merged_addons_path}")

    # Always write data_dir from layout
    lines.append(f"data_dir = {layout.data_dir}")

    return "\n".join(lines) + "\n"


# -----------------------------
# Script generation
# -----------------------------

def _script_odoo_bin_sh(layout: Layout) -> str:
    odoo_bin = layout.odoo_dir / "odoo-bin"
    try:
        rel = odoo_bin.resolve().relative_to(layout.root.resolve())
        return "${ROOT_DIR}/" + rel.as_posix()
    except Exception:
        return str(odoo_bin)


def _script_odoo_bin_bat(layout: Layout) -> str:
    odoo_bin = layout.odoo_dir / "odoo-bin"
    try:
        rel = odoo_bin.resolve().relative_to(layout.root.resolve())
        return "%ROOT_DIR%\\" + "\\".join(rel.parts)
    except Exception:
        return str(odoo_bin)


def _write_odoo_command_sh(layout: Layout, name: str, info_message: str, command: str) -> None:
    content = """#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

VENV_DIR="${ROOT_DIR}/venv"
PY="${VENV_DIR}/bin/python"
ODOO_BIN="__ODOO_BIN__"
CONF="${ROOT_DIR}/odoo-configs/odoo-server.conf"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "ERROR: required venv directory not found at ${VENV_DIR}" >&2
  exit 1
fi
if [[ ! -x "${PY}" ]]; then
  echo "ERROR: venv python not found/executable at ${PY}" >&2
  exit 1
fi
if [[ ! -f "${ODOO_BIN}" ]]; then
  echo "ERROR: odoo-bin not found at ${ODOO_BIN}" >&2
  exit 1
fi

echo "INFO: __INFO_MESSAGE__"
__COMMAND__
"""
    content = (
        content
        .replace("__ODOO_BIN__", _script_odoo_bin_sh(layout))
        .replace("__INFO_MESSAGE__", info_message)
        .replace("__COMMAND__", command)
    )
    _write_script(layout, name, "sh", content)


def _write_odoo_command_bat(layout: Layout, name: str, info_message: str, command: str) -> None:
    content = r"""@echo off
setlocal enabledelayedexpansion

REM Resolve ROOT directory (parent of this script directory)
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set SCRIPT_DIR=%SCRIPT_DIR:~0,-1%
for %%I in ("%SCRIPT_DIR%\..") do set ROOT_DIR=%%~fI

set VENV_DIR=%ROOT_DIR%\venv
set PY=%VENV_DIR%\Scripts\python.exe
set ODOO_BIN=__ODOO_BIN__
set CONF=%ROOT_DIR%\odoo-configs\odoo-server.conf

if not exist "%VENV_DIR%" (
  echo ERROR: required venv directory not found at %VENV_DIR%
  exit /b 1
)
if not exist "%PY%" (
  echo ERROR: venv python not found at %PY%
  exit /b 1
)
if not exist "%ODOO_BIN%" (
  echo ERROR: odoo-bin not found at %ODOO_BIN%
  exit /b 1
)

echo INFO: __INFO_MESSAGE__
__COMMAND__

endlocal
"""
    content = (
        content
        .replace("__ODOO_BIN__", _script_odoo_bin_bat(layout))
        .replace("__INFO_MESSAGE__", info_message)
        .replace("__COMMAND__", command)
    )
    _write_script(layout, name, "bat", content)


def write_run_sh(layout: Layout) -> None:
    _write_odoo_command_sh(
        layout,
        "run",
        "Starting Odoo server using config ${CONF}. Passing through any extra arguments.",
        'exec "${PY}" "${ODOO_BIN}" -c "${CONF}" "$@"',
    )


def write_instance_sh(layout: Layout) -> None:
    content = """#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

VENV_DIR="${ROOT_DIR}/venv"
PY="${VENV_DIR}/bin/python"
ODOO_BIN="__ODOO_BIN__"
CONF="${ROOT_DIR}/odoo-configs/odoo-server.conf"

LOGS_DIR="${ROOT_DIR}/odoo-logs"
LOG_FILE="${LOGS_DIR}/odoo-server.log"
PID_FILE="${LOGS_DIR}/odoo-server.pid"

require_paths() {
  if [[ ! -d "${VENV_DIR}" ]]; then
    echo "ERROR: required venv directory not found at ${VENV_DIR}" >&2
    exit 1
  fi
  if [[ ! -x "${PY}" ]]; then
    echo "ERROR: venv python not found/executable at ${PY}" >&2
    exit 1
  fi
  if [[ ! -f "${ODOO_BIN}" ]]; then
    echo "ERROR: odoo-bin not found at ${ODOO_BIN}" >&2
    exit 1
  fi
  if [[ ! -f "${CONF}" ]]; then
    echo "ERROR: Odoo config not found at ${CONF}" >&2
    exit 1
  fi
}

is_running() {
  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      echo "${pid}"
      return 0
    fi
  fi
  return 1
}

start() {
  mkdir -p "${LOGS_DIR}"
  require_paths

  local pid
  if pid="$(is_running)"; then
    echo "INFO: Odoo already running (PID=${pid})"
    return 0
  fi

  echo "----- $(date -Is) START -----" >> "${LOG_FILE}"
  nohup "${PY}" "${ODOO_BIN}" -c "${CONF}" "$@" >> "${LOG_FILE}" 2>&1 &

  pid=$!
  echo "${pid}" > "${PID_FILE}"
  echo "INFO: Started Odoo (PID=${pid}). Logging to ${LOG_FILE}"
}

stop() {
  mkdir -p "${LOGS_DIR}"

  local pid
  if pid="$(is_running)"; then
    echo "INFO: Stopping Odoo (PID=${pid})"
    kill "${pid}" 2>/dev/null || true

    # Wait up to ~30 seconds for a graceful shutdown
    for _ in {1..30}; do
      if kill -0 "${pid}" 2>/dev/null; then
        sleep 1
      else
        break
      fi
    done

    if kill -0 "${pid}" 2>/dev/null; then
      echo "WARN: Odoo did not stop gracefully; sending SIGKILL" >&2
      kill -9 "${pid}" 2>/dev/null || true
    fi

    rm -f "${PID_FILE}"
    echo "INFO: Stopped."
    return 0
  fi

  # Cleanup stale PID file (if any)
  rm -f "${PID_FILE}"
  echo "INFO: Odoo not running."
}

status() {
  local pid
  if pid="$(is_running)"; then
    # Requirement: print PID if running
    echo "${pid}"
    return 0
  fi
  echo "NOT RUNNING" >&2
  return 1
}

cmd="${1:-}"
shift || true

case "${cmd}" in
  start)
    start "$@"
    ;;
  stop)
    stop
    ;;
  restart)
    stop
    start "$@"
    ;;
  status)
    status
    ;;
  *)
    echo "Usage: $(basename "$0") {start|stop|restart|status} [odoo args...]" >&2
    exit 2
    ;;
esac
"""
    content = content.replace("__ODOO_BIN__", _script_odoo_bin_sh(layout))
    _write_script(layout, "instance", "sh", content)


def write_run_bat(layout: Layout) -> None:
    _write_odoo_command_bat(
        layout,
        "run",
        "Starting Odoo server using config %CONF%. Passing through any extra arguments.",
        '"%PY%" "%ODOO_BIN%" -c "%CONF%" %*',
    )


def write_test_sh(layout: Layout) -> None:
    _write_odoo_command_sh(
        layout,
        "test",
        "Running Odoo tests using config ${CONF}. Passing through any extra arguments.",
        'exec "${PY}" "${ODOO_BIN}" -c "${CONF}" --test-enable --stop-after-init "$@"',
    )


def write_test_bat(layout: Layout) -> None:
    _write_odoo_command_bat(
        layout,
        "test",
        "Running Odoo tests using config %CONF%. Passing through any extra arguments.",
        '"%PY%" "%ODOO_BIN%" -c "%CONF%" --test-enable --stop-after-init %*',
    )


def write_shell_sh(layout: Layout) -> None:
    _write_odoo_command_sh(
        layout,
        "shell",
        "Starting Odoo shell using config ${CONF}. Passing through any extra arguments.",
        'exec "${PY}" "${ODOO_BIN}" shell -c "${CONF}" "$@"',
    )


def write_shell_bat(layout: Layout) -> None:
    _write_odoo_command_bat(
        layout,
        "shell",
        "Starting Odoo shell using config %CONF%. Passing through any extra arguments.",
        '"%PY%" "%ODOO_BIN%" shell -c "%CONF%" %*',
    )


def write_backup_sh(layout: Layout, db_name: str) -> None:
    content = f"""#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
ROOT_DIR="$(cd "${{SCRIPT_DIR}}/.." && pwd)"

VENV_DIR="${{ROOT_DIR}}/venv"
BACKUPS_DIR="${{ROOT_DIR}}/odoo-backups"
BACKUP_BIN="${{VENV_DIR}}/bin/click-odoo-backupdb"
CONF="${{ROOT_DIR}}/odoo-configs/odoo-server.conf"

TODAY=$(date +%Y%m%d)
TIME=$(date +%H%M%S)
BACKUP_FILENAME="{db_name}_${{TODAY}}_${{TIME}}.zip"
FULL_BACKUP_PATH="${{BACKUPS_DIR}}/${{BACKUP_FILENAME}}"

if [[ ! -d "${{VENV_DIR}}" ]]; then
  echo "ERROR: required venv directory not found at ${{VENV_DIR}}" >&2
  exit 1
fi
if [[ ! -d "${{BACKUPS_DIR}}" ]]; then
  echo "ERROR: required odoo-backups directory not found at ${{BACKUPS_DIR}}" >&2
  exit 1
fi
if [[ ! -x "${{BACKUP_BIN}}" ]]; then
  echo "ERROR: click-odoo-backupdb not found/executable at ${{BACKUP_BIN}}" >&2
  exit 1
fi
if [[ ! -f "${{CONF}}" ]]; then
  echo "ERROR: Odoo config not found at ${{CONF}}" >&2
  exit 1
fi

echo "INFO: Creating new backup '${{FULL_BACKUP_PATH}}' using config ${{CONF}}. Passing through any extra arguments."
exec "${{BACKUP_BIN}}" -c "${{CONF}}" --format zip "{db_name}" "${{FULL_BACKUP_PATH}}" --log-level debug "$@"
"""
    _write_script(layout, "backup", "sh", content)


def write_backup_bat(layout: Layout, db_name: str) -> None:
    content = rf"""@echo off
setlocal enabledelayedexpansion

REM Resolve ROOT directory (parent of this script directory)
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set SCRIPT_DIR=%SCRIPT_DIR:~0,-1%
for %%I in ("%SCRIPT_DIR%\..") do set ROOT_DIR=%%~fI

set VENV_DIR=%ROOT_DIR%\venv
set BACKUPS_DIR=%ROOT_DIR%\odoo-backups
set BACKUP_BIN=%VENV_DIR%\Scripts\click-odoo-backupdb.exe
set CONF=%ROOT_DIR%\odoo-configs\odoo-server.conf

if not exist "%VENV_DIR%" (
  echo ERROR: required venv directory not found at %VENV_DIR%
  exit /b 1
)
if not exist "%BACKUPS_DIR%" (
  echo ERROR: required odoo-backups directory not found at %BACKUPS_DIR%
  exit /b 1
)
if not exist "%BACKUP_BIN%" (
  echo ERROR: click-odoo-backupdb not found at %BACKUP_BIN%
  exit /b 1
)
if not exist "%CONF%" (
  echo ERROR: Odoo config not found at %CONF%
  exit /b 1
)

REM Build timestamped filename (yyyyMMdd_HHmmss) via PowerShell for reliable formatting
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set TODAY=%%i
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format HHmmss"') do set TIME=%%i

set BACKUP_FILENAME={db_name}_%TODAY%_%TIME%.zip
set FULL_BACKUP_PATH=%BACKUPS_DIR%\%BACKUP_FILENAME%

echo INFO: Creating new backup "%FULL_BACKUP_PATH%" using config %CONF%. Passing through any extra arguments.
"%BACKUP_BIN%" -c "%CONF%" --format zip "{db_name}" "%FULL_BACKUP_PATH%" --log-level debug %*

endlocal
"""
    _write_script(layout, "backup", "bat", content)


def write_restore_sh(layout: Layout, db_name: str) -> None:
    content = f"""#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
ROOT_DIR="$(cd "${{SCRIPT_DIR}}/.." && pwd)"

VENV_DIR="${{ROOT_DIR}}/venv"
RESTORE_BIN="${{VENV_DIR}}/bin/click-odoo-restoredb"
CONF="${{ROOT_DIR}}/odoo-configs/odoo-server.conf"

if [[ ! -d "${{VENV_DIR}}" ]]; then
  echo "ERROR: required venv directory not found at ${{VENV_DIR}}" >&2
  exit 1
fi
if [[ ! -x "${{RESTORE_BIN}}" ]]; then
  echo "ERROR: click-odoo-restoredb not found/executable at ${{RESTORE_BIN}}" >&2
  exit 1
fi
if [[ ! -f "${{CONF}}" ]]; then
  echo "ERROR: Odoo config not found at ${{CONF}}" >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "ERROR: missing restore source (backup file/path). Provide it as the first argument." >&2
  echo "Example: ./restore.sh /path/to/backup.zip" >&2
  exit 2
fi

echo "INFO: Restoring Odoo database '{db_name}' using config ${{CONF}}. Passing through any extra arguments."
exec "${{RESTORE_BIN}}" -c "${{CONF}}" --copy --neutralize --log-level debug "{db_name}" "$@"
"""
    _write_script(layout, "restore", "sh", content)


def write_restore_bat(layout: Layout, db_name: str) -> None:
    content = rf"""@echo off
setlocal enabledelayedexpansion

REM Resolve ROOT directory (parent of this script directory)
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set SCRIPT_DIR=%SCRIPT_DIR:~0,-1%
for %%I in ("%SCRIPT_DIR%\..") do set ROOT_DIR=%%~fI

set VENV_DIR=%ROOT_DIR%\venv
set RESTORE_BIN=%VENV_DIR%\Scripts\click-odoo-restoredb.exe
set CONF=%ROOT_DIR%\odoo-configs\odoo-server.conf

if not exist "%VENV_DIR%" (
  echo ERROR: required venv directory not found at %VENV_DIR%
  exit /b 1
)
if not exist "%RESTORE_BIN%" (
  echo ERROR: click-odoo-restoredb not found at %RESTORE_BIN%
  exit /b 1
)
if not exist "%CONF%" (
  echo ERROR: Odoo config not found at %CONF%
  exit /b 1
)

if "%~1"=="" (
  echo ERROR: missing restore source ^(backup file/path^). Provide it as the first argument.
  echo Example: restore.bat C:\path\to\backup.zip
  exit /b 2
)

echo INFO: Restoring Odoo database "{db_name}" using config %CONF%. Passing through any extra arguments.
"%RESTORE_BIN%" -c "%CONF%" --copy --neutralize --log-level debug "{db_name}" %*

endlocal
"""
    _write_script(layout, "restore", "bat", content)


def write_update_sh(layout: Layout) -> None:
    content = f"""#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
ROOT_DIR="$(cd "${{SCRIPT_DIR}}/.." && pwd)"

VENV_DIR="${{ROOT_DIR}}/venv"
UPDATE_BIN="${{VENV_DIR}}/bin/click-odoo-update"
CONF="${{ROOT_DIR}}/odoo-configs/odoo-server.conf"

if [[ ! -d "${{VENV_DIR}}" ]]; then
  echo "ERROR: required venv directory not found at ${{VENV_DIR}}" >&2
  exit 1
fi
if [[ ! -x "${{UPDATE_BIN}}" ]]; then
  echo "ERROR: click-odoo-update not found/executable at ${{UPDATE_BIN}}" >&2
  exit 1
fi
if [[ ! -f "${{CONF}}" ]]; then
  echo "ERROR: Odoo config not found at ${{CONF}}" >&2
  exit 1
fi

echo "INFO: Updating Odoo addons using config ${{CONF}}. Passing through any extra arguments."
exec "${{UPDATE_BIN}}" -c "${{CONF}}" --log-level debug "$@"
"""
    _write_script(layout, "update", "sh", content)


def write_update_bat(layout: Layout) -> None:
    content = rf"""@echo off
setlocal enabledelayedexpansion

REM Resolve ROOT directory (parent of this script directory)
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set SCRIPT_DIR=%SCRIPT_DIR:~0,-1%
for %%I in ("%SCRIPT_DIR%\..") do set ROOT_DIR=%%~fI

set VENV_DIR=%ROOT_DIR%\venv
set UPDATE_BIN=%VENV_DIR%\Scripts\click-odoo-update.exe
set CONF=%ROOT_DIR%\odoo-configs\odoo-server.conf

if not exist "%VENV_DIR%" (
  echo ERROR: required venv directory not found at %VENV_DIR%
  exit /b 1
)
if not exist "%UPDATE_BIN%" (
  echo ERROR: click-odoo-update not found at %UPDATE_BIN%
  exit /b 1
)
if not exist "%CONF%" (
  echo ERROR: Odoo config not found at %CONF%
  exit /b 1
)

echo INFO: Updating Odoo addons using config %CONF%. Passing through any extra arguments.
"%UPDATE_BIN%" -c "%CONF%" --log-level debug %*

endlocal
"""
    _write_script(layout, "update", "bat", content)


# -----------------------------
# Docker artifact generation
# -----------------------------

def _docker_requirements_dir(layout: Layout) -> Path:
    return layout.docker_dir / "requirements"


def _docker_configs_dir(layout: Layout) -> Path:
    return layout.docker_dir / "configs"


def _docker_build_constraints_path(layout: Layout) -> Path:
    return _docker_requirements_dir(layout) / "build-constraints.txt"


def _docker_requirements_input_path(layout: Layout) -> Path:
    return _docker_requirements_dir(layout) / "addons-requirements.in.txt"


def _docker_requirements_lock_path(layout: Layout) -> Path:
    return _docker_requirements_dir(layout) / "addons-requirements.lock.txt"


def _dockerfile_path(layout: Layout) -> Path:
    return layout.docker_dir / "Dockerfile"


def _dockerignore_path(layout: Layout) -> Path:
    return layout.docker_dir / ".dockerignore"


def _docker_addons_dir(layout: Layout) -> Path:
    return layout.docker_dir / "addons"


def _docker_odoo_conf_path(layout: Layout) -> Path:
    return _docker_configs_dir(layout) / "odoo.conf"


def _docker_compose_path(layout: Layout) -> Path:
    return layout.root / "compose.yml"


def _docker_safe_addon_mount_name(addon_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", addon_name.strip()).strip("._-")
    return safe or "addon"


def _docker_addon_mount_name_map(cfg: ProjectConfig) -> dict[str, str]:
    used: set[str] = set()
    out: dict[str, str] = {}

    for addon_name in cfg.addons:
        base = _docker_safe_addon_mount_name(addon_name)
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}-{suffix}"
            suffix += 1

        used.add(candidate)
        out[addon_name] = candidate

    return out


def _docker_container_addons_path_for_addons_mode(cfg: ProjectConfig) -> str:
    if cfg.docker.addons_mode != _DOCKER_ADDONS_MODE_DEV or not cfg.addons:
        return str(_DOCKER_ADDONS_CONTAINER_ROOT)

    mount_names = _docker_addon_mount_name_map(cfg)
    return ",".join(
        str(_DOCKER_ADDONS_CONTAINER_ROOT / mount_names[addon_name])
        for addon_name in cfg.addons
    )


def _yaml_quote_scalar(value: str) -> str:
    return json.dumps(value)


def _compose_host_path(layout: Layout, path: Path) -> str:
    try:
        resolved_path = path.resolve()
        resolved_root = layout.root.resolve()
        rel = resolved_path.relative_to(resolved_root)
        rel_text = rel.as_posix()
        return "." if rel_text == "." else f"./{rel_text}"
    except Exception:
        return path.expanduser().absolute().as_posix()


def _has_active_requirements(lines: list[str]) -> bool:
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return True
    return False


def _docker_requirements_ignore(cfg: VirtualenvConfig) -> list[str]:
    ignore = list(cfg.explicit_requirements_ignore)
    for spec in cfg.explicit_requirements:
        name = _extract_req_name_from_spec(spec)
        if name and name not in ignore:
            ignore.append(name)
    return ignore


def compile_docker_addons_requirements_lock(
        python_version: str,
        workspace_root: Path,
        requirement_files: list[Path],
        explicit_requirements: list[str],
        requirements_ignore: list[str],
        output_lock_path: Path,
        requirements_dir: Path,
        build_constraints_path: Path,
) -> Path:
    """Compile an addon-only requirements lock for the generated Docker image."""
    _ensure_command("uv")
    requirements_dir.mkdir(parents=True, exist_ok=True)
    in_path = requirements_dir / "addons-requirements.in.txt"

    lines: list[str] = [
        "# This file is generated by odt-env (DO NOT EDIT).",
        "# Source: addon repository requirements plus odt-env Docker defaults and explicit [virtualenv].requirements.",
        "# Odoo core requirements are intentionally excluded for official Odoo Docker images.",
        "",
    ]
    _add_requirements_section(lines, "# --- odt-env Docker default requirements ---", _DEFAULT_DOCKER_REQUIREMENTS)
    _add_requirements_section(
        lines,
        "# --- explicit requirements from [virtualenv].requirements ---",
        explicit_requirements,
    )
    lines.extend(
        _collect_requirement_file_lines(
            workspace_root,
            requirement_files,
            _requirements_ignore_set(requirements_ignore),
        )
    )
    _write_requirements_input(in_path, lines)

    if not _has_active_requirements(lines):
        _write_text_file(
            output_lock_path,
            "# This file is generated by odt-env (DO NOT EDIT).\n"
            "# No addon Python requirements were collected.\n",
        )
        _logger.info("No Docker addon requirements collected; wrote empty lock marker: %s", output_lock_path)
        return output_lock_path

    cmd = [
        "uv", "pip", "compile",
        "--python-version", python_version,
        str(in_path),
        "-o", str(output_lock_path),
    ]
    _add_build_constraints(cmd, build_constraints_path)

    _logger.info(
        "Compiling Docker addon lock file with uv for Python %s: %s -> %s",
        python_version,
        in_path,
        output_lock_path,
    )
    _run_checked(
        cmd,
        cwd=workspace_root,
        err_msg=(
            "Failed to compile Docker addon requirements lock file.\n"
            f"Input: {in_path}\n"
            f"Output: {output_lock_path}"
        ),
    )
    return output_lock_path


def _is_odoo_module_dir(path: Path) -> bool:
    return path.is_dir() and (
        (path / "__manifest__.py").is_file()
        or (path / "__openerp__.py").is_file()
    )


def _iter_direct_odoo_modules(source_root: Path) -> list[Path]:
    if _is_odoo_module_dir(source_root):
        return [source_root]
    return sorted(
        [child for child in source_root.iterdir() if _is_odoo_module_dir(child)],
        key=lambda p: p.name.lower(),
    )


def stage_docker_addons(layout: Layout, cfg: ProjectConfig) -> int:
    """Stage concrete Odoo module directories for the Docker build context."""
    addons_dir = _docker_addons_dir(layout)
    if addons_dir.exists():
        _rmtree(addons_dir)
    addons_dir.mkdir(parents=True, exist_ok=True)

    staged_modules: dict[str, Path] = {}

    for addon_name, spec in cfg.addons.items():
        source_root = _validate_local_addon_path(layout, addon_name, spec)
        if not source_root.exists():
            raise Exception(
                f"Addon source for [addons.{addon_name}] does not exist: {source_root}. "
                "Run with --sync-addons/--sync-all first, or provide an existing local path."
            )
        if not source_root.is_dir():
            raise Exception(
                f"Addon source for [addons.{addon_name}] is not a directory: {source_root}"
            )

        module_dirs = _iter_direct_odoo_modules(source_root)
        if not module_dirs:
            _logger.warning(
                "No direct Odoo module directories found in addon source [%s]: %s",
                addon_name,
                source_root,
            )
            continue

        for module_dir in module_dirs:
            existing = staged_modules.get(module_dir.name)
            if existing is not None:
                raise Exception(
                    "Docker addon staging collision for module "
                    f"'{module_dir.name}': {existing} and {module_dir}. "
                    "Rename, remove, or split conflicting addon sources before generating the Docker build context."
                )

            dest = addons_dir / module_dir.name
            shutil.copytree(module_dir, dest, symlinks=True)
            staged_modules[module_dir.name] = module_dir

    if not staged_modules:
        (addons_dir / ".odt-env-empty").write_text(
            "No Odoo modules were staged for this Docker build context.\n",
            encoding="utf-8",
        )

    _logger.info("Staged %s Odoo addon module(s) for Docker build context.", len(staged_modules))
    return len(staged_modules)


def write_dockerignore(layout: Layout) -> Path:
    content = """# Generated by odt-env. Edit if your Docker build context needs different exclusions.
# Runtime configuration is bind-mounted by Docker Compose and must not be sent with the image build context.
configs/
# The lock file is needed by the Dockerfile, but the generated compile input is not.
requirements/addons-requirements.in.txt
**/__pycache__/
**/*.py[cod]
**/.pytest_cache/
**/.mypy_cache/
**/.ruff_cache/
"""
    path = _dockerignore_path(layout)
    path.write_text(content, encoding="utf-8")
    return path


def _get_optional_docker_host_port(cfg: Dict[str, Any], key: str) -> Optional[int]:
    raw = cfg.get(key)
    if raw is None or str(raw).strip() == "":
        return None

    try:
        port = int(str(raw).strip())
    except ValueError as e:
        raise Exception(
            f"Invalid option '{key}' in section [config] (expected TCP port number)."
        ) from e

    if not 1 <= port <= 65535:
        raise Exception(
            f"Invalid option '{key}' in section [config] (expected TCP port number between 1 and 65535)."
        )

    return port


def _render_docker_compose_ports(cfg: Dict[str, Any]) -> str:
    """Render host-side port publishing for the generated sample Docker Compose file.

    [config].http_port, [config].gevent_port and [config].longpolling_port are
    interpreted as host ports for Docker Compose only. The container keeps the
    standard Odoo Docker ports so the generated Docker Odoo config intentionally
    does not include these options.
    """
    host_http_port = (
        _get_optional_docker_host_port(cfg, "http_port")
        or _DEFAULT_DOCKER_HTTP_CONTAINER_PORT
    )
    ports = [
        f'      - "{host_http_port}:{_DEFAULT_DOCKER_HTTP_CONTAINER_PORT}"',
    ]

    host_gevent_port = _get_optional_docker_host_port(cfg, "gevent_port")
    host_longpolling_port = _get_optional_docker_host_port(cfg, "longpolling_port")

    if host_gevent_port is not None and host_longpolling_port is not None and host_gevent_port != host_longpolling_port:
        raise Exception(
            "Invalid [config] Docker port settings: 'gevent_port' and 'longpolling_port' "
            "both map to container port 8072 and must not have different values."
        )

    host_async_port = host_gevent_port if host_gevent_port is not None else host_longpolling_port
    if host_async_port is not None:
        ports.append(
            f'      - "{host_async_port}:{_DEFAULT_DOCKER_GEVENT_CONTAINER_PORT}"'
        )

    return "\n".join(ports)


def render_docker_odoo_conf(cfg: ProjectConfig) -> str:
    """Render a Docker-runtime Odoo config from optional project config values."""
    lines: list[str] = ["[options]"]

    # Keep project-specific options, but force the standard Odoo Docker paths and
    # ports. Port options from [config] are used as host-side Docker Compose
    # published ports only; Odoo inside the container keeps the official Docker
    # image defaults (8069 and 8072).
    for key, value in cfg.config.items():
        if key in {"addons_path", "data_dir", *_DOCKER_HOST_PORT_CONFIG_KEYS}:
            continue
        lines.append(f"{key} = {_format_conf_value(value)}")

    lines.append(f"addons_path = {_docker_container_addons_path_for_addons_mode(cfg)}")
    lines.append("data_dir = /var/lib/odoo")

    return "\n".join(lines) + "\n"


def write_docker_odoo_conf(layout: Layout, cfg: ProjectConfig) -> Path:
    path = _docker_odoo_conf_path(layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_docker_odoo_conf(cfg), encoding="utf-8")
    return path


def _render_docker_dev_addon_volumes(layout: Layout, cfg: ProjectConfig) -> list[str]:
    """Render bind mounts for addon sources in Docker dev mode."""
    mount_names = _docker_addon_mount_name_map(cfg)
    volumes: list[str] = []

    for addon_name, spec in cfg.addons.items():
        source_root = _validate_local_addon_path(layout, addon_name, spec)
        if not source_root.exists():
            raise Exception(
                f"Addon source for [addons.{addon_name}] does not exist: {source_root}. "
                "Run with --sync-addons/--sync-all first, or provide an existing local path."
            )
        if not source_root.is_dir():
            raise Exception(
                f"Addon source for [addons.{addon_name}] is not a directory: {source_root}"
            )

        volumes.extend([
            "      - type: bind",
            f"        source: {_yaml_quote_scalar(_compose_host_path(layout, source_root))}",
            f"        target: {_yaml_quote_scalar(str(_DOCKER_ADDONS_CONTAINER_ROOT / mount_names[addon_name]))}",
        ])

    return volumes


def _render_docker_compose_volumes(layout: Layout, cfg: ProjectConfig) -> str:
    volumes = [
        "      - ./odoo-docker/configs:/etc/odoo:ro",
    ]

    if cfg.docker.addons_mode == _DOCKER_ADDONS_MODE_DEV:
        volumes.extend(_render_docker_dev_addon_volumes(layout, cfg))

    volumes.append("      - odoo-data:/var/lib/odoo")
    return "\n".join(volumes)


def render_docker_compose(layout: Layout, image_name: str, cfg: ProjectConfig) -> str:
    """Render a sample Docker Compose file for the generated custom Odoo image."""
    docker = cfg.docker
    compose_project_name = f"name: {docker.compose_project_name}\n\n" if docker.compose_project_name else ""
    db_service = docker.db_service
    odoo_service = docker.odoo_service
    ports = _render_docker_compose_ports(cfg.config)
    volumes = _render_docker_compose_volumes(layout, cfg)

    return f"""# Generated by odt-env.
{compose_project_name}services:
  {db_service}:
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_DB: postgres
      POSTGRES_USER: odoo
      POSTGRES_PASSWORD: odoo
    volumes:
      - odoo-db-data:/var/lib/postgresql/data

  {odoo_service}:
    image: {image_name}
    restart: unless-stopped
    depends_on:
      - {db_service}
    ports:
{ports}
    environment:
      HOST: {db_service}
      PORT: 5432
      USER: odoo
      PASSWORD: odoo
    volumes:
{volumes}

volumes:
  odoo-db-data:
  odoo-data:
"""


def write_docker_compose(
        layout: Layout,
        image_name: str,
        cfg: ProjectConfig,
) -> Path:
    """Write ROOT/compose.yml, always overwriting an existing generated file."""
    path = _docker_compose_path(layout)

    if path.exists() and not path.is_file():
        raise Exception(f"Docker Compose path exists but is not a file: {path}")

    path.write_text(render_docker_compose(layout, image_name, cfg), encoding="utf-8")
    _logger.info("Generated Docker Compose file: %s", path)
    return path


def write_dockerfile(layout: Layout, cfg: ProjectConfig, has_build_constraints: bool) -> Path:
    build_constraints_copy = ""
    build_constraints_install = ""
    cleanup_constraints = ""
    if has_build_constraints:
        build_constraints_copy = "COPY requirements/build-constraints.txt /tmp/build-constraints.txt\n"
        build_constraints_install = " --build-constraints /tmp/build-constraints.txt"
        cleanup_constraints = " /tmp/build-constraints.txt"

    base_image = (cfg.docker.base_image or f"odoo:{cfg.odoo.version}").strip()
    addon_copy_step = ""
    if cfg.docker.addons_mode == _DOCKER_ADDONS_MODE_DEPLOY:
        addon_copy_step = """
COPY addons/ /mnt/extra-addons/
"""

    addon_chown_step = ""
    if cfg.docker.addons_mode == _DOCKER_ADDONS_MODE_DEPLOY:
        addon_chown_step = """

RUN chown -R odoo:odoo /mnt/extra-addons
"""

    content = f"""# Generated by odt-env.
# Review and edit this file before building when project-specific changes are needed.
FROM {base_image}

USER root
{addon_copy_step}
COPY requirements/addons-requirements.lock.txt /tmp/addons-requirements.lock.txt
{build_constraints_copy.rstrip()}

RUN PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install --no-cache-dir uv \\
 && if grep -Eq '^[[:space:]]*[^#[:space:]]' /tmp/addons-requirements.lock.txt; then \\
      uv pip install --system --break-system-packages --no-cache-dir{build_constraints_install} -r /tmp/addons-requirements.lock.txt; \\
    else \\
      echo "INFO: No addon Python requirements to install."; \\
    fi \\
 && rm -f /tmp/addons-requirements.lock.txt{cleanup_constraints}{addon_chown_step}
USER odoo
"""
    path = _dockerfile_path(layout)
    path.write_text(content, encoding="utf-8")
    return path


def create_docker_artifacts(
        layout: Layout,
        cfg: ProjectConfig,
        addon_requirement_files: list[Path],
) -> dict[str, Any]:
    """Generate a reviewable Docker build context under ROOT/odoo-docker/."""
    layout.docker_dir.mkdir(parents=True, exist_ok=True)
    _docker_requirements_dir(layout).mkdir(parents=True, exist_ok=True)
    _docker_configs_dir(layout).mkdir(parents=True, exist_ok=True)

    build_constraints_path = _docker_build_constraints_path(layout)
    if cfg.virtualenv.build_constraints:
        build_constraints_path.write_text(
            "\n".join(cfg.virtualenv.build_constraints).rstrip("\n") + "\n",
            encoding="utf-8",
        )
    elif build_constraints_path.exists():
        build_constraints_path.unlink()

    lock_path = _docker_requirements_lock_path(layout)
    compile_docker_addons_requirements_lock(
        python_version=cfg.virtualenv.python_version,
        workspace_root=layout.root,
        requirement_files=addon_requirement_files,
        explicit_requirements=cfg.virtualenv.explicit_requirements,
        requirements_ignore=_docker_requirements_ignore(cfg.virtualenv),
        output_lock_path=lock_path,
        requirements_dir=_docker_requirements_dir(layout),
        build_constraints_path=build_constraints_path,
    )

    staged_module_count = 0
    if cfg.docker.addons_mode == _DOCKER_ADDONS_MODE_DEPLOY:
        staged_module_count = stage_docker_addons(layout, cfg)
    else:
        addons_dir = _docker_addons_dir(layout)
        if addons_dir.exists():
            _logger.info("Removing stale Docker staged addons for dev mode: %s", addons_dir)
            _rmtree(addons_dir)

    dockerignore_path = write_dockerignore(layout)
    docker_conf_path = write_docker_odoo_conf(layout, cfg)
    dockerfile_path = write_dockerfile(
        layout,
        cfg,
        has_build_constraints=build_constraints_path.is_file(),
    )

    _logger.info("Generated Docker build context: %s", layout.docker_dir)
    _logger.info("Generated Dockerfile: %s", dockerfile_path)
    _logger.info("Generated Docker Odoo config: %s", docker_conf_path)

    return {
        "docker_dir": layout.docker_dir,
        "dockerfile": dockerfile_path,
        "dockerignore": dockerignore_path,
        "odoo_config": docker_conf_path,
        "requirements_input": _docker_requirements_input_path(layout),
        "requirements_lock": lock_path,
        "build_constraints": build_constraints_path if build_constraints_path.is_file() else None,
        "staged_module_count": staged_module_count,
    }


def build_docker_image(layout: Layout, image_name: str, docker_addons_mode: str = _DEFAULT_DOCKER_ADDONS_MODE) -> str:
    """Build a Docker image from ROOT/odoo-docker/ build context."""
    dockerfile_path = _dockerfile_path(layout)
    lock_path = _docker_requirements_lock_path(layout)
    addons_dir = _docker_addons_dir(layout)

    if not dockerfile_path.is_file():
        raise Exception(
            f"Dockerfile not found: {dockerfile_path}. "
            "Generate Docker artifacts before building the image."
        )
    if not lock_path.is_file():
        raise Exception(
            f"Docker addon requirements lock not found: {lock_path}. "
            "Generate Docker artifacts before building the image."
        )
    if docker_addons_mode == _DOCKER_ADDONS_MODE_DEPLOY and not addons_dir.is_dir():
        raise Exception(
            f"Docker addon staging directory not found: {addons_dir}. "
            "Generate Docker artifacts before building the image."
        )
    _ensure_command("docker")

    cmd = [
        "docker", "build",
        "-f", str(dockerfile_path),
        "-t", image_name,
        str(layout.docker_dir),
    ]
    _logger.info("Building Docker image: %s", image_name)
    _run_checked(cmd, cwd=layout.root, err_msg="Failed to build Docker image.")
    return image_name


# -----------------------------
# Main logic
# -----------------------------

def _sync_project_impl(
        ini_path: Path,
        sync_odoo: bool,
        sync_addons: bool,
        root_override: Optional[Path] = None,
        reuse_wheelhouse: bool = False,
        create_venv: bool = False,
        clear_pip_wheel_cache: bool = False,
        no_configs: bool = False,
        no_scripts: bool = False,
        no_data_dir: bool = False,
        build_docker_image_requested: bool = False,
        vars_overrides: Optional[Dict[str, str]] = None,
        ini_overrides: Optional[Dict[str, Dict[str, str]]] = None,
) -> None:
    root = (root_override or ini_path.parent).resolve()
    if root.exists() and not root.is_dir():
        raise Exception(f"ROOT exists but is not a directory: {root}")
    layout = Layout.from_root(root)

    cfg = load_project_config(
        ini_path,
        vars_overrides=vars_overrides,
        ini_overrides=ini_overrides,
    )

    # If user overrides "data_dir" via [config] section, propagate changes to layout->data_dir.
    # An empty value means "use the default layout data directory". This keeps template INI
    # files override-friendly for --set config:data_dir=... without accidentally resolving
    # an empty value to ROOT.
    if "data_dir" in cfg.config:
        cfg_data_dir_raw = (cfg.config.get("data_dir") or "").strip()
        if cfg_data_dir_raw:
            cfg_data_dir_path = Path(cfg_data_dir_raw).expanduser()
            if not cfg_data_dir_path.is_absolute():
                cfg_data_dir_path = layout.root / cfg_data_dir_path
            try:
                cfg_data_dir = cfg_data_dir_path.resolve()
            except Exception:
                cfg_data_dir = cfg_data_dir_path.absolute()
            _logger.warning(f"data_dir override via [config] section: from={layout.data_dir}, to={cfg_data_dir}")
            layout = replace(layout, data_dir=cfg_data_dir)
        else:
            _logger.info("Ignoring empty [config].data_dir; using default: %s", layout.data_dir)

    # A local [odoo].path replaces the default ROOT/odoo location everywhere:
    # requirements, editable install, generated configs, helper scripts, and status output.
    if cfg.odoo.is_local:
        local_odoo_dir = _resolve_odoo_path(layout, cfg.odoo)
        _logger.info("Using local Odoo source directory from [odoo].path: %s", local_odoo_dir)
        layout = replace(layout, odoo_dir=local_odoo_dir)

    docker_requested = build_docker_image_requested

    # We optionally create/ensure the venv early so we can use its Python for `uv pip compile` / installs.
    venv_py: Optional[Path] = None

    if create_venv:
        venv_dir = layout.root / "venv"

        if venv_dir.exists():
            _logger.info("Recreating venv: removing %s", venv_dir)
            _rmtree(venv_dir)

        # Wheelhouse handling: either reuse, or rebuild from scratch.
        if reuse_wheelhouse:
            if not layout.wheelhouse_dir.exists() or not layout.wheelhouse_dir.is_dir():
                raise Exception(f"--create-venv-from-wheelhouse set but wheelhouse dir not found: {layout.wheelhouse_dir}")
        else:
            if layout.wheelhouse_dir.exists():
                _logger.info("Rebuilding wheelhouse: removing %s", layout.wheelhouse_dir)
                _rmtree(layout.wheelhouse_dir)
            layout.wheelhouse_dir.mkdir(parents=True, exist_ok=True)

        # Create venv (requirements are installed later from a single lock file).
        require_venv(
            layout=layout,
            python_version=cfg.virtualenv.python_version,
            reuse_wheelhouse=reuse_wheelhouse,
            managed_python=cfg.virtualenv.managed_python,
        )
        venv_py = venv_dir / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
        if not venv_py.exists():
            raise Exception(f"venv python not found at expected path: {venv_py}")

        if reuse_wheelhouse and (sync_odoo or sync_addons):
            _logger.warning(
                "--create-venv-from-wheelhouse is set together with repo sync targets; "
                "dependency lock/wheelhouse rebuild will be skipped. "
                "If requirements changed, re-run without --create-venv-from-wheelhouse."
            )
    else:
        if sync_odoo or sync_addons:
            _logger.info(
                "Repo sync selected, but venv/wheelhouse provisioning is disabled; "
                "skipping venv/wheelhouse. Use --create-venv to enable."
            )
        else:
            if docker_requested:
                _logger.info(
                    "No repository sync target selected; Docker artifacts will use already available addon sources."
                )
            else:
                _logger.info(
                    "No sync target selected; regenerating config and helper scripts only (skipping venv/repo operations)."
                )

    layout.configs_dir.mkdir(parents=True, exist_ok=True)
    layout.addons_root.mkdir(parents=True, exist_ok=True)
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.backups_dir.mkdir(parents=True, exist_ok=True)
    if not no_data_dir:
        layout.data_dir.mkdir(parents=True, exist_ok=True)

    # Sync repositories first, collect all requirements, then compile + install once.
    req_files: list[Path] = []
    addon_req_files: list[Path] = []

    if sync_odoo:
        odoo_dest = _validate_local_odoo_path(layout, cfg.odoo)

        if cfg.odoo.is_local:
            _logger.info(
                "Using local Odoo path for [odoo]: %s (skipping git sync)",
                odoo_dest,
            )
        elif cfg.odoo.commit:
            _logger.info(
                "Syncing Odoo repo at commit %s (branch=%s) using shallow fetch with incremental deepen fallback.",
                cfg.odoo.commit,
                cfg.odoo.branch,
            )
            ensure_repo(
                cfg.odoo.repo,
                odoo_dest,
                branch=cfg.odoo.branch,
                depth=1,
                single_branch=True,
                fetch_all=False,
            )
            checkout_commit(
                odoo_dest,
                cfg.odoo.commit,
                branch=cfg.odoo.branch,
                fetch_all=False,
                depth=1,
            )
        elif cfg.odoo.shallow:
            # Shallow + single branch (default; disable with [odoo] shallow=false).
            ensure_repo(
                cfg.odoo.repo,
                odoo_dest,
                branch=cfg.odoo.branch,
                depth=1,
                single_branch=True,
                fetch_all=False,
            )
            checkout_branch(odoo_dest, cfg.odoo.branch, fetch_all=False, depth=1)
        else:
            # Full clone/fetch.
            ensure_repo(
                cfg.odoo.repo,
                odoo_dest,
                branch=cfg.odoo.branch,
                depth=None,
                single_branch=False,
                fetch_all=True,
            )
            checkout_branch(odoo_dest, cfg.odoo.branch, fetch_all=True, depth=None)

        odoo_req = odoo_dest / "requirements.txt"
        if not odoo_req.exists():
            raise Exception(f"Odoo requirements file not found: {odoo_req}")
        req_files.append(odoo_req)
    else:
        # If we're provisioning python but not syncing repos, use whatever is already present in the workspace.
        if venv_py is not None:
            odoo_dest = _validate_local_odoo_path(layout, cfg.odoo)
            odoo_req = odoo_dest / "requirements.txt"
            if odoo_req.exists():
                req_files.append(odoo_req)

    if sync_addons:
        if not cfg.addons:
            _logger.info("No [addons.*] sections configured; skipping addons sync.")
        for addon_name, spec in cfg.addons.items():
            dest = _validate_local_addon_path(layout, addon_name, spec)

            if spec.is_local:
                _logger.info(
                    "Using local addon path for [%s]: %s (skipping git sync)",
                    addon_name,
                    dest,
                )
            elif spec.commit:
                _logger.info(
                    "Syncing addon [%s] at commit %s (branch=%s) using shallow fetch with incremental deepen fallback.",
                    addon_name,
                    spec.commit,
                    spec.branch,
                )
                ensure_repo(
                    spec.repo,
                    dest,
                    branch=spec.branch,
                    depth=1,
                    single_branch=True,
                    fetch_all=False,
                )
                checkout_commit(
                    dest,
                    spec.commit,
                    branch=spec.branch,
                    fetch_all=False,
                    depth=1,
                )
            elif spec.shallow:
                # Shallow + single branch (default; disable with [addons.<name>] shallow=false).
                ensure_repo(
                    spec.repo,
                    dest,
                    branch=spec.branch,
                    depth=1,
                    single_branch=True,
                    fetch_all=False,
                )
                checkout_branch(dest, spec.branch, fetch_all=False, depth=1)
            else:
                # Full clone/fetch.
                ensure_repo(
                    spec.repo,
                    dest,
                    branch=spec.branch,
                    depth=None,
                    single_branch=False,
                    fetch_all=True,
                )
                checkout_branch(dest, spec.branch, fetch_all=True, depth=None)

            addon_req = dest / "requirements.txt"
            if addon_req.exists():
                req_files.append(addon_req)
                addon_req_files.append(addon_req)
    else:
        # If we're provisioning Python or generating Docker artifacts, use existing addon requirements (if present).
        if (venv_py is not None or docker_requested) and cfg.addons:
            for addon_name, spec in cfg.addons.items():
                dest = _validate_local_addon_path(layout, addon_name, spec)
                addon_req = dest / "requirements.txt"
                if addon_req.exists():
                    if venv_py is not None:
                        req_files.append(addon_req)
                    addon_req_files.append(addon_req)

    # Compile and install a single lock file from all synced repos + base requirements.
    # In --create-venv-from-wheelhouse mode we skip compilation + wheel build and only install offline from existing lock/wheels.
    if venv_py is not None:
        if cfg.odoo.is_local:
            _validate_local_odoo_path(layout, cfg.odoo)

        # The generated scripts assume the resolved Odoo source directory exists.
        if not layout.odoo_dir.exists() or not layout.odoo_dir.is_dir():
            raise Exception(
                f"Odoo directory not found: {layout.odoo_dir}. "
                "Run with --sync-odoo/--sync-all first (or ensure ROOT/odoo exists)."
            )

        lock_path = layout.wheelhouse_dir / "all-requirements.lock.txt"
        build_constraints_path = layout.wheelhouse_dir / "build-constraints.txt"

        if reuse_wheelhouse:
            # Reuse existing wheelhouse (offline-only mode)
            if not layout.wheelhouse_dir.exists() or not layout.wheelhouse_dir.is_dir():
                raise Exception(f"Wheelhouse directory not found: {layout.wheelhouse_dir}")
            if not any(layout.wheelhouse_dir.glob("*.whl")):
                raise Exception(f"Wheelhouse looks empty (no .whl files): {layout.wheelhouse_dir}")

            if not lock_path.exists():
                raise Exception(
                    f"--create-venv-from-wheelhouse set but lock file not found: {lock_path} "
                    "(expected existing wheelhouse from a previous run)"
                )

            if cfg.virtualenv.build_constraints and not build_constraints_path.is_file():
                raise Exception(
                    f"--create-venv-from-wheelhouse and build_constraints set in INI but build_constraints file not found: {build_constraints_path} "
                    "(expected existing wheelhouse from a previous run)"
                )

            pip_install_requirements_file(
                venv_python=venv_py,
                workspace_root=layout.root,
                wheelhouse_dir=layout.wheelhouse_dir,
                requirements_path=lock_path,
            )
        else:
            # Write build constraints to file
            if cfg.virtualenv.build_constraints:
                build_constraints_path.write_text(
                    "\n".join(cfg.virtualenv.build_constraints).rstrip("\n") + "\n", encoding="utf-8")

            # We need Odoo requirements to produce a correct lock.
            odoo_req = layout.odoo_dir / "requirements.txt"
            if not odoo_req.exists():
                raise Exception(f"Odoo requirements file not found: {odoo_req}")

            base_requirements = list(_DEFAULT_REQUIREMENTS)
            if cfg.virtualenv.requirements:
                base_requirements.extend(cfg.virtualenv.requirements)

            compile_all_requirements_lock(
                venv_python=venv_py,
                workspace_root=layout.root,
                wheelhouse_dir=layout.wheelhouse_dir,
                requirement_files=req_files,
                base_requirements=base_requirements,
                requirements_ignore=cfg.virtualenv.requirements_ignore,
                output_lock_path=lock_path,
                build_constraints_path=build_constraints_path,
            )

            build_wheelhouse_from_requirements(
                venv_python=venv_py,
                workspace_root=layout.root,
                requirements_path=lock_path,
                wheelhouse_dir=layout.wheelhouse_dir,
                build_constraints_path=build_constraints_path,
                clear_pip_wheel_cache=clear_pip_wheel_cache,
            )

            if create_venv:
                pip_install_requirements_file(
                    venv_python=venv_py,
                    workspace_root=layout.root,
                    wheelhouse_dir=layout.wheelhouse_dir,
                    requirements_path=lock_path,
                )

        if create_venv:
            # Install Odoo itself in editable mode (so local source changes are reflected).
            _logger.info("Installing Odoo in editable mode: %s", layout.odoo_dir)
            cmd = [
                str(venv_py), "-m", "pip", "install",
                "--no-deps",
                "--no-build-isolation",
                "-e", str(layout.odoo_dir),
                # Legacy editable install of odoo
                "--config-settings", "editable_mode=compat",
            ]
            _run_checked(
                cmd,
                cwd=layout.root,
                err_msg="Failed to install Odoo in editable mode.",
            )

    docker_artifacts: Optional[dict[str, Any]] = None
    docker_image_built: Optional[str] = None

    if build_docker_image_requested:
        target_image = (cfg.docker.target_image or "").strip()
        if not target_image:
            raise Exception(
                "Missing Docker target image. Set option 'target_image' in section [docker]."
            )

        _logger.info("Generating Docker artifacts before building the image.")
        docker_artifacts = create_docker_artifacts(
            layout=layout,
            cfg=cfg,
            addon_requirement_files=addon_req_files,
        )
        write_docker_compose(
            layout=layout,
            image_name=target_image,
            cfg=cfg,
        )
        docker_image_built = build_docker_image(layout, target_image, docker_addons_mode=cfg.docker.addons_mode)

    # Generate config (unless disabled).
    if not no_configs:
        addon_paths: list[Path] = [
            _validate_local_addon_path(layout, addon_name, spec)
            for addon_name, spec in cfg.addons.items()
        ]
        conf_text = render_odoo_conf(cfg.config, layout, addon_paths)
        layout.conf_path.write_text(conf_text, encoding="utf-8")
    else:
        _logger.info("Skipping config generation (--no-configs).")

    is_windows = sys.platform.startswith("win")

    # Generate helper scripts (unless disabled).
    if not no_scripts:
        if is_windows:
            write_run_bat(layout)
            write_test_bat(layout)
            write_shell_bat(layout)
            write_update_bat(layout)
        else:
            write_run_sh(layout)
            write_instance_sh(layout)
            write_test_sh(layout)
            write_shell_sh(layout)
            write_update_sh(layout)

        db_name = cfg.config.get("db_name")
        if not isinstance(db_name, str) or not db_name.strip():
            _logger.warning(
                "Missing or invalid '[config].db_name' (expected non-empty string). "
                "Database scripts (backup/restore/restore-force) will NOT be generated."
            )
        else:
            if is_windows:
                write_backup_bat(layout, db_name.strip())
                write_restore_bat(layout, db_name.strip())
            else:
                write_backup_sh(layout, db_name.strip())
                write_restore_sh(layout, db_name.strip())
    else:
        _logger.info("Skipping script generation (--no-scripts).")

    synced: list[str] = []
    if sync_odoo:
        synced.append("odoo")
    if sync_addons:
        synced.append("addons")

    _logger.info("OK")
    if synced:
        synced_label = ", ".join(synced)
    else:
        synced_label = "none"

    generated: list[str] = []
    if not no_configs:
        generated.append("configs")
    if not no_scripts:
        generated.append("scripts")
    if generated:
        synced_label = f"{synced_label} (generated: {', '.join(generated)})"
    else:
        synced_label = f"{synced_label} (no configs and scripts generated)"

    _logger.info(f"  Synced:             {synced_label}")
    _logger.info(f"  ROOT:               {layout.root}")
    _logger.info(f"  Odoo:               {layout.odoo_dir}")
    _logger.info(f"  Addons:             {layout.addons_root}")
    _logger.info(f"  Backups:            {layout.backups_dir}")
    if no_data_dir:
        _logger.info(f"  Data:               SKIPPED (--no-data-dir)")
    else:
        _logger.info(f"  Data:               {layout.data_dir}")
    if no_configs:
        _logger.info(f"  Config:             SKIPPED (--no-configs) [{layout.conf_path}]")
    else:
        _logger.info(f"  Config:             {layout.conf_path}")
    if venv_py is not None:
        _logger.info(f"  Venv:               {layout.root / 'venv'}")
        lock_path = layout.wheelhouse_dir / "all-requirements.lock.txt"
        if lock_path.exists():
            _logger.info(f"  Requirements:       {lock_path}")
            _logger.info(f"  Wheelhouse:         {layout.wheelhouse_dir}")
        if cfg.virtualenv.build_constraints:
            bc = layout.wheelhouse_dir / "build-constraints.txt"
            if bc.exists():
                _logger.info(f"  Build Constraints:  {bc}")

    if docker_artifacts is not None or build_docker_image_requested:
        _logger.info(f"  Docker Addons Mode:        {cfg.docker.addons_mode}")
        _logger.info(f"  Docker Context:     {layout.docker_dir}")
        _logger.info(f"  Dockerfile:         {_dockerfile_path(layout)}")
        docker_conf_path = _docker_odoo_conf_path(layout)
        if docker_conf_path.exists():
            _logger.info(f"  Docker Config:      {docker_conf_path}")
        docker_compose_path = _docker_compose_path(layout)
        if docker_compose_path.exists():
            _logger.info(f"  Docker Compose:     {docker_compose_path}")
        docker_lock_path = _docker_requirements_lock_path(layout)
        if docker_lock_path.exists():
            _logger.info(f"  Docker Requirements: {docker_lock_path}")
        if docker_artifacts is not None:
            staged_modules = docker_artifacts.get("staged_module_count", 0)
            _logger.info(f"  Docker Addons:      {staged_modules} staged module(s)")
        if docker_image_built is not None:
            _logger.info(f"  Docker Image:       {docker_image_built}")

    if no_scripts:
        _logger.info("  Scripts:            SKIPPED (--no-scripts)")
    else:
        _logger.info(f"  Scripts:")
        if is_windows:
            _logger.info(f"  - run:              {layout.script("run", "bat")}")
            _logger.info(f"  - test:             {layout.script("test", "bat")}")
            _logger.info(f"  - shell:            {layout.script("shell", "bat")}")
            _logger.info(f"  - backup:           {layout.script("backup", "bat")}")
            _logger.info(f"  - restore:          {layout.script("restore", "bat")}")
            _logger.info(f"  - update:           {layout.script("update", "bat")}")
        else:
            _logger.info(f"  - run:              {layout.script("run", "sh")}")
            _logger.info(f"  - test:             {layout.script("test", "sh")}")
            _logger.info(f"  - shell:            {layout.script("shell", "sh")}")
            _logger.info(f"  - backup:           {layout.script("backup", "sh")}")
            _logger.info(f"  - restore:          {layout.script("restore", "sh")}")
            _logger.info(f"  - update:           {layout.script("update", "sh")}")


def sync_project(
        ini_path: Path,
        sync_odoo: bool,
        sync_addons: bool,
        root_override: Optional[Path] = None,
        reuse_wheelhouse: bool = False,
        create_venv: bool = False,
        clear_pip_wheel_cache: bool = False,
        no_configs: bool = False,
        no_scripts: bool = False,
        no_data_dir: bool = False,
        build_docker_image_requested: bool = False,
        vars_overrides: Optional[Dict[str, str]] = None,
        ini_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        no_provisioning_log: bool = False,
        cli_argv: Optional[list[str]] = None,
) -> None:
    if no_provisioning_log:
        _sync_project_impl(
            ini_path=ini_path,
            sync_odoo=sync_odoo,
            sync_addons=sync_addons,
            root_override=root_override,
            reuse_wheelhouse=reuse_wheelhouse,
            create_venv=create_venv,
            clear_pip_wheel_cache=clear_pip_wheel_cache,
            no_configs=no_configs,
            no_scripts=no_scripts,
            no_data_dir=no_data_dir,
            build_docker_image_requested=build_docker_image_requested,
            vars_overrides=vars_overrides,
            ini_overrides=ini_overrides,
        )
        return

    root = (root_override or ini_path.parent).resolve()
    layout = Layout.from_root(root)
    run_id = _safe_run_id()
    started_at = _utc_now_iso()
    argv = _redact_cli_args(cli_argv or sys.argv)
    run_path, source_ini_path, resolved_ini_path, last_path = _provisioning_paths(layout, run_id)

    record: dict[str, Any] = {
        "version": f"odt-env-{__version__}",
        "run_id": run_id,
        "status": "started",
        "started_at": started_at,
        "finished_at": None,
        "host": socket.gethostname(),
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "cwd": str(Path.cwd()),
        "argv": argv,
        "command": shlex.join(argv),
        "workspace": {
            "root": str(layout.root),
            "ini_path": str(ini_path),
        },
        "options": {
            "sync_odoo": sync_odoo,
            "sync_addons": sync_addons,
            "reuse_wheelhouse": reuse_wheelhouse,
            "create_venv": create_venv,
            "clear_pip_wheel_cache": clear_pip_wheel_cache,
            "no_configs": no_configs,
            "no_scripts": no_scripts,
            "no_data_dir": no_data_dir,
            "build_docker_image": build_docker_image_requested,
        },
        "vars_overrides": _redact_mapping(vars_overrides or {}),
        "ini_overrides": _redact_mapping(ini_overrides or {}),
        "config": None,
        "artifacts": {
            "manifest": str(run_path),
            "last_manifest": str(last_path),
            "source_ini": None,
            "resolved_ini": None,
        },
        "error": None,
    }

    _write_provisioning_record(layout, run_id, record)

    copied_source_ini_path = _copy_source_ini_for_manifest(layout, run_id, ini_path)
    if copied_source_ini_path is not None:
        source_ini_path = copied_source_ini_path
        record["artifacts"]["source_ini"] = str(source_ini_path)
        _write_provisioning_record(layout, run_id, record)

    resolved_ini = _resolved_ini_for_manifest(
        ini_path,
        vars_overrides=vars_overrides,
        ini_overrides=ini_overrides,
    )
    if resolved_ini is not None:
        resolved_ini_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_ini_path.write_text(resolved_ini, encoding="utf-8")
        if not sys.platform.startswith("win"):
            try:
                resolved_ini_path.chmod(0o600)
            except OSError:
                pass
        record["artifacts"]["resolved_ini"] = str(resolved_ini_path)
        _write_provisioning_record(layout, run_id, record)

    try:
        _sync_project_impl(
            ini_path=ini_path,
            sync_odoo=sync_odoo,
            sync_addons=sync_addons,
            root_override=root_override,
            reuse_wheelhouse=reuse_wheelhouse,
            create_venv=create_venv,
            clear_pip_wheel_cache=clear_pip_wheel_cache,
            no_configs=no_configs,
            no_scripts=no_scripts,
            no_data_dir=no_data_dir,
            build_docker_image_requested=build_docker_image_requested,
            vars_overrides=vars_overrides,
            ini_overrides=ini_overrides,
        )
    except Exception as e:
        record["status"] = "failed"
        record["finished_at"] = _utc_now_iso()
        record["error"] = {
            "type": type(e).__name__,
            "message": str(e),
        }
        try:
            cfg = load_project_config(
                ini_path,
                vars_overrides=vars_overrides,
                ini_overrides=ini_overrides,
                log_resolved=False,
                log_overrides=False,
            )
            details = _collect_provisioning_details(cfg, source_ini_path, resolved_ini_path)
            record["config"] = details["config"]
            record["artifacts"].update(details["artifacts"])
        except Exception:
            pass
        _write_provisioning_record(layout, run_id, record)
        raise

    cfg = load_project_config(
        ini_path,
        vars_overrides=vars_overrides,
        ini_overrides=ini_overrides,
        log_resolved=False,
        log_overrides=False,
    )
    details = _collect_provisioning_details(cfg, source_ini_path, resolved_ini_path)
    record["config"] = details["config"]
    record["artifacts"].update(details["artifacts"])
    record["status"] = "success"
    record["finished_at"] = _utc_now_iso()
    _write_provisioning_record(layout, run_id, record)
    _logger.info("  Provisioning log:   %s", last_path)


# -----------------------------
# CLI
# -----------------------------

def _parse_git_ini_source(raw_ini: str) -> Optional[GitIniSource]:
    """Parse a Git-backed remote INI source.

    Supported syntax:
      git::REPO_URL//PATH/TO/PROJECT.ini?ref=BRANCH_OR_TAG_OR_COMMIT

    The path after ``//`` is a repository-internal path and therefore uses
    POSIX separators on every operating system.
    """
    raw_value = (raw_ini or "").strip()
    if not raw_value.startswith(_GIT_INI_PREFIX):
        return None

    source_value = raw_value[len(_GIT_INI_PREFIX):].strip()
    if not source_value:
        raise Exception(
            "Invalid Git INI source: missing repository URL after 'git::'."
        )

    source_without_query, query_sep, query = source_value.partition("?")
    ref: Optional[str] = None
    if query_sep:
        query_values = parse_qs(query, keep_blank_values=True, strict_parsing=False)
        unsupported = sorted(key for key in query_values if key != "ref")
        if unsupported:
            raise Exception(
                "Invalid Git INI source: unsupported query parameter(s): "
                f"{', '.join(unsupported)}. Only 'ref' is supported."
            )

        ref_values = query_values.get("ref", [])
        if len(ref_values) > 1:
            raise Exception(
                "Invalid Git INI source: query parameter 'ref' may be specified only once."
            )
        if ref_values:
            ref = ref_values[0].strip()
            if not ref:
                raise Exception(
                    "Invalid Git INI source: query parameter 'ref' must not be empty."
                )

    if "//" not in source_without_query:
        raise Exception(
            "Invalid Git INI source: expected 'git::REPO_URL//PATH/TO/PROJECT.ini'."
        )

    repo, raw_path = source_without_query.rsplit("//", 1)
    repo = repo.strip()
    raw_path = raw_path.strip()

    # ``https://...`` without the repository-internal ``//path`` would otherwise
    # split at the URL scheme separator and look superficially valid.
    if not repo or repo.endswith(":") or not raw_path:
        raise Exception(
            "Invalid Git INI source: expected 'git::REPO_URL//PATH/TO/PROJECT.ini'."
        )

    if "\\" in raw_path:
        raise Exception(
            "Invalid Git INI source path: use '/' separators inside the Git repository, not '\\'."
        )

    repo_path = PurePosixPath(raw_path)
    if repo_path.is_absolute():
        raise Exception(
            "Invalid Git INI source path: repository-internal INI path must be relative."
        )
    if not repo_path.parts or any(part in (".", "..") for part in repo_path.parts):
        raise Exception(
            "Invalid Git INI source path: repository-internal INI path must not contain '.' or '..'."
        )
    if not repo_path.name:
        raise Exception(
            "Invalid Git INI source path: expected a file path, not a directory path."
        )

    return GitIniSource(repo=repo, path=repo_path, ref=ref)


def _clone_git_ini_repo(source: GitIniSource, dest: Path) -> None:
    """Clone a Git INI repository into ``dest`` and check out ``source.ref`` if set."""
    if shutil.which("git") is None:
        raise Exception("Required command not found in PATH: git")

    if source.ref:
        try:
            _run([
                "git", "clone",
                "--depth", "1",
                "--single-branch",
                "--branch", source.ref,
                source.repo,
                str(dest),
            ])
            return
        except Exception:
            # Branches and tags are handled by the fast path above. A raw commit
            # hash may not be usable with ``git clone --branch``, so fall back to
            # a full clone followed by an explicit checkout.
            _logger.info(
                "Shallow Git INI clone for ref '%s' failed; retrying with a full clone.",
                source.ref,
            )
            _rmtree(dest)
            _run(["git", "clone", source.repo, str(dest)])
            _run(["git", "checkout", source.ref], cwd=dest)
            return

    _run(["git", "clone", "--depth", "1", source.repo, str(dest)])


def _copy_git_ini_to_root(
        source: GitIniSource,
        root: Path,
        target_name: str = _DEFAULT_PROJECT_INI_NAME,
) -> Path:
    """Fetch a Git-backed remote INI, copy it into ``root``, and return that local copy."""
    with tempfile.TemporaryDirectory(prefix="odt-env-git-ini-") as tmp_dir:
        repo_dir = Path(tmp_dir) / "repo"
        _clone_git_ini_repo(source, repo_dir)

        source_ini_path = repo_dir.joinpath(*source.path.parts)
        if not source_ini_path.exists():
            raise Exception(
                "Git INI file does not exist in the cloned repository: "
                f"{source.path.as_posix()}"
            )
        if not source_ini_path.is_file():
            raise Exception(
                "Git INI path is not a file in the cloned repository: "
                f"{source.path.as_posix()}"
            )

        copied_ini_path = root / target_name
        try:
            shutil.copy2(source_ini_path, copied_ini_path)
        except OSError as e:
            raise Exception(
                f"Failed to copy Git INI to workspace root: {copied_ini_path} ({e})"
            ) from e

        try:
            copied_ini_path = copied_ini_path.resolve()
        except Exception:
            copied_ini_path = copied_ini_path.absolute()

        _logger.info(
            "Copied Git INI into workspace root: %s (source=%s//%s, ref=%s)",
            copied_ini_path,
            source.repo,
            source.path.as_posix(),
            source.ref or "<default>",
        )
        return copied_ini_path


def _github_blob_url_to_raw(url: str) -> Optional[str]:
    """Convert a GitHub web UI blob URL to a raw.githubusercontent.com URL."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.lower() != "github.com":
        return None

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 5 or parts[2] != "blob":
        return None

    owner, repo, _blob, ref, *file_parts = parts
    if not owner or not repo or not ref or not file_parts:
        return None
    if any(part in (".", "..") for part in file_parts):
        raise Exception("Invalid GitHub blob URL: file path must not contain '.' or '..'.")

    raw_path = "/" + "/".join([owner, repo, ref, *file_parts])
    return urlunparse(("https", "raw.githubusercontent.com", raw_path, "", "", ""))


def _parse_url_ini_source(raw_ini: str) -> Optional[UrlIniSource]:
    """Parse a URL-backed remote INI source.

    GitHub ``/blob/`` URLs are accepted as a convenience and normalized to
    raw.githubusercontent.com URLs before downloading.
    """
    raw_value = (raw_ini or "").strip()
    parsed = urlparse(raw_value)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        raise Exception("Invalid URL INI source: missing host.")

    if parsed.scheme == "http":
        _logger.warning(
            "URL INI source uses plain HTTP; prefer HTTPS when possible: %s",
            raw_value,
        )

    normalized = _github_blob_url_to_raw(raw_value)
    if normalized is not None:
        _logger.info(
            "Normalized GitHub blob URL to raw INI URL: %s -> %s",
            raw_value,
            normalized,
        )
        return UrlIniSource(url=normalized, original_url=raw_value)

    return UrlIniSource(url=raw_value, original_url=raw_value)


def _safe_url_ini_filename(source_url: str) -> str:
    """Return a safe local filename for a downloaded URL INI."""
    parsed = urlparse(source_url)
    raw_name = PurePosixPath(unquote(parsed.path)).name.strip()
    if not raw_name or raw_name in {".", ".."}:
        return "odoo-project.ini"

    # Keep the URL basename but strip path separators that could appear after
    # decoding percent-encoded input.
    safe_name = raw_name.replace("/", "_").replace("\\", "_")
    return safe_name or "odoo-project.ini"


def _copy_url_ini_to_root(
        source: UrlIniSource,
        root: Path,
        target_name: str = _DEFAULT_PROJECT_INI_NAME,
        require_odoo_section: bool = True,
) -> Path:
    """Download a URL-backed remote INI into ``root`` and return that local copy."""
    request = Request(
        source.url,
        headers={"User-Agent": f"odt-env/{__version__}"},
        method="GET",
    )

    try:
        with urlopen(request, timeout=_URL_INI_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read(_URL_INI_MAX_BYTES + 1)
    except HTTPError as e:
        raise Exception(f"Failed to download URL INI: HTTP {e.code} {e.reason} ({source.url})") from e
    except URLError as e:
        raise Exception(f"Failed to download URL INI: {e.reason} ({source.url})") from e
    except OSError as e:
        raise Exception(f"Failed to download URL INI: {e} ({source.url})") from e

    if len(raw) > _URL_INI_MAX_BYTES:
        raise Exception(
            f"URL INI is too large: maximum supported size is {_URL_INI_MAX_BYTES} bytes ({source.url})"
        )

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise Exception(f"URL INI must be UTF-8 encoded: {source.url}") from e

    if not text.strip():
        raise Exception(f"URL INI is empty: {source.url}")

    # Fail early for common copy/paste mistakes where a web UI HTML page is
    # downloaded instead of the raw INI file.
    if re.search(r"<\s*!doctype\s+html|<\s*html[\s>]", text[:2048], flags=re.IGNORECASE):
        raise Exception(
            "Downloaded URL INI looks like an HTML page, not a raw INI file. "
            "Use a raw file URL instead."
        )

    probe = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())
    try:
        probe.read_string(text)
    except configparser.Error as e:
        raise Exception(f"Downloaded URL INI is not a valid INI file: {source.url} ({e})") from e
    if require_odoo_section and not probe.has_section("odoo"):
        raise Exception(f"Downloaded URL INI is missing required [odoo] section: {source.url}")

    if content_type:
        _logger.info("Downloaded URL INI content type: %s", content_type)

    copied_ini_path = root / target_name
    try:
        copied_ini_path.write_text(text, encoding="utf-8")
    except OSError as e:
        raise Exception(f"Failed to write URL INI to workspace root: {copied_ini_path} ({e})") from e

    try:
        copied_ini_path = copied_ini_path.resolve()
    except Exception:
        copied_ini_path = copied_ini_path.absolute()

    _logger.info(
        "Downloaded URL INI into workspace root: %s (source=%s)",
        copied_ini_path,
        source.original_url,
    )
    return copied_ini_path


def _resolve_local_ini_path(parser: argparse.ArgumentParser, raw_ini: str) -> Path:
    """Resolve and validate a local INI path supplied on the command line."""
    ini_path = Path(raw_ini).expanduser().resolve()
    if not ini_path.exists():
        parser.error(f'INI file does not exist: {ini_path}')
    if not ini_path.is_file():
        parser.error(f'INI path is not a file: {ini_path}')
    return ini_path


def _parse_remote_ini_sources(parser: argparse.ArgumentParser, raw_ini: str) -> tuple[Optional[GitIniSource], Optional[UrlIniSource]]:
    """Parse a CLI INI value as a Git or URL source, returning (git, url)."""
    try:
        git_ini_source = _parse_git_ini_source(raw_ini)
        url_ini_source = None if git_ini_source is not None else _parse_url_ini_source(raw_ini)
    except Exception as e:
        parser.error(str(e))
    return git_ini_source, url_ini_source


def _materialize_cli_ini_source(
        parser: argparse.ArgumentParser,
        raw_ini: str,
        destination_root: Path,
        *,
        require_odoo_section: bool = True,
        target_name: str = _DEFAULT_PROJECT_INI_NAME,
) -> Path:
    """Materialize a local, Git-backed, or URL-backed CLI INI source as a local file."""
    raw_value = (raw_ini or "").strip()
    if not raw_value:
        parser.error("Empty INI source.")

    git_ini_source, url_ini_source = _parse_remote_ini_sources(parser, raw_value)
    if git_ini_source is not None:
        destination_root.mkdir(parents=True, exist_ok=True)
        return _copy_git_ini_to_root(git_ini_source, destination_root, target_name=target_name)
    if url_ini_source is not None:
        destination_root.mkdir(parents=True, exist_ok=True)
        return _copy_url_ini_to_root(
            url_ini_source,
            destination_root,
            target_name=target_name,
            require_odoo_section=require_odoo_section,
        )
    return _resolve_local_ini_path(parser, raw_value)


def _source_kind_for_cli_ini(parser: argparse.ArgumentParser, raw_ini: str) -> str:
    git_ini_source, url_ini_source = _parse_remote_ini_sources(parser, raw_ini)
    if git_ini_source is not None:
        return "Git"
    if url_ini_source is not None:
        return "URL"
    return "local"


def _workspace_root_for_explicit_sources(
        parser: argparse.ArgumentParser,
        raw_sources: list[str],
        raw_root: Optional[str],
) -> Optional[Path]:
    """Resolve ROOT for an explicit positional INI and/or -i/--include stack."""
    if raw_root:
        return _validate_root_override(parser, raw_root)

    if not raw_sources:
        return None

    first_source = raw_sources[0]
    source_kind = _source_kind_for_cli_ini(parser, first_source)
    if source_kind == "local":
        first_path = _resolve_local_ini_path(parser, first_source)
        root = first_path.parent.resolve()
        _logger.info('Workspace ROOT default (first local INI directory): %s', root)
        return root

    try:
        cwd = Path.cwd()
    except OSError as e:
        parser.error(f'Failed to resolve current working directory for {source_kind} INI ROOT: {e}')

    try:
        root = cwd.resolve()
    except Exception:
        root = cwd.absolute()

    if not root.is_dir():
        parser.error(f'Current working directory for {source_kind} INI ROOT is not a directory: {root}')

    _logger.info(
        'Workspace ROOT default for %s INI (current working directory): %s',
        source_kind,
        root,
    )
    return root


def _read_merged_ini_layers(layer_paths: list[Path]) -> configparser.ConfigParser:
    """Read INI layers from left to right; later layers override earlier layers."""
    if not layer_paths:
        raise Exception("At least one INI layer is required.")

    cp = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())
    try:
        read_ok = cp.read(layer_paths, encoding="utf-8")
    except configparser.Error as e:
        raise Exception(f"Failed to read merged INI layers: {e}") from e

    read_ok_paths = set()
    for filename in read_ok:
        try:
            read_ok_paths.add(Path(filename).resolve())
        except Exception:
            read_ok_paths.add(Path(filename).absolute())

    missing = []
    for path in layer_paths:
        try:
            resolved_path = path.resolve()
        except Exception:
            resolved_path = path.absolute()
        if resolved_path not in read_ok_paths:
            missing.append(str(path))

    if missing:
        raise Exception(f"Failed to read INI layer(s): {', '.join(missing)}")

    return cp


def _merge_ini_layers_to_workspace_ini(layer_paths: list[Path], output_path: Path) -> Path:
    """Merge INI layers and write the merged project file into the workspace root."""
    cp = _read_merged_ini_layers(layer_paths)
    merged_ini = _ini_for_merged_source_file(cp)
    _write_text_file(
        output_path,
        merged_ini,
        private=True,
        atomic=True,
    )
    _logger.info(
        "Merged %s INI layer(s) into workspace project file: %s",
        len(layer_paths),
        output_path,
    )
    return output_path


def _safe_source_copy_name(index: int, source_path: Path) -> str:
    raw_name = source_path.name or "project.ini"
    safe_name = raw_name.replace("/", "_").replace("\\", "_")
    return f"{index:02d}-{safe_name or 'project.ini'}"


def _save_ini_layer_source_copies(root: Path, layer_paths: list[Path]) -> Optional[Path]:
    """Keep individual source INI layers under ROOT/.odt-env for audit/debugging."""
    if not layer_paths:
        return None

    source_dir = root / ".odt-env" / "last-source-project.d"
    if source_dir.exists():
        _rmtree(source_dir)
    source_dir.mkdir(parents=True, exist_ok=True)

    for index, source_path in enumerate(layer_paths, start=1):
        target = source_dir / _safe_source_copy_name(index, source_path)
        shutil.copy2(source_path, target)
        _chmod_private(target)

    _logger.info("Saved INI layer source copies: %s", source_dir)
    return source_dir


def _prepare_included_project_ini(
        parser: argparse.ArgumentParser,
        raw_sources: list[str],
        root: Path,
        vars_overrides: Optional[Dict[str, str]],
        ini_overrides: Optional[Dict[str, Dict[str, str]]],
        config_defaults: Optional[Dict[str, str]] = None,
) -> Path:
    """Materialize and merge positional INI + -i/--include layers into ROOT/odoo-project.ini."""
    with tempfile.TemporaryDirectory(prefix="odt-env-ini-layers-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        layer_paths: list[Path] = []

        for index, raw_source in enumerate(raw_sources, start=1):
            layer_root = tmp_root / f"layer-{index:02d}"
            layer_path = _materialize_cli_ini_source(
                parser,
                raw_source,
                layer_root,
                require_odoo_section=False,
                target_name=f"{index:02d}-{_DEFAULT_PROJECT_INI_NAME}",
            )
            layer_paths.append(layer_path)

        output_path = root / _DEFAULT_PROJECT_INI_NAME
        _save_ini_layer_source_copies(root, layer_paths)
        _merge_ini_layers_to_workspace_ini(layer_paths, output_path)
        _save_effective_ini_copy(
            output_path,
            vars_overrides=vars_overrides,
            ini_overrides=ini_overrides,
            config_defaults=config_defaults,
        )
        return output_path


def build_parser() -> argparse.ArgumentParser:
    epilog = """If no arguments are specified, odt-env prints help and exits.

Examples:

  Creating or syncing a workspace:
    odt-env --root ./odoo18-workspace --sync-all --create-venv
    odt-env /path/to/odoo-project.ini --sync-all --create-venv

  Using multiple project files:
    odt-env -i base-project.ini -i odoo-addons.ini --sync-all --create-venv

  Using a remote project file:
    odt-env git::https://github.com/lck/odoo-devops-tools.git//examples/odoo18-minimal.ini?ref=main --sync-all --create-venv

  Building a Docker image:
    odt-env /path/to/odoo-project.ini --sync-addons --build-docker-image

  Additional commands:
    odt-env /path/to/odoo-project.ini --create-venv-from-wheelhouse
    odt-env --show-last-run
"""

    parser = argparse.ArgumentParser(
        prog="odt-env",
        description=f"odt-env {__version__}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"odt-env {__version__}",
        help="Show the program version and exit.",
    )

    parser.add_argument(
        "ini",
        metavar="INI",
        nargs="?",
        help=(
            "Optional local path to odoo-project.ini, Git-backed remote INI "
            "git::REPO_URL//PATH/TO/PROJECT.ini?ref=REF, or URL to a raw INI file. "
            "If omitted and no -i/--include is provided, odt-env uses ROOT/odoo-project.ini, "
            "creating it from the bundled default template when missing."
        ),
    )

    parser.add_argument(
        "-i",
        "--include",
        dest="include_inis",
        action="append",
        default=[],
        metavar="INI",
        help=(
            "Include an additional project INI layer. Can be passed multiple times; "
            "layers are processed in CLI order and later layers override earlier layers. "
            "When a positional INI is also provided, it is used as the base layer before includes."
        ),
    )

    parser.add_argument(
        "--root",
        metavar="ROOT",
        default=None,
        help=(
            "Override workspace ROOT directory. By default, local INI uses its containing directory; "
            "remote INI sources use the current working directory. In include mode, the default is the "
            "directory of the first local source, or the current working directory when the first source is remote. "
            "Explicit ROOT is created automatically if needed, except for --show-last-run, which is read-only."
        ),
    )

    parser.add_argument(
        "-e",
        "--extra-var",
        dest="extra_vars",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Override or inject a variable in the optional [vars] INI section. "
            "Can be passed multiple times. Example: -e odoo_version=19.0"
        ),
    )

    parser.add_argument(
        "-S",
        "--set",
        dest="ini_overrides",
        action="append",
        default=[],
        metavar="SECTION:KEY=VALUE",
        help=(
            "Override an option that is already present in the INI file. "
            "Can be passed multiple times. New options are allowed only in [config]. "
            "Example: --set odoo:version=19.0"
        ),
    )

    target = parser.add_mutually_exclusive_group()
    target.add_argument("--sync-odoo", dest="odoo", action="store_true", help="Sync only Odoo repository")
    target.add_argument("--sync-addons", dest="addons", action="store_true", help="Sync only addon repositories")
    target.add_argument("--sync-all", dest="all", action="store_true", help="Sync Odoo + addons")

    parser.add_argument(
        "--create-venv",
        action="store_true",
        help=(
            "Enable virtualenv provisioning by recreating ROOT/venv and refreshing wheelhouse. "
            "If ROOT/venv already exists, it is deleted and created again. "
            "Without this flag, odt-env will not touch venv/wheelhouse. "
            "Wheelhouse is always rebuilt together with --create-venv."
        ),
    )
    parser.add_argument(
        "--create-venv-from-wheelhouse",
        action="store_true",
        help=(
            "Recreate ROOT/venv from existing ROOT/wheelhouse (and all-requirements.lock.txt) and install offline only. "
            "Implies --create-venv and skips lock compilation and wheelhouse build."
        ),
    )
    parser.add_argument(
        "--clear-pip-wheel-cache",
        action="store_true",
        help="Remove all items from the pip's wheel cache.",
    )

    parser.add_argument(
        "--build-docker-image",
        action="store_true",
        help=(
            "Build the Docker image configured by [docker].target_image by extending the configured base Docker image. "
            "In [docker].addons_mode=deploy, addons are copied into the image. "
            "In [docker].addons_mode=dev, addons are bind-mounted in ROOT/compose.yml. "
            "Also generates ROOT/compose.yml."
        ),
    )

    parser.add_argument(
        "--no-configs",
        action="store_true",
        help="Do not (re)generate config files (e.g. ROOT/odoo-configs/odoo-server.conf).",
    )
    parser.add_argument(
        "--no-scripts",
        action="store_true",
        help="Do not (re)generate helper scripts under ROOT/odoo-scripts/.",
    )
    parser.add_argument(
        "--no-data-dir",
        action="store_true",
        help="Do not generate odoo data folder.",
    )
    parser.add_argument(
        "--no-provisioning-log",
        action="store_true",
        help="Do not write provisioning log under ROOT/.odt-env/.",
    )
    parser.add_argument(
        "--show-last-run",
        action="store_true",
        help=(
            "Print ROOT/.odt-env/last-provisioning.json to stdout and exit without provisioning. "
            "ROOT is --root when provided, otherwise the current working directory."
        ),
    )

    return parser


def _validate_root_override(parser: argparse.ArgumentParser, raw_root: str) -> Path:
    """Validate, normalize, and prepare the --root override.

    - Expands '~'
    - Resolves to an absolute path
    - Creates the directory if it does not exist
    - Ensures it is a directory

    Returns the normalized Path.
    """
    _logger.info('CLI --root provided: %s', raw_root)

    candidate = Path(raw_root).expanduser()

    # Resolve to an absolute path for consistent workspace layout and logging.
    try:
        resolved = candidate.resolve()
    except Exception:
        # Fallback: make it absolute without resolving symlinks.
        resolved = candidate.absolute()

    if resolved.exists() and not resolved.is_dir():
        parser.error(f'--root path is not a directory: {resolved}')

    if not resolved.exists():
        try:
            _logger.info(f'Creating --root directory: {resolved}')
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            parser.error(f'Failed to create --root directory {resolved}: {e}')

    if not resolved.is_dir():
        parser.error(f'--root path is not a directory: {resolved}')

    _logger.info('Validated --root: %s', resolved)
    return resolved


def _show_last_run(root: Path) -> None:
    """Print ROOT/.odt-env/last-provisioning.json to stdout."""
    last_path = root / ".odt-env" / "last-provisioning.json"
    if not last_path.is_file():
        raise FileNotFoundError(f"Last provisioning log not found: {last_path}")

    text = last_path.read_text(encoding="utf-8")
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


def main() -> None:
    # Standard logging to stdout only.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = build_parser()

    if len(sys.argv) == 1:
        parser.print_help()
        return

    args = parser.parse_args()

    if bool(getattr(args, 'show_last_run', False)):
        root_candidate = Path(args.root).expanduser() if args.root else Path.cwd()
        try:
            root = root_candidate.resolve()
        except Exception:
            root = root_candidate.absolute()

        if not root.exists():
            parser.error(f"ROOT path does not exist: {root}")
        if not root.is_dir():
            parser.error(f"ROOT path is not a directory: {root}")

        try:
            _show_last_run(root)
        except Exception as e:
            print(f"{parser.prog}: error: {e}", file=sys.stderr)
            raise SystemExit(1)
        return

    clear_pip_wheel_cache = bool(getattr(args, 'clear_pip_wheel_cache', False))
    create_venv_from_wheelhouse = bool(getattr(args, 'create_venv_from_wheelhouse', False))
    reuse_wheelhouse = create_venv_from_wheelhouse
    create_venv = bool(getattr(args, 'create_venv', False)) or create_venv_from_wheelhouse
    no_configs = bool(getattr(args, 'no_configs', False))
    no_scripts = bool(getattr(args, 'no_scripts', False))
    no_data_dir = bool(getattr(args, 'no_data_dir', False))
    build_docker_image_requested = bool(getattr(args, 'build_docker_image', False))
    no_provisioning_log = bool(getattr(args, 'no_provisioning_log', False))

    try:
        vars_overrides = _parse_cli_vars(getattr(args, 'extra_vars', []) or [])
        ini_overrides = _parse_cli_ini_overrides(getattr(args, 'ini_overrides', []) or [])
    except Exception as e:
        parser.error(str(e))

    if vars_overrides:
        _logger.info('CLI [vars] overrides enabled for keys: %s', ', '.join(sorted(vars_overrides)))
    if ini_overrides:
        override_targets = [f"{section}:{key}" for section, options in ini_overrides.items() for key in options]
        _logger.info('CLI INI overrides enabled for keys: %s', ', '.join(sorted(override_targets)))

    root_override: Optional[Path] = None
    include_inis = [item for item in (getattr(args, 'include_inis', []) or []) if (item or "").strip()]
    has_positional_ini = bool((args.ini or "").strip())
    raw_explicit_sources = ([args.ini] if has_positional_ini else []) + include_inis
    implicit_ini = not raw_explicit_sources

    if implicit_ini:
        if args.root:
            root_override = _validate_root_override(parser, args.root)
        else:
            try:
                cwd = Path.cwd()
            except OSError as e:
                parser.error(f'Failed to resolve current working directory for implicit INI ROOT: {e}')

            try:
                root_override = cwd.resolve()
            except Exception:
                root_override = cwd.absolute()

            if not root_override.is_dir():
                parser.error(f'Current working directory for implicit INI ROOT is not a directory: {root_override}')

            _logger.info(
                'Workspace ROOT default for implicit INI (current working directory): %s',
                root_override,
            )

        ini_path = root_override / _DEFAULT_PROJECT_INI_NAME
        config_defaults = None if build_docker_image_requested else _IMPLICIT_LOCAL_CONFIG_DEFAULTS

        try:
            created_default_ini = not ini_path.exists()
            _write_default_project_ini_template(ini_path)
            if _implicit_ini_needs_effective_save(
                ini_path,
                vars_overrides=vars_overrides,
                ini_overrides=ini_overrides,
                config_defaults=config_defaults,
                force=created_default_ini,
            ):
                _save_effective_ini_copy(
                    ini_path,
                    vars_overrides=vars_overrides,
                    ini_overrides=ini_overrides,
                    config_defaults=config_defaults,
                )
        except Exception as e:
            _logger.error('%s', e)
            raise SystemExit(1)
    elif include_inis:
        root_override = _workspace_root_for_explicit_sources(parser, raw_explicit_sources, args.root)
        if root_override is None:
            parser.error("Internal error: failed to determine workspace ROOT for included INI layers.")

        try:
            ini_path = _prepare_included_project_ini(
                parser,
                raw_explicit_sources,
                root_override,
                vars_overrides=vars_overrides,
                ini_overrides=ini_overrides,
            )
        except Exception as e:
            _logger.error('%s', e)
            raise SystemExit(1)
    else:
        try:
            git_ini_source = _parse_git_ini_source(args.ini)
            url_ini_source = None if git_ini_source is not None else _parse_url_ini_source(args.ini)
        except Exception as e:
            parser.error(str(e))

        if git_ini_source is not None or url_ini_source is not None:
            source_kind = "Git" if git_ini_source is not None else "URL"
            if args.root:
                root_override = _validate_root_override(parser, args.root)
            else:
                try:
                    cwd = Path.cwd()
                except OSError as e:
                    parser.error(f'Failed to resolve current working directory for {source_kind} INI ROOT: {e}')

                try:
                    root_override = cwd.resolve()
                except Exception:
                    root_override = cwd.absolute()

                if not root_override.is_dir():
                    parser.error(f'Current working directory for {source_kind} INI ROOT is not a directory: {root_override}')

                _logger.info(
                    'Workspace ROOT default for %s INI (current working directory): %s',
                    source_kind,
                    root_override,
                )

            try:
                if git_ini_source is not None:
                    ini_path = _copy_git_ini_to_root(git_ini_source, root_override)
                else:
                    ini_path = _copy_url_ini_to_root(url_ini_source, root_override)

                _save_effective_ini_copy(
                    ini_path,
                    vars_overrides=vars_overrides,
                    ini_overrides=ini_overrides,
                )
            except Exception as e:
                _logger.error('%s', e)
                raise SystemExit(1)
        else:
            ini_path = _resolve_local_ini_path(parser, args.ini)

            if args.root:
                root_override = _validate_root_override(parser, args.root)
            else:
                _logger.info('Workspace ROOT default (INI directory): %s', ini_path.parent.resolve())

    if args.all:
        sync_odoo, sync_addons = True, True
    elif args.odoo:
        sync_odoo, sync_addons = True, False
    elif args.addons:
        sync_odoo, sync_addons = False, True
    else:
        # No sync target selected -> only regenerate configs + helper scripts.
        sync_odoo, sync_addons = False, False

    try:
        sync_project(
            ini_path,
            sync_odoo=sync_odoo,
            sync_addons=sync_addons,
            root_override=root_override,
            reuse_wheelhouse=reuse_wheelhouse,
            create_venv=create_venv,
            clear_pip_wheel_cache=clear_pip_wheel_cache,
            no_configs=no_configs,
            no_scripts=no_scripts,
            no_data_dir=no_data_dir,
            build_docker_image_requested=build_docker_image_requested,
            vars_overrides=vars_overrides,
            ini_overrides=ini_overrides,
            no_provisioning_log=no_provisioning_log,
            cli_argv=sys.argv,
        )
    except Exception as e:
        _logger.error(f'{e}')
        raise SystemExit(1)


if __name__ == "__main__":
    main()
