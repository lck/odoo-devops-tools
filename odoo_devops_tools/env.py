#!/usr/bin/env python3
"""
odoo-compose

Build a reproducible Odoo workspace from a project definition.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import ctypes
import hashlib
import io
import json
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse, urlunparse
from urllib.request import Request, urlopen

from . import __version__

_logger = logging.getLogger("odoo-compose")


_COMPLETION_SHELLS = ("bash", "zsh", "fish", "powershell")
_COMPLETION_PATH_OPTIONS = {
    "--root": "directory",
    "--create-from-bundle": "file",
    "--create-bundle": "file",
    "--dry-run": "directory",
    "-i": "file",
    "--include": "file",
}

_GIT_INI_PREFIX = "git+"
_URL_INI_TIMEOUT_SECONDS = 30
_URL_INI_MAX_BYTES = 1024 * 1024

_DEFAULT_PROJECT_INI_NAME = "odoo-project.ini"

_BUNDLE_FORMAT = "odt-env-workspace-bundle"
_BUNDLE_FORMAT_VERSION = 1
_BUNDLE_MANIFEST_NAME = "manifest.json"
_BUNDLE_DEFAULT_SUFFIX = ".odt.zip"
_BUNDLE_ALLOWED_TOP_LEVEL = {
    _BUNDLE_MANIFEST_NAME,
    _DEFAULT_PROJECT_INI_NAME,
    "odoo",
    "odoo-addons",
    "wheelhouse",
}
_BUNDLE_IGNORED_NAMES = {
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
_BUNDLE_IGNORED_SUFFIXES = {".pyc", ".pyo"}

_DEFAULT_PROJECT_INI_TEMPLATE = r"""
[odoo]
version = 19.0
"""

_DEFAULT_REQUIREMENTS = [
    "pip",
    "setuptools",
    "wheel",
    "click-odoo-contrib",
]

_DEFAULT_DOCKER_REQUIREMENTS = [
    "click-odoo-contrib",
]

_DEFAULT_DOCKER_UV_VERSION = "0.12.12"
_DEFAULT_DOCKER_RESTIC_VERSION = "0.19.1"
_DEFAULT_DOCKER_POSTGRES_VERSION = 16
_DEFAULT_DOCKER_MAILPIT_VERSION = "v1.31.3"
_DEFAULT_DOCKER_MAILPIT_SMTP_PORT = 1025
_DEFAULT_DOCKER_MAILPIT_WEBUI_PORT = 8025
_DEFAULT_DOCKER_MAILPIT_POP3_PORT = 1110
_DEFAULT_DOCKER_MAILPIT_POP3_AUTH = "odoo:odoo"
_DEFAULT_DOCKER_HTTP_CONTAINER_PORT = 8069
_DEFAULT_DOCKER_GEVENT_CONTAINER_PORT = 8072
_DOCKER_CONTEXT_LOCAL = "local"
_DOCKER_CONTEXT_DEPLOY = "deploy"
_DOCKER_ODOO_SOURCE_IMAGE = "image"
_DOCKER_ODOO_SOURCE_WORKSPACE = "workspace"
_DOCKER_ODOO_CONTAINER_ROOT = PurePosixPath("/opt/odoo")
_DOCKER_ADDONS_CONTAINER_ROOT = PurePosixPath("/mnt/extra-addons")
_DOCKER_HOST_PORT_CONFIG_KEYS = {"http_port", "gevent_port", "longpolling_port"}
_DOCKER_DB_CONNECTION_CONFIG_KEYS = {"db_host", "db_port", "db_name", "db_user", "db_password"}
_DOCKER_MAILPIT_OVERRIDDEN_CONFIG_KEYS = {
    "smtp_server",
    "smtp_port",
    "smtp_ssl",
    "smtp_user",
    "smtp_password",
}
_DOCKER_ODOO_CONF_IGNORED_CONFIG_KEYS = {
    "addons_path",
    "data_dir",
    *_DOCKER_HOST_PORT_CONFIG_KEYS,
    *_DOCKER_DB_CONNECTION_CONFIG_KEYS,
}

_DEFAULT_ODOO_REPO = "https://github.com/odoo/odoo.git"

_BACKUP_RESTORE_SCRIPT_NAMES = (
    "backup-db", "restore-db", "backup-filestore", "restore-filestore",
    "restic", "backup-filestore-restic", "restore-filestore-restic",
)

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
    20: ("3.12", "3.14", [], []),
}

_SENSITIVE_KEYS = ("password", "passwd", "secret", "token", "api_key", "apikey", "private_key")

_INI_SET_SUPPORTED_OPTIONS = {
    "virtualenv": {
        "managed_python",
        "python_version",
        "build_constraints",
        "constraints",
        "requirements",
        "requirements_ignore",
    },
    "odoo": {
        "version",
        "repo",
        "branch",
        "commit",
        "shallow",
        "path",
    },
    "docker": {
        "base_image",
        "odoo_source",
        "postgres_version",
        "restic_version",
        "mailpit_enabled",
        "mailpit_version",
        "mailpit_webui_port",
        "odoo_cpus",
        "odoo_mem_limit",
        "db_cpus",
        "db_mem_limit",
        "mailpit_cpus",
        "mailpit_mem_limit",
        "restart",
    },
}
_INI_SET_ADDON_SUPPORTED_OPTIONS = {
    "repo",
    "branch",
    "commit",
    "shallow",
    "path",
}
_INI_SET_DOCKER_SHARE_SUPPORTED_OPTIONS = {
    "source",
    "target",
}

_NATIVE_RUNTIME_STATE = "odoo-process.json"
_NATIVE_RUNTIME_LOCK = "odoo-process.lock"
_NATIVE_RUNTIME_LOG = "odoo-up.log"

_CLI_COMMANDS = (
    ("setup", "Create or update an Odoo workspace"),
    ("up", "Prepare and start the Odoo workspace"),
    ("down", "Stop the Odoo workspace (keep data)"),
    ("status", "Show the Odoo runtime status"),
    ("completion", "Generate shell completion script (bash, zsh, fish, powershell)"),
    ("run", "Run Odoo"),
    ("shell", "Open an Odoo shell"),
    ("update", "Update installed Odoo modules"),
    ("test", "Run Odoo tests"),
    ("neutralize", "Neutralize the workspace database"),
    ("backup-db", "Back up the database"),
    ("restore-db", "Restore the database"),
    ("backup-filestore", "Back up the filestore"),
    ("restore-filestore", "Restore the filestore"),
    ("restic", "Run a Restic command"),
    ("backup-filestore-restic", "Back up the filestore using Restic"),
    ("restore-filestore-restic", "Restore a Restic filestore snapshot"),
)


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
    constraints: list[str]
    requirements: list[str]
    requirements_ignore: list[str]
    explicit_requirements: list[str]
    explicit_requirements_ignore: list[str]
    managed_python: bool = True


@dataclass(frozen=True)
class DockerShareSpec:
    source: str
    target: str


@dataclass(frozen=True)
class DockerConfig:
    base_image: Optional[str] = None
    odoo_source: str = _DOCKER_ODOO_SOURCE_IMAGE
    postgres_version: int = _DEFAULT_DOCKER_POSTGRES_VERSION
    restic_version: str = _DEFAULT_DOCKER_RESTIC_VERSION
    mailpit_enabled: bool = False
    mailpit_version: str = _DEFAULT_DOCKER_MAILPIT_VERSION
    mailpit_webui_port: int = _DEFAULT_DOCKER_MAILPIT_WEBUI_PORT
    restart: str = "unless-stopped"
    odoo_cpus: Optional[str] = None
    odoo_mem_limit: Optional[str] = None
    db_cpus: Optional[str] = None
    db_mem_limit: Optional[str] = None
    mailpit_cpus: Optional[str] = None
    mailpit_mem_limit: Optional[str] = None
    shares: Dict[str, DockerShareSpec] = field(default_factory=dict)


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
    wheelhouse_dir: Path
    docker_dir: Path
    docker_local_dir: Path
    docker_deploy_dir: Path

    @staticmethod
    def from_root(root: Path) -> "Layout":
        odoo_dir = root / "odoo"
        addons_root = root / "odoo-addons"
        backups_dir = root / "odoo-backups"
        configs_dir = root / "odoo-configs"
        conf_path = configs_dir / "odoo-server.conf"
        data_dir = root / "odoo-data"
        wheelhouse_dir = root / "wheelhouse"
        docker_dir = root / "docker"
        docker_local_dir = docker_dir / _DOCKER_CONTEXT_LOCAL
        docker_deploy_dir = docker_dir / _DOCKER_CONTEXT_DEPLOY
        return Layout(
            root=root,
            odoo_dir=odoo_dir,
            addons_root=addons_root,
            backups_dir=backups_dir,
            configs_dir=configs_dir,
            conf_path=conf_path,
            data_dir=data_dir,
            wheelhouse_dir=wheelhouse_dir,
            docker_dir=docker_dir,
            docker_local_dir=docker_local_dir,
            docker_deploy_dir=docker_deploy_dir,
        )


# -----------------------------
# Helpers: validation & parsing
# -----------------------------

class _IniInterpolation(configparser.ExtendedInterpolation):
    """Interpolate ${section:option}; literal dollars need no escaping.
    """

    def before_set(self, parser, section, option, value):
        # Literal '$' and '$$' are permitted in every INI value.
        return value

    def _interpolate_some(self, parser, option, accum, rest, section, map, depth):
        rawval = parser.get(section, option, raw=True, fallback=rest)
        if depth > configparser.MAX_INTERPOLATION_DEPTH:
            raise configparser.InterpolationDepthError(option, section, rawval)

        while rest:
            pos = rest.find("${")
            if pos < 0:
                accum.append(rest)
                return

            prefix = rest[:pos]
            slash_count = len(prefix) - len(prefix.rstrip("\\"))
            accum.append(prefix[:len(prefix) - slash_count] + "\\" * (slash_count // 2))
            match = self._KEYCRE.match(rest, pos)

            if slash_count % 2:
                # An odd number of backslashes makes the reference literal.
                if match:
                    accum.append(match.group(0))
                    rest = rest[match.end():]
                else:
                    accum.append("${")
                    rest = rest[pos + 2:]
                continue

            if match is None:
                raise configparser.InterpolationSyntaxError(
                    option, section, "bad interpolation variable reference %r" % rest[pos:]
                )

            path = match.group(1).split(":")
            rest = rest[match.end():]
            sect = section
            opt = option
            try:
                if len(path) == 1:
                    opt = parser.optionxform(path[0])
                    value = map[opt]
                elif len(path) == 2:
                    sect = path[0]
                    opt = parser.optionxform(path[1])
                    value = parser.get(sect, opt, raw=True)
                else:
                    raise configparser.InterpolationSyntaxError(
                        option, section, "More than one ':' found: %r" % match.group(0)
                    )
            except (KeyError, configparser.NoSectionError, configparser.NoOptionError):
                raise configparser.InterpolationMissingOptionError(
                    option, section, rawval, ":".join(path)
                ) from None

            if value is not None:
                if "${" in value:
                    self._interpolate_some(
                        parser, opt, accum, value, sect,
                        dict(parser.items(sect, raw=True)), depth + 1,
                    )
                else:
                    accum.append(value)


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
        stream_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=not stream_output,
    )

    failure_message = err_msg if err_msg else "Command failed"

    if not stream_output:
        _handle_process_output(
            p,
            f"{failure_message}\n"
            f"Command: {_format_cmd(cmd)}\n"
            f"{p.stdout}\n{p.stderr}",
        )
    elif p.returncode != 0:
        raise Exception(
            f"{failure_message}\n"
            f"Command: {_format_cmd(cmd)}"
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


def _render_resolved_ini(
        cp: configparser.ConfigParser, *, redact: bool = False, escape_references: bool = False,
) -> str:
    """Render an interpolated INI copy. ConfigParser does not preserve comments."""
    rendered = configparser.ConfigParser(interpolation=None)

    for section in cp.sections():
        rendered.add_section(section)
        for option in cp._sections.get(section, {}).keys():
            value = cp.get(section, option, raw=False)
            if redact and _is_sensitive_key(option):
                value = "******"
            if escape_references:
                # This INI may be reloaded: a resolved literal ${...} must not
                # become a new reference. Backslashes before it must round-trip.
                value = re.sub(
                    r"(\\*)\$\{",
                    lambda m: "\\" * (2 * len(m.group(1)) + 1) + "${",
                    value,
                )
            rendered.set(section, option, value)

    buf = io.StringIO()
    rendered.write(buf)
    return buf.getvalue()


def _ini_for_audit_log(cp: configparser.ConfigParser) -> str:
    """Return resolved INI content suitable for audit logs."""
    return _render_resolved_ini(cp, redact=True)


def _ini_for_effective_file(cp: configparser.ConfigParser) -> str:
    """Return resolved INI content suitable for saving as the effective project file."""
    return _render_resolved_ini(cp, redact=False, escape_references=True)


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


def _path_for_report(path: Path) -> str:
    """Return a stable absolute path for human-readable reports."""
    try:
        return str(path.resolve())
    except Exception:
        return str(path.absolute())


def _same_path(left: Path, right: Path) -> bool:
    """Best-effort path comparison used only for reporting decisions."""
    try:
        return left.resolve() == right.resolve()
    except Exception:
        return left.absolute() == right.absolute()


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
        vars_overrides: Optional[Dict[str, str]],
        ini_overrides: Optional[Dict[str, Dict[str, str]]],
        force: bool = False,
) -> bool:
    """Return True when implicit-INI mode should persist an effective project file."""
    return bool(force or vars_overrides or ini_overrides)


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
) -> None:
    """
    Rewrite a workspace-root INI with the values actually used after CLI
    overrides.

    The original template/source file is kept under ROOT/.odt-env/last-source-project.ini,
    and the resolved/effective copy is kept under ROOT/.odt-env/last-resolved-project.ini.
    """
    cp = _read_ini(
        ini_path,
        vars_overrides=vars_overrides,
        ini_overrides=ini_overrides,
        log_overrides=False,
    )
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


def _update_ini_values_preserving_layout(
        content: str,
        vars_overrides: Optional[Dict[str, str]],
        ini_overrides: Optional[Dict[str, Dict[str, str]]],
) -> str:
    """Apply CLI overrides without rewriting unrelated INI lines or interpolation."""
    values: Dict[str, Dict[str, str]] = {}
    if vars_overrides:
        values["vars"] = dict(vars_overrides)
    if ini_overrides:
        for section, options in ini_overrides.items():
            values.setdefault(section, {}).update(options)
    if not values:
        return content

    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines(keepends=True)
    def ini_value(value: str) -> str:
        # Keep multi-line CLI values indented as valid INI continuation lines.
        return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline + "    ")
    sections: Dict[str, tuple[int, int]] = {}
    current_section = None
    start = 0
    header_pattern = re.compile(r"^[ \t]*\[([^\]\r\n]+)\][ \t]*(?:[;#].*)?$")
    for index, line in enumerate(lines):
        match = header_pattern.fullmatch(line.rstrip("\r\n"))
        if match:
            if current_section is not None:
                sections[current_section] = (start, index)
            current_section = match.group(1)
            start = index + 1
    if current_section is not None:
        sections[current_section] = (start, len(lines))

    # Process sections in reverse file order so earlier indices remain stable.
    for section, (start, end) in sorted(
            ((name, bounds) for name, bounds in sections.items() if name in values),
            key=lambda item: item[1][0], reverse=True,
    ):
        pending = {key.lower(): (key, value) for key, value in values.pop(section).items()}
        option_pattern = re.compile(r"^([ \t]*[^:=\s][^:=]*?)([ \t]*[=:][ \t]*)(.*)$")
        for index in range(end - 1, start - 1, -1):
            line = lines[index]
            match = option_pattern.fullmatch(line.rstrip("\r\n"))
            if not match:
                continue
            option = match.group(1).strip().lower()
            if option not in pending:
                continue
            _name, value = pending.pop(option)
            ending = line[len(line.rstrip("\r\n")):]
            # Remove any continuation lines of the previous multi-line value.
            next_index = index + 1
            while next_index < len(lines) and next_index < end:
                continuation = lines[next_index]
                stripped = continuation.strip()
                if stripped and continuation[:1] in " \t" and not stripped.startswith(("#", ";")):
                    del lines[next_index]
                    end -= 1
                elif not stripped or stripped.startswith(("#", ";")):
                    next_index += 1
                else:
                    break
            lines[index] = f"{match.group(1)}{match.group(2)}{ini_value(value)}{ending}"
        if pending:
            new_lines = [f"{key} = {ini_value(value)}{newline}" for key, value in pending.values()]
            # Keep trailing blank lines between sections after the new options.
            insert_at = end
            while insert_at > start and not lines[insert_at - 1].strip():
                insert_at -= 1
            # Ensure a missing final line ending doesn't join the new option.
            if insert_at > 0 and lines[insert_at - 1] and not lines[insert_at - 1].endswith(("\r", "\n")):
                lines[insert_at - 1] += newline
            lines[insert_at:insert_at] = new_lines

    for section, options in values.items():
        if lines and not lines[-1].endswith(("\r", "\n")):
            lines[-1] += newline
        if lines and lines[-1].strip():
            lines.append(newline)
        lines.append(f"[{section}]{newline}")
        lines.extend(f"{key} = {ini_value(value)}{newline}" for key, value in options.items())
    return "".join(lines)


def _save_workspace_ini_overrides(
        source_ini: Path,
        workspace_ini: Path,
        vars_overrides: Optional[Dict[str, str]],
        ini_overrides: Optional[Dict[str, Dict[str, str]]],
) -> None:
    """Persist --set/-e only to ROOT/odoo-project.ini, preserving source layout."""
    # Reject invalid effective values before touching the workspace file.
    load_project_config(
        source_ini, vars_overrides=vars_overrides,
        ini_overrides=ini_overrides, log_resolved=False, log_overrides=False,
    )
    cp = _read_ini(source_ini, vars_overrides, ini_overrides, log_overrides=False)
    resolved_ini = _ini_for_effective_file(cp)
    original = source_ini.read_bytes().decode("utf-8")
    updated = _update_ini_values_preserving_layout(original, vars_overrides, ini_overrides)
    workspace_ini.parent.mkdir(parents=True, exist_ok=True)
    source_copy = workspace_ini.parent / ".odt-env" / "last-source-project.ini"
    source_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_ini, source_copy)
    _chmod_private(source_copy)
    # Preserve existing CRLF/LF line endings byte for byte, including on Windows.
    temp_path = workspace_ini.with_suffix(workspace_ini.suffix + ".tmp")
    temp_path.write_bytes(updated.encode("utf-8"))
    os.replace(temp_path, workspace_ini)
    _chmod_private(workspace_ini)
    resolved_copy = _save_resolved_project_ini_copy(workspace_ini, resolved_ini)
    _logger.info(
        "Saved CLI overrides to workspace INI: %s (original source: %s, resolved copy: %s)",
        workspace_ini, source_copy, resolved_copy,
    )


def _refresh_last_ini_snapshots(
        layout: Layout,
        ini_path: Path,
        vars_overrides: Optional[Dict[str, str]],
        ini_overrides: Optional[Dict[str, Dict[str, str]]],
        source_snapshot_ready: bool = False,
) -> None:
    """Keep current source and resolved INI snapshots on every provisioning run."""
    cp = _read_ini(
        ini_path,
        vars_overrides=vars_overrides,
        ini_overrides=ini_overrides,
        log_overrides=False,
    )
    resolved_ini = _ini_for_effective_file(cp)
    source_snapshot = layout.root / ".odt-env" / "last-source-project.ini"
    if not source_snapshot_ready:
        source_snapshot.parent.mkdir(parents=True, exist_ok=True)
        if not _same_path(ini_path, source_snapshot):
            shutil.copy2(ini_path, source_snapshot)
        _chmod_private(source_snapshot)
    _save_resolved_project_ini_copy(layout.root / _DEFAULT_PROJECT_INI_NAME, resolved_ini)


def _existing_workspace_db_name(root: Path) -> Optional[str]:
    """Keep a database name already persisted in the workspace across INI refreshes."""
    ini_path = root / _DEFAULT_PROJECT_INI_NAME
    if not ini_path.is_file():
        return None
    cp = _read_ini(ini_path, log_overrides=False)
    value = cp.get("config", "db_name", fallback="").strip()
    return value or None


def _generated_workspace_db_name(root: Path) -> str:
    """Stable fallback, unique across same-named workspaces at different paths."""
    slug = re.sub(r"[^a-z0-9]+", "_", root.name.lower()).strip("_") or "odoo"
    digest = hashlib.sha256(os.fsencode(str(root.resolve()))).hexdigest()[:10]
    # PostgreSQL identifiers are at most 63 bytes; the slug is ASCII-only.
    return f"{slug[:52]}_{digest}"


def _persist_native_db_name(
        root: Path,
        ini_path: Path,
        db_name: str,
        vars_overrides: Optional[Dict[str, str]],
        ini_overrides: Optional[Dict[str, Dict[str, str]]],
) -> None:
    """Save the fallback to ROOT/odoo-project.ini without modifying external sources."""
    project_ini = root / _DEFAULT_PROJECT_INI_NAME
    if _same_path(ini_path, project_ini):
        # Preserve comments, ordering and interpolation for locally maintained INI.
        original = project_ini.read_text(encoding="utf-8")
        section = re.search(r"(?im)^[ \t]*\[config\][ \t]*(?:[;#][^\r\n]*)?$", original)
        if section is None:
            updated = original.rstrip("\r\n") + f"\n\n[config]\ndb_name = {db_name}\n"
        else:
            following = re.search(r"(?m)^[ \t]*\[[^\]\r\n]+\]", original[section.end():])
            end = section.end() + following.start() if following else len(original)
            part = original[section.end():end]
            value_line = re.search(r"(?m)^[ \t]*db_name[ \t]*=[^\r\n]*", part)
            if value_line:
                start = section.end() + value_line.start()
                stop = section.end() + value_line.end()
                updated = original[:start] + f"db_name = {db_name}" + original[stop:]
            else:
                updated = original[:section.end()] + f"\ndb_name = {db_name}" + original[section.end():]
        _write_text_file(project_ini, updated, private=True, atomic=True)
    else:
        # For a local external INI, materialize the effective project definition
        # in the workspace; never edit the user's source file.
        cp = _read_ini(
            ini_path,
            vars_overrides=vars_overrides,
            ini_overrides=ini_overrides,
            log_overrides=False,
        )
        if not cp.has_section("config"):
            cp.add_section("config")
        cp.set("config", "db_name", db_name)
        _write_text_file(project_ini, _ini_for_effective_file(cp), private=True, atomic=True)

    _logger.warning(
        "[config].db_name was not set; generated '%s' and saved it to %s. "
        "Set db_name explicitly for a native deployment.",
        db_name,
        project_ini,
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


def _validate_ini_overrides(
        ini_overrides: Dict[str, Dict[str, str]],
) -> None:
    """Validate --set targets against the supported project-file schema."""
    for section, options in ini_overrides.items():
        if section == "config":
            for key in options:
                if key == "addons_path":
                    raise Exception(
                        "Invalid -S/--set override: option 'addons_path' is not supported in section [config]. "
                        "addons_path is always generated automatically."
                    )
            continue

        if section == "vars":
            # [vars] is intentionally schema-free. -e/--extra-var remains the
            # dedicated shorthand, but --set keeps working here as well.
            continue

        if section.startswith("addons."):
            addon_name = section.split(".", 1)[1].strip()
            if not addon_name:
                raise Exception(
                    "Invalid -S/--set override: addon section name must be in the form [addons.<name>]."
                )
            supported = _INI_SET_ADDON_SUPPORTED_OPTIONS
        elif section.startswith("docker.share."):
            share_name = section.split(".", 2)[2].strip()
            if not share_name:
                raise Exception(
                    "Invalid -S/--set override: Docker share section name must be in the form "
                    "[docker.share.<name>]."
                )
            supported = _INI_SET_DOCKER_SHARE_SUPPORTED_OPTIONS
        else:
            supported = _INI_SET_SUPPORTED_OPTIONS.get(section)
            if supported is None:
                supported_sections = ", ".join(
                    [
                        "vars",
                        "virtualenv",
                        "odoo",
                        "addons.<name>",
                        "docker",
                        "docker.share.<name>",
                        "config",
                    ]
                )
                raise Exception(
                    f"Invalid -S/--set override: unsupported section [{section}]. "
                    f"Supported sections: {supported_sections}."
                )

        for key in options:
            if key not in supported:
                supported_keys = ", ".join(sorted(supported))
                raise Exception(
                    f"Invalid -S/--set override: unsupported option '{key}' in section [{section}]. "
                    f"Supported options: {supported_keys}."
                )


def _read_ini(
        entry_ini: Path,
        vars_overrides: Optional[Dict[str, str]] = None,
        ini_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        log_overrides: bool = True,
) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=_IniInterpolation())
    read_ok = cp.read(entry_ini, encoding="utf-8")
    if not read_ok:
        raise Exception(f"Failed to read INI config: {entry_ini}")

    # Validate --set against the supported project-file schema before applying
    # overrides. Supported missing sections/options are created on demand.
    if ini_overrides:
        _validate_ini_overrides(ini_overrides)
        for section in ini_overrides:
            if not cp.has_section(section):
                cp.add_section(section)

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


def _validate_docker_image(value: str, option: str) -> str:
    image = (value or "").strip()
    # This value is embedded directly into Dockerfile FROM instructions.
    # Require a single Docker image reference (registry, tag and digest allowed).
    component = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    label = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    registry = rf"(?:{label}(?:\.{label})*(?::[0-9]+)?|\[[0-9A-Fa-f:.]+\](?::[0-9]+)?)"
    tag = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
    digest = r"[A-Za-z][A-Za-z0-9_+.-]*:[A-Za-z0-9=_-]{32,}"
    if (
        not image
        or len(image) > 512
        or "\n" in (value or "")
        or "\r" in (value or "")
        or re.fullmatch(
            rf"(?:{registry}/)?{component}(?:/{component})*(?::{tag})?(?:@{digest})?",
            image,
            flags=re.ASCII,
        ) is None
    ):
        raise Exception(
            f"Invalid option '{option}' in section [docker] "
            "(expected a single Docker image reference, optionally with registry, tag or digest)."
        )

    return image


def _validate_docker_image_version(value: str, option: str) -> str:
    """Accept an explicit release version, not a floating Docker tag."""
    version = (value or "").strip()
    if not re.fullmatch(
        r"v?[0-9]+\.[0-9]+\.[0-9]+(?:[-._][A-Za-z0-9][A-Za-z0-9._-]*)?",
        version,
        flags=re.ASCII,
    ) or len(version) > 128:
        raise Exception(
            f"Invalid option '{option}' in section [docker] "
            "(expected a specific version, for example '0.19.1' or 'v1.31.3'; "
            "empty values and floating tags such as 'latest' are not allowed)."
        )
    return version


def _validate_docker_source(value: str, option: str) -> str:
    source = (value or "").strip().lower()
    supported = {_DOCKER_ODOO_SOURCE_IMAGE, _DOCKER_ODOO_SOURCE_WORKSPACE}
    if source not in supported:
        choices = ", ".join(sorted(supported))
        raise Exception(
            f"Invalid option '{option}' in section [docker] "
            f"(expected one of: {choices})."
        )
    return source


def _validate_postgres_version(value: str, option: str) -> int:
    raw_value = (value or "").strip()
    if not re.fullmatch(r"[1-9]\d*", raw_value):
        raise Exception(
            f"Invalid option '{option}' in section [docker] "
            "(expected a positive integer PostgreSQL major version, for example 16 or 18)."
        )
    return int(raw_value)


def _validate_tcp_port(value: str, option: str) -> int:
    raw_value = (value or "").strip()
    try:
        port = int(raw_value)
    except ValueError as e:
        raise Exception(
            f"Invalid option '{option}' in section [docker] "
            "(expected an integer TCP port between 1 and 65535)."
        ) from e
    if not 1 <= port <= 65535:
        raise Exception(
            f"Invalid option '{option}' in section [docker] "
            "(expected an integer TCP port between 1 and 65535)."
        )
    return port


def _validate_docker_restart_policy(value: str, option: str) -> str:
    policy = (value or "").strip().lower()
    if policy not in {"no", "always", "unless-stopped", "on-failure"}:
        raise Exception(
            f"Invalid option '{option}' in section [docker] "
            "(expected one of: no, always, unless-stopped, on-failure)."
        )
    return policy


def _validate_docker_cpus(value: str, option: str) -> Optional[str]:
    raw = (value or "").strip()
    if not raw:
        return None
    if (
        len(raw) > 32
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw, flags=re.ASCII) is None
        or Decimal(raw) <= 0
    ):
        raise Exception(
            f"Invalid option '{option}' in section [docker] "
            "(expected a positive CPU count, for example 0.5 or 2.0)."
        )
    return str(Decimal(raw))


def _validate_docker_mem_limit(value: str, option: str) -> Optional[str]:
    raw = (value or "").strip()
    if not raw:
        return None
    # Docker Compose memory values are positive byte counts with optional units.
    if len(raw) > 32 or re.fullmatch(
        r"[1-9][0-9]*(?:b|[kmgpt](?:b|ib)?)?", raw, flags=re.ASCII | re.IGNORECASE
    ) is None:
        raise Exception(
            f"Invalid option '{option}' in section [docker] "
            "(expected a positive memory amount, for example 256m or 4g)."
        )
    return raw.lower()


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
    #   [docker.share.<name>] for each host-directory share (optional)
    #   [config] (optional)

    odoo_version = _require_ini_option(cp, "odoo", "version").strip()
    default_python_version, default_build_constraints, default_requirements = _get_default_virtualenv_settings(
        odoo_version
    )

    python_version = cp.get("virtualenv", "python_version", fallback="").strip()
    if not python_version:
        python_version = default_python_version

    ini_build_constraints = _get_ini_list(cp, "virtualenv", "build_constraints")
    ini_constraints = _get_ini_list(cp, "virtualenv", "constraints")
    ini_requirements = _get_ini_list(cp, "virtualenv", "requirements")

    build_constraints = list(dict.fromkeys([
        *default_build_constraints,
        *ini_build_constraints,
    ]))
    constraints = list(dict.fromkeys(ini_constraints))
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
        constraints=constraints,
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
            # Git addon destinations are ROOT/odoo-addons/<name>. Keep the
            # section name a single, portable path component (also on Windows).
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name.endswith("."):
                raise Exception(
                    f"Invalid addon section name [{sec}]: use letters, digits, '.', '_' or '-' "
                    "without path separators or traversal components."
                )

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

    docker_shares: Dict[str, DockerShareSpec] = {}
    share_targets: set[str] = set()
    for sec in cp.sections():
        if sec == "docker.share":
            raise Exception(
                "Invalid section [docker.share] (expected [docker.share.<name>])."
            )
        if not sec.startswith("docker.share."):
            continue

        name = sec.split(".", 2)[2].strip()
        if not name:
            raise Exception(
                f"Invalid section [{sec}] (expected [docker.share.<name>] with a non-empty name)."
            )

        for key in cp._sections.get(sec, {}).keys():
            if key not in _INI_SET_DOCKER_SHARE_SUPPORTED_OPTIONS:
                supported_keys = ", ".join(sorted(_INI_SET_DOCKER_SHARE_SUPPORTED_OPTIONS))
                raise Exception(
                    f"Unsupported option '{key}' in section [{sec}]. "
                    f"Supported options: {supported_keys}."
                )

        source = _require_ini_option(cp, sec, "source").strip()
        target = _require_ini_option(cp, sec, "target").strip()
        if not source:
            raise Exception(
                f"Invalid option 'source' in section [{sec}] (expected non-empty path)."
            )
        if not target:
            raise Exception(
                f"Invalid option 'target' in section [{sec}] (expected non-empty path)."
            )

        target_path = PurePosixPath(target)
        if not target_path.is_absolute() or ".." in target_path.parts:
            raise Exception(
                f"Invalid option 'target' in section [{sec}] "
                "(expected absolute container path without '..')."
            )
        target = str(target_path)
        if target in share_targets:
            raise Exception(
                f"Duplicate Docker share target '{target}' in section [{sec}]."
            )
        share_targets.add(target)
        docker_shares[name] = DockerShareSpec(source=source, target=target)

    docker = DockerConfig(
        base_image=f"odoo:{odoo_version}",
        odoo_source=_DOCKER_ODOO_SOURCE_IMAGE,
        postgres_version=_DEFAULT_DOCKER_POSTGRES_VERSION,
        restic_version=_DEFAULT_DOCKER_RESTIC_VERSION,
        mailpit_enabled=False,
        mailpit_version=_DEFAULT_DOCKER_MAILPIT_VERSION,
        mailpit_webui_port=_DEFAULT_DOCKER_MAILPIT_WEBUI_PORT,
        shares=docker_shares,
    )
    if cp.has_section("docker"):
        supported_docker_options = {
            "base_image",
            "odoo_source",
            "postgres_version",
            "restic_version",
            "mailpit_enabled",
            "mailpit_version",
            "mailpit_webui_port",
            "odoo_cpus",
            "odoo_mem_limit",
            "db_cpus",
            "db_mem_limit",
            "mailpit_cpus",
            "mailpit_mem_limit",
            "restart",
        }
        for key in cp._sections.get("docker", {}).keys():
            if key not in supported_docker_options:
                raise Exception(
                    f"Unsupported option '{key}' in section [docker]. "
                    "Supported options: base_image, mailpit_enabled, mailpit_version, "
                    "mailpit_webui_port, odoo_source, postgres_version, restic_version, "
                    "odoo_cpus, odoo_mem_limit, db_cpus, db_mem_limit, "
                    "mailpit_cpus, mailpit_mem_limit, restart."
                )

        base_image = (
            _validate_docker_image(cp.get("docker", "base_image"), "base_image")
            if cp.has_option("docker", "base_image")
            else f"odoo:{odoo_version}"
        )
        odoo_source = (
            _validate_docker_source(cp.get("docker", "odoo_source"), "odoo_source")
            if cp.has_option("docker", "odoo_source")
            else _DOCKER_ODOO_SOURCE_IMAGE
        )
        docker = DockerConfig(
            base_image=base_image,
            odoo_source=odoo_source,
            postgres_version=(
                _validate_postgres_version(
                    cp.get("docker", "postgres_version"),
                    "postgres_version",
                )
                if cp.has_option("docker", "postgres_version")
                else _DEFAULT_DOCKER_POSTGRES_VERSION
            ),
            restic_version=(
                _validate_docker_image_version(cp.get("docker", "restic_version"), "restic_version")
                if cp.has_option("docker", "restic_version")
                else _DEFAULT_DOCKER_RESTIC_VERSION
            ),
            mailpit_enabled=_get_ini_bool(cp, "docker", "mailpit_enabled", default=False),
            mailpit_version=(
                _validate_docker_image_version(cp.get("docker", "mailpit_version"), "mailpit_version")
                if cp.has_option("docker", "mailpit_version")
                else _DEFAULT_DOCKER_MAILPIT_VERSION
            ),
            mailpit_webui_port=(
                _validate_tcp_port(cp.get("docker", "mailpit_webui_port"), "mailpit_webui_port")
                if cp.has_option("docker", "mailpit_webui_port")
                else _DEFAULT_DOCKER_MAILPIT_WEBUI_PORT
            ),
            restart=(
                _validate_docker_restart_policy(cp.get("docker", "restart"), "restart")
                if cp.has_option("docker", "restart")
                else "unless-stopped"
            ),
            odoo_cpus=(
                _validate_docker_cpus(cp.get("docker", "odoo_cpus"), "odoo_cpus")
                if cp.has_option("docker", "odoo_cpus") else None
            ),
            odoo_mem_limit=(
                _validate_docker_mem_limit(cp.get("docker", "odoo_mem_limit"), "odoo_mem_limit")
                if cp.has_option("docker", "odoo_mem_limit") else None
            ),
            db_cpus=(
                _validate_docker_cpus(cp.get("docker", "db_cpus"), "db_cpus")
                if cp.has_option("docker", "db_cpus") else None
            ),
            db_mem_limit=(
                _validate_docker_mem_limit(cp.get("docker", "db_mem_limit"), "db_mem_limit")
                if cp.has_option("docker", "db_mem_limit") else None
            ),
            mailpit_cpus=(
                _validate_docker_cpus(cp.get("docker", "mailpit_cpus"), "mailpit_cpus")
                if cp.has_option("docker", "mailpit_cpus") else None
            ),
            mailpit_mem_limit=(
                _validate_docker_mem_limit(cp.get("docker", "mailpit_mem_limit"), "mailpit_mem_limit")
                if cp.has_option("docker", "mailpit_mem_limit") else None
            ),
            shares=docker_shares,
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

    project = ProjectConfig(
        virtualenv=venv,
        odoo=odoo,
        addons=addons,
        config=config,
        docker=docker,
    )
    # Reject unsafe explicit database names before generating workspace files.
    if str(config.get("db_name") or "").strip():
        _docker_default_db_name(project)
    return project


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

                out_lines.append(f"# odoo-compose: begin include {include_rel}")
                if include_path in visited:
                    out_lines.append(f"# odoo-compose: skipped recursive include {include_rel}")
                else:
                    visited.add(include_path)
                    out_lines.extend(_filter_requirements_file(include_path, ignore_names, visited=visited))
                out_lines.append(f"# odoo-compose: end include {include_rel}")
                continue

        # Editable installs: -e <spec> / --editable <spec>
        spec = no_comment.strip()
        if spec.startswith(("-e ", "--editable ")):
            parts = spec.split(maxsplit=1)
            spec = parts[1] if len(parts) == 2 else ""

        name = _extract_req_name_from_spec(spec)
        if name and name in ignore_names:
            out_lines.append(f"# odoo-compose: skipped (ignored package '{name}'): {raw}")
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


def _add_constraints(cmd: list[str], constraints_path: Path) -> None:
    if constraints_path.is_file():
        cmd.extend(["--constraints", str(constraints_path)])


def _write_constraints(
        path: Path,
        constraints: list[str],
        explicit_requirements: list[str],
) -> None:
    explicit_names = {
        name
        for spec in explicit_requirements
        if (name := _extract_req_name_from_spec(spec))
    }
    lines = ["# This file is generated by odoo-compose (DO NOT EDIT)."]
    for constraint in constraints:
        name = _extract_req_name_from_spec(constraint)
        if name and name in explicit_names:
            lines.append(
                f"# odoo-compose: skipped (overridden by explicit [virtualenv].requirements package '{name}'): {constraint}"
            )
        else:
            lines.append(constraint)
    _write_text_file(path, "\n".join(lines).rstrip("\n") + "\n")


def compile_all_requirements_lock(
        venv_python: Path,
        workspace_root: Path,
        requirement_files: list[Path],
        base_requirements: list[str],
        requirements_ignore: list[str],
        output_lock_path: Path,
        wheelhouse_dir: Path,
        constraints_path: Path,
        build_constraints_path: Path,
) -> Path:
    """Compile a single lock file from all Python requirement sources."""
    _ensure_command("uv")
    wheelhouse_dir.mkdir(parents=True, exist_ok=True)
    in_path = wheelhouse_dir / "all-requirements.in.txt"

    lines: list[str] = [
        "# This file is generated by odoo-compose (DO NOT EDIT).",
        "# Source: Odoo + addon repository requirements, plus [virtualenv].requirements and odoo-compose defaults.",
        "",
    ]
    _add_requirements_section(
        lines,
        "# --- base requirements (from INI + odoo-compose defaults) ---",
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
    _add_constraints(cmd, constraints_path)
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
    # Git 2.54+ can spawn background maintenance that races with a subsequent
    # shallow fetch and rewrites .git/shallow while it is being read.
    # Disable automatic maintenance for our Git commands without changing the
    # user's persistent Git configuration.
    run_cmd = ["git", "-c", "maintenance.auto=false", *cmd[1:]] if cmd and cmd[0] == "git" else cmd
    if cmd and cmd[0] == "git":
        _logger.info("git: %s (cwd=%s)", " ".join(run_cmd), str(cwd) if cwd else "<cwd>")
    p = subprocess.run(
        run_cmd,
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
        # A --single-branch clone restricts remote.origin.fetch to the cloned
        # branch. Fetching a different branch by name would update FETCH_HEAD
        # but NOT origin/<branch>, which checkout_branch() needs.
        fetch_cmd += [f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]
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


def checkout_branch(
        dest: Path,
        branch: str,
        fetch_all: bool = True,
        depth: Optional[int] = None,
        already_fetched: bool = False,
) -> None:
    """
    Checkout the requested `branch` in an existing repo.

    - fetch_all=True (default): do a broad fetch (all remotes/branches + tags), then checkout.
      This matches the previous behavior and is suitable for full clones (e.g. addons).

    - fetch_all=False: fetch ONLY `origin/<branch>` (optionally shallow via depth),
      then force local branch `<branch>` to match `origin/<branch>`.
      This is intended for the Odoo repo where we want single-branch + shallow clones.
    - already_fetched=True: reuse the fetch performed by ensure_repo() instead
      of fetching the same branch again.
    """
    _logger.info("checkout_branch: %s @ %s (fetch_all=%s, depth=%s)", dest, branch, fetch_all, depth)
    assert_clean_worktree(dest)

    if fetch_all:
        if not already_fetched:
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

    # Narrow fetch: ensure origin/<branch> exists even if origin was cloned
    # with --single-branch and has a restricted remote.origin.fetch refspec.
    # ensure_repo() already fetched this ref when already_fetched=True.
    if not already_fetched:
        _fetch_branch(dest, branch=branch, depth=depth)

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
    if spec.is_local:
        return _resolve_workspace_path(layout, spec.path or "", layout.addons_root / addon_name)
    dest = layout.addons_root / addon_name
    # Implicit Git destinations must not follow symlinks into another repo,
    # outside the workspace, or back onto the addons root itself.
    if (
        not _is_path_within(layout.addons_root, layout.root)
        or dest.is_symlink()
        or not _is_path_within(dest, layout.addons_root)
        or dest.resolve() == layout.addons_root.resolve()
    ):
        raise Exception(
            f"Unsafe addon destination for [addons.{addon_name}]: {dest} "
            f"must resolve inside {layout.addons_root}. Use an explicit 'path' for local addons."
        )
    return dest


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

    # Write every key from [config] (dynamic; no fixed schema), except values generated by odoo-compose.
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
# Docker artifact generation
# -----------------------------

def _docker_requirements_dir(docker_context_dir: Path) -> Path:
    return docker_context_dir / "requirements"


def _docker_configs_dir(docker_context_dir: Path) -> Path:
    return docker_context_dir / "configs"


def _docker_build_constraints_path(docker_context_dir: Path) -> Path:
    return _docker_requirements_dir(docker_context_dir) / "build-constraints.txt"


def _docker_constraints_path(docker_context_dir: Path) -> Path:
    return _docker_requirements_dir(docker_context_dir) / "constraints.txt"


def _docker_requirements_input_path(docker_context_dir: Path) -> Path:
    return _docker_requirements_dir(docker_context_dir) / "all-requirements.in.txt"


def _dockerfile_path(docker_context_dir: Path) -> Path:
    return docker_context_dir / "Dockerfile"


def _dockerignore_path(docker_context_dir: Path) -> Path:
    return docker_context_dir / ".dockerignore"


def _docker_addons_dir(docker_context_dir: Path) -> Path:
    return docker_context_dir / "addons"


def _docker_odoo_dir(docker_context_dir: Path) -> Path:
    return docker_context_dir / "odoo"


def _docker_odoo_conf_path(docker_context_dir: Path) -> Path:
    return _docker_configs_dir(docker_context_dir) / "odoo.conf"


def _docker_compose_path(layout: Layout) -> Path:
    return layout.root / "compose.yaml"


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


def _docker_local_container_addons_path(cfg: ProjectConfig) -> str:
    if not cfg.addons:
        return str(_DOCKER_ADDONS_CONTAINER_ROOT)

    mount_names = _docker_addon_mount_name_map(cfg)
    return ",".join(
        str(_DOCKER_ADDONS_CONTAINER_ROOT / mount_names[addon_name])
        for addon_name in cfg.addons
    )


def _docker_uses_workspace_odoo(cfg: ProjectConfig) -> bool:
    return cfg.docker.odoo_source == _DOCKER_ODOO_SOURCE_WORKSPACE


def _docker_workspace_odoo_path(layout: Layout, cfg: ProjectConfig) -> Path:
    source = _resolve_odoo_path(layout, cfg.odoo)
    if not source.exists() or not source.is_dir():
        raise Exception(
            f"Docker workspace Odoo source not found: {source}. "
            "Run with --sync-odoo/--sync-all first, or provide an existing [odoo].path."
        )
    odoo_bin = source / "odoo-bin"
    if not odoo_bin.is_file():
        raise Exception(f"Docker workspace Odoo source is missing odoo-bin: {odoo_bin}")
    return source


def _docker_workspace_core_addons_path() -> str:
    return ",".join([
        str(_DOCKER_ODOO_CONTAINER_ROOT / "addons"),
        str(_DOCKER_ODOO_CONTAINER_ROOT / "odoo" / "addons"),
    ])


def _docker_runtime_addons_path(cfg: ProjectConfig, *, local: bool) -> str:
    extra_addons = (
        _docker_local_container_addons_path(cfg)
        if local
        else str(_DOCKER_ADDONS_CONTAINER_ROOT)
    )
    if not _docker_uses_workspace_odoo(cfg):
        return extra_addons
    return f"{_docker_workspace_core_addons_path()},{extra_addons}"


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


def _docker_requirements_ignore(cfg: VirtualenvConfig) -> list[str]:
    ignore = list(cfg.explicit_requirements_ignore)
    for spec in cfg.explicit_requirements:
        name = _extract_req_name_from_spec(spec)
        if name and name not in ignore:
            ignore.append(name)
    return ignore


def write_docker_requirements_input(
        workspace_root: Path,
        requirement_files: list[Path],
        explicit_requirements: list[str],
        requirements_ignore: list[str],
        output_path: Path,
) -> Path:
    """Write Docker dependency inputs without resolving them on the host."""
    lines: list[str] = [
        "# This file is generated by odoo-compose (DO NOT EDIT).",
        "# Source: addon repository requirements plus odoo-compose Docker defaults and explicit [virtualenv].requirements.",
        "# Dependency resolution is performed later inside the Docker build.",
        "",
    ]
    _add_requirements_section(lines, "# --- odoo-compose Docker default requirements ---", _DEFAULT_DOCKER_REQUIREMENTS)
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
    _write_requirements_input(output_path, lines)
    return output_path


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


def stage_docker_addons(layout: Layout, cfg: ProjectConfig, docker_context_dir: Path) -> int:
    """Stage concrete Odoo module directories for a deploy Docker build context."""
    addons_dir = _docker_addons_dir(docker_context_dir)
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
                    "Rename, remove, or split conflicting addon sources before generating the Docker deploy context."
                )

            dest = addons_dir / module_dir.name
            shutil.copytree(module_dir, dest, symlinks=True)
            staged_modules[module_dir.name] = module_dir

    if not staged_modules:
        (addons_dir / ".odt-env-empty").write_text(
            "No Odoo modules were staged for this Docker deploy context.\n",
            encoding="utf-8",
        )

    _logger.info("Staged %s Odoo addon module(s) for Docker deploy context.", len(staged_modules))
    return len(staged_modules)


def stage_docker_odoo_source(layout: Layout, cfg: ProjectConfig, docker_context_dir: Path) -> Optional[Path]:
    if not _docker_uses_workspace_odoo(cfg):
        return None

    source = _docker_workspace_odoo_path(layout, cfg)
    destination = _docker_odoo_dir(docker_context_dir)
    if destination.exists():
        _rmtree(destination)

    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git",
            "__pycache__",
            "*.pyc",
            "*.pyo",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
        ),
    )
    _logger.info("Staged workspace Odoo source for Docker deploy context: %s", destination)
    return destination


def write_dockerignore(docker_context_dir: Path, *, local: bool) -> Path:
    lines = [
        "# Generated by odoo-compose. Edit if your Docker build context needs different exclusions.",
    ]
    if local:
        lines.extend([
            "# Local runtime configuration is bind-mounted by Docker Compose.",
            "configs/",
            "# Local helper scripts run on the host and are not part of the image.",
            "scripts/",
        ])
    lines.extend([
        "**/__pycache__/",
        "**/*.py[cod]",
        "**/.pytest_cache/",
        "**/.mypy_cache/",
        "**/.ruff_cache/",
        "",
    ])
    path = _dockerignore_path(docker_context_dir)
    path.write_text("\n".join(lines), encoding="utf-8")
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
    """Render host-side ports for the generated local Docker Compose file."""
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
    workers = int(str(cfg.get("workers", "0")).strip() or "0")
    if host_async_port is None and workers > 0:
        host_async_port = _DEFAULT_DOCKER_GEVENT_CONTAINER_PORT

    if host_async_port is not None:
        ports.append(
            f'      - "{host_async_port}:{_DEFAULT_DOCKER_GEVENT_CONTAINER_PORT}"'
        )

    return "\n".join(ports)


def render_docker_odoo_conf(
        cfg: ProjectConfig,
        *,
        addons_path: str,
        local_database: bool = False,
) -> str:
    """Render Odoo config for a generated Docker runtime."""
    lines: list[str] = ["[options]"]

    lines.append(f"addons_path = {addons_path}")
    lines.append("data_dir = /var/lib/odoo")
    if _parse_odoo_version(cfg.odoo.version) >= 20:
        lines.append("http_interface = 0.0.0.0")

    # Database connection settings from [config] are runtime-specific and are
    # never copied into generated Docker configs.
    for key, value in cfg.config.items():
        if key in _DOCKER_ODOO_CONF_IGNORED_CONFIG_KEYS:
            continue
        if (
            local_database
            and cfg.docker.mailpit_enabled
            and key in _DOCKER_MAILPIT_OVERRIDDEN_CONFIG_KEYS
        ):
            continue
        lines.append(f"{key} = {_format_conf_value(value)}")

    # The generated local Compose environment has a fixed PostgreSQL service
    # and credentials. Keep these values in the local Odoo config as well so
    # tools executed directly with `docker compose exec`, such as
    # click-odoo-update, can connect without relying on the Odoo image entrypoint.
    # Deploy configs intentionally omit database connection settings.
    if local_database:
        lines.extend([
            "db_host = db",
            "db_port = 5432",
            "db_user = odoo",
            "db_password = odoo",
        ])
        if cfg.docker.mailpit_enabled:
            lines.extend([
                "smtp_server = mailpit",
                f"smtp_port = {_DEFAULT_DOCKER_MAILPIT_SMTP_PORT}",
                "smtp_ssl = False",
                "smtp_user = False",
                "smtp_password = False",
            ])

    return "\n".join(lines) + "\n"


def write_docker_odoo_conf(
        docker_context_dir: Path,
        cfg: ProjectConfig,
        *,
        addons_path: str,
        local_database: bool = False,
) -> Path:
    path = _docker_odoo_conf_path(docker_context_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_docker_odoo_conf(
            cfg,
            addons_path=addons_path,
            local_database=local_database,
        ),
        encoding="utf-8",
    )
    return path


def _render_docker_local_addon_volumes(layout: Layout, cfg: ProjectConfig) -> list[str]:
    """Render bind mounts for addon sources in the local Docker Compose environment."""
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


def _render_docker_share_volumes(layout: Layout, cfg: ProjectConfig) -> list[str]:
    """Render configured host-directory shares as read-write bind mounts."""
    volumes: list[str] = []
    for spec in cfg.docker.shares.values():
        source = _resolve_workspace_path(layout, spec.source, layout.root)
        volumes.extend([
            "      - type: bind",
            f"        source: {_yaml_quote_scalar(_compose_host_path(layout, source))}",
            f"        target: {_yaml_quote_scalar(spec.target)}",
            "        bind:",
            "          create_host_path: false",
        ])
    return volumes


def _render_docker_compose_volumes(layout: Layout, cfg: ProjectConfig) -> str:
    volumes = [
        "      - ./docker/local/configs:/etc/odoo:ro",
    ]
    if _docker_uses_workspace_odoo(cfg):
        odoo_source = _docker_workspace_odoo_path(layout, cfg)
        volumes.extend([
            "      - type: bind",
            f"        source: {_yaml_quote_scalar(_compose_host_path(layout, odoo_source))}",
            f"        target: {_yaml_quote_scalar(str(_DOCKER_ODOO_CONTAINER_ROOT))}",
            "        read_only: true",
        ])
    volumes.extend(_render_docker_local_addon_volumes(layout, cfg))
    volumes.extend(_render_docker_share_volumes(layout, cfg))
    volumes.append("      - odoo-data:/var/lib/odoo")
    return "\n".join(volumes)


def render_docker_compose(layout: Layout, cfg: ProjectConfig) -> str:
    """Render the local-development Docker Compose file."""
    ports = _render_docker_compose_ports(cfg.config)
    volumes = _render_docker_compose_volumes(layout, cfg)
    build_context = _yaml_quote_scalar(_compose_host_path(layout, layout.docker_local_dir))
    restart_value = '"no"' if cfg.docker.restart == "no" else cfg.docker.restart
    postgres_data_path = (
        "/var/lib/postgresql"
        if cfg.docker.postgres_version >= 18
        else "/var/lib/postgresql/data"
    )

    mailpit_service = ""
    depends_on = ["      db:", "        condition: service_healthy"]
    named_volumes = [
        "  odoo-db-data:",
        "  odoo-data:",
    ]

    if cfg.docker.mailpit_enabled:
        mailpit_resources = ""
        if cfg.docker.mailpit_cpus is not None:
            mailpit_resources += f"    cpus: {cfg.docker.mailpit_cpus}\n"
        if cfg.docker.mailpit_mem_limit is not None:
            mailpit_resources += f"    mem_limit: {cfg.docker.mailpit_mem_limit}\n"
        mailpit_service = f"""
  mailpit:
    image: ghcr.io/axllent/mailpit:{cfg.docker.mailpit_version}
    restart: {restart_value}
{mailpit_resources}    environment:
      MP_DATABASE: /data/mailpit.db
      MP_DISABLE_VERSION_CHECK: "true"
      MP_POP3_BIND_ADDR: "0.0.0.0:{_DEFAULT_DOCKER_MAILPIT_POP3_PORT}"
      MP_POP3_AUTH: "{_DEFAULT_DOCKER_MAILPIT_POP3_AUTH}"
    ports:
      - "{cfg.docker.mailpit_webui_port}:{_DEFAULT_DOCKER_MAILPIT_WEBUI_PORT}"
    volumes:
      - mailpit-data:/data
"""
        named_volumes.append("  mailpit-data:")

    depends_on_block = "\n".join(depends_on)
    named_volumes_block = "\n".join(named_volumes)
    db_resources = ""
    if cfg.docker.db_cpus is not None:
        db_resources += f"    cpus: {cfg.docker.db_cpus}\n"
    if cfg.docker.db_mem_limit is not None:
        db_resources += f"    mem_limit: {cfg.docker.db_mem_limit}\n"
    odoo_resources = ""
    if cfg.docker.odoo_cpus is not None:
        odoo_resources += f"    cpus: {cfg.docker.odoo_cpus}\n"
    if cfg.docker.odoo_mem_limit is not None:
        odoo_resources += f"    mem_limit: {cfg.docker.odoo_mem_limit}\n"

    return f"""services:
  db:
    image: postgres:{cfg.docker.postgres_version}
    restart: {restart_value}
{db_resources}    logging:
      driver: local
      options:
        max-size: "50m"
        max-file: "10"
    environment:
      POSTGRES_DB: postgres
      POSTGRES_USER: odoo
      POSTGRES_PASSWORD: odoo
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -q -h 127.0.0.1 -U $${{POSTGRES_USER}} -d $${{POSTGRES_DB}}"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 30s
    volumes:
      - odoo-db-data:{postgres_data_path}
{mailpit_service}
  odoo:
    build:
      context: {build_context}
    restart: {restart_value}
{odoo_resources}    logging:
      driver: local
      options:
        max-size: "50m"
        max-file: "10"
    depends_on:
{depends_on_block}
    healthcheck:
      test: ["CMD", "curl", "--fail", "--silent", "--show-error", "--max-time", "5", "--output", "/dev/null", "http://127.0.0.1:8069/web/login"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 90s
    ports:
{ports}
    environment:
      HOST: db
      PORT: 5432
      USER: odoo
      PASSWORD: odoo
    volumes:
{volumes}

volumes:
{named_volumes_block}
"""


def write_docker_compose(
        layout: Layout,
        cfg: ProjectConfig,
        source_ini: Path,
        effective_ini_sha256: str,
) -> Path:
    """Write ROOT/compose.yaml, preserving its timestamp if the content is unchanged."""
    path = _docker_compose_path(layout)

    if path.exists() and not path.is_file():
        raise Exception(f"Docker Compose path exists but is not a file: {path}")

    source_sha256 = hashlib.sha256(source_ini.read_bytes()).hexdigest()
    header = (
        "# =============================================================================\n"
        "# AUTO-GENERATED FILE - DO NOT EDIT MANUALLY\n"
        "# Generator: odoo-devops-tools (odoo-compose)\n"
        f"# Version: {__version__}\n"
        f"# Source: {source_ini.name}\n"
        f"# Source SHA256: {source_sha256}\n"
        f"# Effective SHA256: {effective_ini_sha256}\n"
        "# Effective SHA256 includes INI interpolation and CLI overrides.\n"
        "# Generated: __GENERATED_AT__\n"
        "# Update the source INI and regenerate this file.\n"
        "# =============================================================================\n\n"
    )
    compose_content = render_docker_compose(layout, cfg)

    if path.is_file():
        existing = path.read_text(encoding="utf-8", errors="replace")
        previous_generated_at = re.search(
            r"(?m)^# Generated: (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)$",
            existing,
        )
        if previous_generated_at:
            unchanged = header.replace(
                "__GENERATED_AT__", previous_generated_at.group(1)
            ) + compose_content
            if existing == unchanged:
                _logger.info("Docker Compose file is unchanged: %s", path)
                return path

    header = header.replace("__GENERATED_AT__", _utc_now_iso())
    path.write_text(header + compose_content, encoding="utf-8")
    _logger.info("Generated Docker Compose file: %s", path)
    return path


def _docker_default_db_name(cfg: ProjectConfig) -> str:
    db_name = cfg.config.get("db_name")
    if not isinstance(db_name, str) or not db_name.strip():
        return "odoo"
    db_name = db_name.strip()
    # The same name is used by database and destructive filestore helpers.
    if len(db_name) > 63 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", db_name):
        raise Exception(
            "Invalid [config].db_name: expected 1-63 ASCII letters, digits, "
            "underscores, dots or hyphens, starting with a letter, digit or underscore."
        )
    return db_name


def _native_default_db_name(cfg: ProjectConfig) -> str:
    db_name = str(cfg.config.get("db_name") or "").strip()
    if not db_name:
        raise Exception("Native database commands require [config].db_name.")
    # These names become PostgreSQL identifiers, filesystem path components and
    # Windows batch variables. Reject traversal, CMD metacharacters and names
    # that PostgreSQL could silently truncate to another database name.
    if len(db_name) > 63 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", db_name):
        raise Exception(
            "Invalid [config].db_name: expected 1-63 ASCII letters, digits, "
            "underscores, dots or hyphens, starting with a letter, digit or underscore."
        )
    return db_name


def _native_pg_env(cfg: ProjectConfig) -> dict[str, str]:
    # libpq reads these environment variables, keeping passwords out of command
    # arguments. Missing/False options leave libpq defaults and .pgpass intact.
    values: dict[str, str] = {}
    for key, variable in (
        ("db_host", "PGHOST"), ("db_port", "PGPORT"), ("db_user", "PGUSER"),
        ("db_password", "PGPASSWORD"), ("db_sslmode", "PGSSLMODE"),
    ):
        value = _format_conf_value(cfg.config.get(key, "")).strip()
        if value and value.lower() not in ("false", "none"):
            values[variable] = value
    return values


def write_dockerfile(
        docker_context_dir: Path,
        cfg: ProjectConfig,
        has_constraints: bool,
        has_build_constraints: bool,
        *,
        include_addons: bool,
        include_config: bool,
) -> Path:
    constraints_copy = ""
    constraints_compile = ""
    build_constraints_copy = ""
    build_constraints_compile = ""
    build_constraints_install = ""
    cleanup_constraints = ""
    if has_constraints:
        constraints_copy = "COPY requirements/constraints.txt /tmp/constraints.txt\n"
        constraints_compile = " --constraints /tmp/constraints.txt"
        cleanup_constraints += " /tmp/constraints.txt"
    if has_build_constraints:
        build_constraints_copy = "COPY requirements/build-constraints.txt /tmp/build-constraints.txt\n"
        build_constraints_compile = " --build-constraints /tmp/build-constraints.txt"
        build_constraints_install = " --build-constraints /tmp/build-constraints.txt"
        cleanup_constraints += " /tmp/build-constraints.txt"
    base_image = (cfg.docker.base_image or f"odoo:{cfg.odoo.version}").strip()
    addon_copy_step = ""
    addon_chown_step = ""
    if include_addons:
        addon_copy_step = """
COPY addons/ /mnt/extra-addons/
"""
        addon_chown_step = """

RUN chown -R odoo:odoo /mnt/extra-addons
"""

    config_copy_step = ""
    if include_config:
        config_copy_step = """
COPY --chown=odoo:odoo configs/odoo.conf /etc/odoo/odoo.conf
"""

    odoo_source_copy_step = ""
    workspace_base_odoo_cleanup_step = ""
    workspace_runtime_step = ""
    if _docker_uses_workspace_odoo(cfg):
        workspace_base_odoo_cleanup_step = """
RUN rm -rf \\
      /usr/lib/python3/dist-packages/odoo \\
      /usr/lib/python3/dist-packages/odoo-*.egg-info \\
      /usr/lib/python3/dist-packages/odoo-*.dist-info \\
 && rm -f /usr/bin/odoo
"""
        if include_addons:
            odoo_source_copy_step = f"""
COPY --chown=odoo:odoo odoo/ {_DOCKER_ODOO_CONTAINER_ROOT.as_posix()}/
"""
        workspace_runtime_step = f"""
ENV PYTHONPATH={_DOCKER_ODOO_CONTAINER_ROOT.as_posix()}

RUN printf '%s\\n' '#!/bin/sh' 'exec python3 {_DOCKER_ODOO_CONTAINER_ROOT.as_posix()}/odoo-bin "$@"' > /usr/local/bin/odoo \\
 && chmod 0755 /usr/local/bin/odoo
"""

    content = f"""# Generated by odoo-compose.
# Review and edit this file before building when project-specific changes are needed.
FROM {base_image}

USER root
{workspace_base_odoo_cleanup_step}
COPY --from=restic/restic:{cfg.docker.restic_version} /usr/bin/restic /usr/bin/restic
COPY --from=ghcr.io/astral-sh/uv:{_DEFAULT_DOCKER_UV_VERSION} /uv /bin/uv
{addon_copy_step}{config_copy_step}
COPY requirements/all-requirements.in.txt /tmp/all-requirements.in.txt
{constraints_copy.rstrip()}
{build_constraints_copy.rstrip()}

RUN if grep -Eq '^[[:space:]]*[^#[:space:]]' /tmp/all-requirements.in.txt; then \\
      uv pip compile --python python3 --no-cache{constraints_compile}{build_constraints_compile} /tmp/all-requirements.in.txt -o /tmp/all-requirements.lock.txt \\
      && uv pip install --system --break-system-packages --no-cache{build_constraints_install} -r /tmp/all-requirements.lock.txt; \\
    else \\
      echo "INFO: No Docker Python requirements to install."; \\
    fi \\
 && rm -f /tmp/all-requirements.in.txt /tmp/all-requirements.lock.txt{cleanup_constraints}{addon_chown_step}
{odoo_source_copy_step}{workspace_runtime_step}
USER odoo
"""
    path = _dockerfile_path(docker_context_dir)
    path.write_text(content, encoding="utf-8")
    return path


def _prepare_docker_requirements(
        layout: Layout,
        cfg: ProjectConfig,
        addon_requirement_files: list[Path],
        docker_context_dir: Path,
        copy_from: Optional[Path] = None,
) -> tuple[Path, Path, Path]:
    requirements_dir = _docker_requirements_dir(docker_context_dir)
    requirements_dir.mkdir(parents=True, exist_ok=True)

    if copy_from is not None:
        if not copy_from.is_dir():
            raise Exception(f"Docker requirements source directory not found: {copy_from}")
        _rmtree(requirements_dir)
        shutil.copytree(copy_from, requirements_dir)
        input_path = _docker_requirements_input_path(docker_context_dir)
        constraints_path = _docker_constraints_path(docker_context_dir)
        build_constraints_path = _docker_build_constraints_path(docker_context_dir)
        if not input_path.is_file():
            raise Exception(f"Copied Docker requirements are missing input file: {input_path}")
        return input_path, constraints_path, build_constraints_path

    constraints_path = _docker_constraints_path(docker_context_dir)
    if cfg.virtualenv.constraints:
        _write_constraints(
            constraints_path,
            cfg.virtualenv.constraints,
            cfg.virtualenv.explicit_requirements,
        )

    build_constraints_path = _docker_build_constraints_path(docker_context_dir)
    if cfg.virtualenv.build_constraints:
        build_constraints_path.write_text(
            "\n".join(cfg.virtualenv.build_constraints).rstrip("\n") + "\n",
            encoding="utf-8",
        )

    input_path = _docker_requirements_input_path(docker_context_dir)
    write_docker_requirements_input(
        workspace_root=layout.root,
        requirement_files=list(addon_requirement_files),
        explicit_requirements=cfg.virtualenv.explicit_requirements,
        requirements_ignore=_docker_requirements_ignore(cfg.virtualenv),
        output_path=input_path,
    )
    return input_path, constraints_path, build_constraints_path


def create_local_docker_artifacts(
        layout: Layout,
        cfg: ProjectConfig,
        addon_requirement_files: list[Path],
        source_ini: Path,
        effective_ini_sha256: str,
) -> dict[str, Any]:
    """Generate the default local-development Docker context and ROOT/compose.yaml."""
    if layout.docker_local_dir.exists():
        _rmtree(layout.docker_local_dir)
    layout.docker_local_dir.mkdir(parents=True, exist_ok=True)

    input_path, constraints_path, build_constraints_path = _prepare_docker_requirements(
        layout=layout,
        cfg=cfg,
        addon_requirement_files=addon_requirement_files,
        docker_context_dir=layout.docker_local_dir,
    )
    dockerignore_path = write_dockerignore(layout.docker_local_dir, local=True)
    docker_conf_path = write_docker_odoo_conf(
        layout.docker_local_dir,
        cfg,
        addons_path=_docker_runtime_addons_path(cfg, local=True),
        local_database=True,
    )
    dockerfile_path = write_dockerfile(
        layout.docker_local_dir,
        cfg,
        has_constraints=constraints_path.is_file(),
        has_build_constraints=build_constraints_path.is_file(),
        include_addons=False,
        include_config=False,
    )
    compose_path = write_docker_compose(layout, cfg, source_ini, effective_ini_sha256)

    _logger.info("Generated local Docker build context: %s", layout.docker_local_dir)
    _logger.info("Generated local Dockerfile: %s", dockerfile_path)
    _logger.info("Generated local Docker Odoo config: %s", docker_conf_path)

    return {
        "docker_dir": layout.docker_local_dir,
        "dockerfile": dockerfile_path,
        "dockerignore": dockerignore_path,
        "odoo_config": docker_conf_path,
        "compose": compose_path,
        "requirements_dir": _docker_requirements_dir(layout.docker_local_dir),
        "requirements_input": input_path,
        "constraints": constraints_path if constraints_path.is_file() else None,
        "build_constraints": build_constraints_path if build_constraints_path.is_file() else None,
    }


def create_deploy_docker_artifacts(
        layout: Layout,
        cfg: ProjectConfig,
        addon_requirement_files: list[Path],
        requirements_source_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Generate a self-contained Docker deployment build context under ROOT/docker/deploy/."""
    if layout.docker_deploy_dir.exists():
        _rmtree(layout.docker_deploy_dir)
    layout.docker_deploy_dir.mkdir(parents=True, exist_ok=True)

    input_path, constraints_path, build_constraints_path = _prepare_docker_requirements(
        layout=layout,
        cfg=cfg,
        addon_requirement_files=addon_requirement_files,
        docker_context_dir=layout.docker_deploy_dir,
        copy_from=requirements_source_dir,
    )
    staged_module_count = stage_docker_addons(layout, cfg, layout.docker_deploy_dir)
    staged_odoo_source = stage_docker_odoo_source(layout, cfg, layout.docker_deploy_dir)
    dockerignore_path = write_dockerignore(layout.docker_deploy_dir, local=False)
    docker_conf_path = write_docker_odoo_conf(
        layout.docker_deploy_dir,
        cfg,
        addons_path=_docker_runtime_addons_path(cfg, local=False),
    )
    dockerfile_path = write_dockerfile(
        layout.docker_deploy_dir,
        cfg,
        has_constraints=constraints_path.is_file(),
        has_build_constraints=build_constraints_path.is_file(),
        include_addons=True,
        include_config=True,
    )

    _logger.info("Generated Docker deploy build context: %s", layout.docker_deploy_dir)
    _logger.info("Generated Docker deploy Dockerfile: %s", dockerfile_path)
    _logger.info("Generated Docker deploy Odoo config: %s", docker_conf_path)

    return {
        "docker_dir": layout.docker_deploy_dir,
        "dockerfile": dockerfile_path,
        "dockerignore": dockerignore_path,
        "odoo_config": docker_conf_path,
        "requirements_dir": _docker_requirements_dir(layout.docker_deploy_dir),
        "requirements_input": input_path,
        "constraints": constraints_path if constraints_path.is_file() else None,
        "build_constraints": build_constraints_path if build_constraints_path.is_file() else None,
        "staged_module_count": staged_module_count,
        "staged_odoo_source": staged_odoo_source,
    }


# -----------------------------
# Portable workspace bundles
# -----------------------------

def _normalized_machine(value: Optional[str] = None) -> str:
    machine = (value or platform.machine() or "unknown").strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "aarch64": "arm64",
    }
    return aliases.get(machine, machine)


def _is_path_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_bundle_addon_name(name: str) -> str:
    value = (name or "").strip()
    if not value or value in {".", ".."} or Path(value).name != value or "/" in value or "\\" in value:
        raise Exception(
            f"Invalid addon name for workspace bundle: {name!r}. "
            "Addon section names must not contain path separators."
        )
    return value


def _bundle_ignore_entry(path: Path) -> bool:
    return path.name in _BUNDLE_IGNORED_NAMES or path.suffix.lower() in _BUNDLE_IGNORED_SUFFIXES


def _resolve_bundle_symlink(path: Path, source_root: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise Exception(f"Cannot resolve symlink in {label}: {path} ({e})") from e

    if not _is_path_within(resolved, source_root):
        raise Exception(
            f"Cannot create a portable bundle because {label} contains a symlink "
            f"that points outside its source tree: {path} -> {resolved}"
        )
    return resolved


def _copy_tree_for_bundle(source: Path, destination: Path, label: str) -> None:
    source = _validate_existing_dir(source, label)
    source_root = source.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    def copy_directory(current: Path, target_dir: Path, active_directories: set[Path]) -> None:
        try:
            resolved_current = current.resolve(strict=True)
        except (OSError, RuntimeError) as e:
            raise Exception(f"Cannot resolve directory in {label}: {current} ({e})") from e

        if not _is_path_within(resolved_current, source_root):
            raise Exception(
                f"Cannot create a portable bundle because {label} resolves outside its source tree: "
                f"{current} -> {resolved_current}"
            )
        if resolved_current in active_directories:
            raise Exception(
                f"Cannot create a portable bundle because {label} contains a cyclic directory symlink: {current}"
            )
        if not resolved_current.is_dir():
            raise Exception(f"Expected a directory in {label}: {current}")

        target_dir.mkdir(parents=True, exist_ok=True)
        next_active = {*active_directories, resolved_current}

        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError as e:
            raise Exception(f"Failed to list directory in {label}: {current} ({e})") from e

        for entry in entries:
            if _bundle_ignore_entry(entry):
                continue

            target = target_dir / entry.name
            if entry.is_symlink():
                resolved_entry = _resolve_bundle_symlink(entry, source_root, label)
                if resolved_entry.is_dir():
                    # Store a real directory tree in the bundle rather than a
                    # symlink. This supports common OCA setup/odoo/addons links
                    # and keeps the resulting ZIP portable across platforms.
                    copy_directory(resolved_entry, target, next_active)
                elif resolved_entry.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(resolved_entry, target)
                else:
                    raise Exception(f"Unsupported symlink target in {label}: {entry} -> {resolved_entry}")
                continue

            if entry.is_dir():
                copy_directory(entry, target, next_active)
            elif entry.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, target)
            else:
                raise Exception(f"Unsupported non-regular file in {label}: {entry}")

    copy_directory(source_root, destination, set())


def _git_snapshot(path: Path, label: str, allow_dirty: bool) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "is_git_repository": False,
        "head": None,
        "branch": None,
        "dirty": None,
    }

    if shutil.which("git") is None:
        return metadata

    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(path),
        text=True,
        capture_output=True,
    )
    if probe.returncode != 0 or (probe.stdout or "").strip().lower() != "true":
        return metadata

    metadata["is_git_repository"] = True
    metadata["head"] = _run(["git", "rev-parse", "HEAD"], cwd=path)
    branch = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=str(path),
        text=True,
        capture_output=True,
    )
    metadata["branch"] = (branch.stdout or "").strip() or None
    status = _run(["git", "status", "--porcelain", "--", "."], cwd=path)
    metadata["dirty"] = bool(status.strip())

    if metadata["dirty"] and not allow_dirty:
        raise Exception(
            f"Cannot create workspace bundle: {label} has uncommitted changes: {path}\n"
            "Commit, stash, or discard the changes, or pass --allow-dirty-bundle to bundle the current snapshot."
        )

    return metadata


def _portable_project_ini(
        ini_path: Path,
        cfg: ProjectConfig,
        vars_overrides: Optional[Dict[str, str]],
        ini_overrides: Optional[Dict[str, Dict[str, str]]],
) -> str:
    source = _read_ini(
        ini_path,
        vars_overrides=vars_overrides,
        ini_overrides=ini_overrides,
        log_overrides=False,
    )
    portable = configparser.ConfigParser(interpolation=None)

    for section in source.sections():
        if section == "odoo" or section.startswith("addons."):
            continue
        portable.add_section(section)
        for option in source._sections.get(section, {}).keys():
            value = source.get(section, option, raw=False)
            if _is_sensitive_key(option):
                value = ""
            elif _redact_url_secret(value) != value:
                value = ""
            elif section == "config" and option == "data_dir":
                value = ""
            portable.set(section, option, value)

    portable.add_section("odoo")
    portable.set("odoo", "version", cfg.odoo.version)
    portable.set("odoo", "path", "odoo")

    for addon_name in cfg.addons:
        safe_name = _safe_bundle_addon_name(addon_name)
        section = f"addons.{safe_name}"
        portable.add_section(section)
        portable.set(section, "path", f"odoo-addons/{safe_name}")

    buf = io.StringIO()
    portable.write(buf)
    return buf.getvalue()


def _source_manifest_entry(
        *,
        configured_type: str,
        repo: Optional[str],
        branch: Optional[str],
        commit: Optional[str],
        git_snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "configured_type": configured_type,
        "repo": _redact_url_secret(repo) if repo else None,
        "branch": branch,
        "configured_commit": commit,
        "snapshot": git_snapshot,
    }


def _bundle_file_manifest(staging_root: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(staging_root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(staging_root).as_posix()
        if relative == _BUNDLE_MANIFEST_NAME:
            continue
        files.append({
            "path": relative,
            "size": path.stat().st_size,
            "sha256": _sha256_file(path),
            "mode": stat.S_IMODE(path.stat().st_mode),
        })
    return files


def create_workspace_bundle(
        *,
        layout: Layout,
        cfg: ProjectConfig,
        ini_path: Path,
        output_path: Path,
        vars_overrides: Optional[Dict[str, str]],
        ini_overrides: Optional[Dict[str, Dict[str, str]]],
        allow_dirty: bool,
) -> Path:
    output_path = output_path.expanduser()
    try:
        output_path = output_path.resolve()
    except Exception:
        output_path = output_path.absolute()

    if output_path.exists() and output_path.is_dir():
        raise Exception(f"Workspace bundle output is a directory: {output_path}")

    odoo_source = _validate_local_odoo_path(layout, cfg.odoo)
    addon_sources: dict[str, Path] = {}
    for addon_name, spec in cfg.addons.items():
        safe_name = _safe_bundle_addon_name(addon_name)
        addon_sources[safe_name] = _validate_local_addon_path(layout, addon_name, spec)

    wheelhouse = _validate_existing_dir(layout.wheelhouse_dir, "Wheelhouse directory")
    lock_path = wheelhouse / "all-requirements.lock.txt"
    if not lock_path.is_file():
        raise Exception(
            f"Cannot create workspace bundle because the dependency lock is missing: {lock_path}\n"
            "Run odoo-compose-venv with --create-venv first to build a complete wheelhouse."
        )
    constraints_path = wheelhouse / "constraints.txt"
    if cfg.virtualenv.constraints and not constraints_path.is_file():
        raise Exception(
            f"Cannot create workspace bundle because constraints are missing: {constraints_path}\n"
            "Run odoo-compose-venv with --create-venv first to build a complete wheelhouse."
        )
    build_constraints_path = wheelhouse / "build-constraints.txt"
    if cfg.virtualenv.build_constraints and not build_constraints_path.is_file():
        raise Exception(
            f"Cannot create workspace bundle because build constraints are missing: {build_constraints_path}\n"
            "Run odoo-compose-venv with --create-venv first to build a complete wheelhouse."
        )

    included_roots = [odoo_source, wheelhouse, *addon_sources.values()]
    for included_root in included_roots:
        if _is_path_within(output_path, included_root):
            raise Exception(
                f"Workspace bundle output must not be written inside bundled content: {output_path}\n"
                f"Conflicting source: {included_root}"
            )

    odoo_git = _git_snapshot(odoo_source, "Odoo source", allow_dirty)
    addons_git: dict[str, dict[str, Any]] = {}
    for addon_name, source in addon_sources.items():
        addons_git[addon_name] = _git_snapshot(source, f"addon [{addon_name}]", allow_dirty)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    if temporary_output.exists():
        temporary_output.unlink()

    try:
        with tempfile.TemporaryDirectory(prefix="odt-env-bundle-") as temp_dir:
            staging_root = Path(temp_dir)
            _write_text_file(
                staging_root / _DEFAULT_PROJECT_INI_NAME,
                _portable_project_ini(
                    ini_path,
                    cfg,
                    vars_overrides=vars_overrides,
                    ini_overrides=ini_overrides,
                ),
                private=True,
            )
            _copy_tree_for_bundle(odoo_source, staging_root / "odoo", "Odoo source")
            for addon_name, source in addon_sources.items():
                _copy_tree_for_bundle(
                    source,
                    staging_root / "odoo-addons" / addon_name,
                    f"addon [{addon_name}]",
                )
            (staging_root / "odoo-addons").mkdir(parents=True, exist_ok=True)
            _copy_tree_for_bundle(wheelhouse, staging_root / "wheelhouse", "wheelhouse")

            files = _bundle_file_manifest(staging_root)
            manifest = {
                "format": _BUNDLE_FORMAT,
                "format_version": _BUNDLE_FORMAT_VERSION,
                "created_at": _utc_now_iso(),
                "created_by": {
                    "odt_env_version": __version__,
                    "host": socket.gethostname(),
                },
                "compatibility": {
                    "sys_platform": sys.platform,
                    "machine": _normalized_machine(),
                    "python_implementation": "cpython",
                    "python_version": cfg.virtualenv.python_version,
                },
                "project": {
                    "odoo_version": cfg.odoo.version,
                    "managed_python": cfg.virtualenv.managed_python,
                },
                "sources": {
                    "odoo": _source_manifest_entry(
                        configured_type="local" if cfg.odoo.is_local else "git",
                        repo=cfg.odoo.repo if not cfg.odoo.is_local else None,
                        branch=cfg.odoo.branch if not cfg.odoo.is_local else None,
                        commit=cfg.odoo.commit if not cfg.odoo.is_local else None,
                        git_snapshot=odoo_git,
                    ),
                    "addons": {
                        addon_name: _source_manifest_entry(
                            configured_type="local" if cfg.addons[addon_name].is_local else "git",
                            repo=cfg.addons[addon_name].repo,
                            branch=cfg.addons[addon_name].branch,
                            commit=cfg.addons[addon_name].commit,
                            git_snapshot=addons_git[addon_name],
                        )
                        for addon_name, source in addon_sources.items()
                    },
                },
                "files": files,
            }
            _write_text_file(
                staging_root / _BUNDLE_MANIFEST_NAME,
                json.dumps(manifest, indent=2, sort_keys=False) + "\n",
                private=True,
            )

            with zipfile.ZipFile(
                    temporary_output,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
            ) as archive:
                for path in sorted(staging_root.rglob("*"), key=lambda item: item.as_posix()):
                    arcname = path.relative_to(staging_root).as_posix()
                    if path.is_dir():
                        arcname = f"{arcname}/"
                    archive.write(path, arcname)

        os.replace(temporary_output, output_path)
    except Exception:
        try:
            temporary_output.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    _logger.info("Created portable workspace bundle: %s", output_path)
    return output_path


def _validate_bundle_member(info: zipfile.ZipInfo) -> PurePosixPath:
    raw_name = info.filename
    if not raw_name or "\\" in raw_name or raw_name.startswith("/"):
        raise Exception(f"Unsafe path in workspace bundle: {raw_name!r}")

    path = PurePosixPath(raw_name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise Exception(f"Unsafe path in workspace bundle: {raw_name!r}")
    if path.parts[0] not in _BUNDLE_ALLOWED_TOP_LEVEL:
        raise Exception(f"Unsupported top-level path in workspace bundle: {raw_name!r}")

    unix_mode = info.external_attr >> 16
    if unix_mode and stat.S_ISLNK(unix_mode):
        raise Exception(f"Symbolic links are not allowed in workspace bundles: {raw_name!r}")
    return path


def _read_bundle_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    manifest_entries = [info for info in archive.infolist() if info.filename == _BUNDLE_MANIFEST_NAME]
    if len(manifest_entries) != 1:
        raise Exception(
            f"Invalid workspace bundle: expected exactly one {_BUNDLE_MANIFEST_NAME}, "
            f"found {len(manifest_entries)}."
        )
    try:
        payload = json.loads(archive.read(manifest_entries[0]).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise Exception(f"Invalid workspace bundle manifest: {e}") from e
    if not isinstance(payload, dict):
        raise Exception("Invalid workspace bundle manifest: expected a JSON object.")
    return payload


def _validate_bundle_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("format") != _BUNDLE_FORMAT:
        raise Exception(f"Unsupported workspace bundle format: {manifest.get('format')!r}")
    if manifest.get("format_version") != _BUNDLE_FORMAT_VERSION:
        raise Exception(
            f"Unsupported workspace bundle version: {manifest.get('format_version')!r}; "
            f"supported version is {_BUNDLE_FORMAT_VERSION}."
        )

    compatibility = manifest.get("compatibility")
    if not isinstance(compatibility, dict):
        raise Exception("Invalid workspace bundle manifest: missing compatibility metadata.")

    raw_platform = compatibility.get("sys_platform")
    raw_machine = compatibility.get("machine")
    if not isinstance(raw_platform, str) or not raw_platform.strip():
        raise Exception("Invalid workspace bundle manifest: missing target platform.")
    if not isinstance(raw_machine, str) or not raw_machine.strip():
        raise Exception("Invalid workspace bundle manifest: missing target CPU architecture.")

    bundle_platform = raw_platform.strip()
    bundle_machine = _normalized_machine(raw_machine)
    current_machine = _normalized_machine()
    if bundle_platform != sys.platform or bundle_machine != current_machine:
        raise Exception(
            "Workspace bundle is not compatible with this machine.\n"
            f"Bundle: platform={bundle_platform or '<missing>'}, machine={bundle_machine or '<missing>'}\n"
            f"Current: platform={sys.platform}, machine={current_machine}"
        )


def _extract_bundle_to_staging(
        archive: zipfile.ZipFile,
        staging_root: Path,
) -> None:
    seen: set[str] = set()
    for info in archive.infolist():
        path = _validate_bundle_member(info)
        normalized = path.as_posix().rstrip("/")
        if normalized in seen:
            raise Exception(f"Duplicate path in workspace bundle: {normalized}")
        seen.add(normalized)

        target = staging_root.joinpath(*path.parts)
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info, "r") as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)

        unix_mode = stat.S_IMODE(info.external_attr >> 16)
        if unix_mode and not sys.platform.startswith("win"):
            try:
                target.chmod(unix_mode)
            except OSError:
                pass


def _verify_extracted_bundle(staging_root: Path, manifest: dict[str, Any]) -> None:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise Exception("Invalid workspace bundle manifest: 'files' must be a list.")

    expected: dict[str, dict[str, Any]] = {}
    for item in raw_files:
        if not isinstance(item, dict):
            raise Exception("Invalid workspace bundle manifest: each file entry must be an object.")
        relative = item.get("path")
        if not isinstance(relative, str):
            raise Exception("Invalid workspace bundle manifest: file path must be a string.")
        pseudo_info = zipfile.ZipInfo(relative)
        _validate_bundle_member(pseudo_info)
        if relative == _BUNDLE_MANIFEST_NAME or relative in expected:
            raise Exception(f"Invalid or duplicate file path in workspace bundle manifest: {relative}")
        expected[relative] = item

    actual = {
        path.relative_to(staging_root).as_posix()
        for path in staging_root.rglob("*")
        if path.is_file() and path.relative_to(staging_root).as_posix() != _BUNDLE_MANIFEST_NAME
    }
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        unexpected = sorted(actual - set(expected))
        raise Exception(
            "Workspace bundle contents do not match its manifest.\n"
            f"Missing: {missing or 'none'}\n"
            f"Unexpected: {unexpected or 'none'}"
        )

    for relative, item in expected.items():
        path = staging_root.joinpath(*PurePosixPath(relative).parts)
        expected_size = item.get("size")
        expected_hash = item.get("sha256")
        if not isinstance(expected_size, int) or not isinstance(expected_hash, str):
            raise Exception(f"Invalid checksum metadata for bundle file: {relative}")
        if path.stat().st_size != expected_size:
            raise Exception(f"Workspace bundle size check failed for: {relative}")
        actual_hash = _sha256_file(path)
        if actual_hash != expected_hash:
            raise Exception(f"Workspace bundle checksum failed for: {relative}")

    required = [
        staging_root / _DEFAULT_PROJECT_INI_NAME,
        staging_root / "odoo",
        staging_root / "odoo-addons",
        staging_root / "wheelhouse",
        staging_root / "wheelhouse" / "all-requirements.lock.txt",
    ]
    for path in required:
        if not path.exists():
            raise Exception(f"Workspace bundle is missing required content: {path.relative_to(staging_root)}")


def _ensure_empty_bundle_root(root: Path) -> None:
    if root.exists() and not root.is_dir():
        raise Exception(f"Workspace ROOT is not a directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    try:
        first = next(root.iterdir())
    except StopIteration:
        return
    raise Exception(
        f"Workspace ROOT must be empty when importing a bundle: {root}\n"
        f"Found existing entry: {first.name}"
    )


def extract_workspace_bundle(bundle_path: Path, root: Path) -> tuple[Path, dict[str, Any]]:
    bundle_path = bundle_path.expanduser()
    try:
        bundle_path = bundle_path.resolve()
    except Exception:
        bundle_path = bundle_path.absolute()
    if not bundle_path.is_file():
        raise Exception(f"Workspace bundle not found: {bundle_path}")

    _ensure_empty_bundle_root(root)
    moved: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix=".odt-env-import-", dir=str(root.parent)) as temp_dir:
            staging_root = Path(temp_dir)
            with zipfile.ZipFile(bundle_path, "r") as archive:
                manifest = _read_bundle_manifest(archive)
                _validate_bundle_manifest(manifest)
                _extract_bundle_to_staging(archive, staging_root)
            _verify_extracted_bundle(staging_root, manifest)

            for name in sorted(_BUNDLE_ALLOWED_TOP_LEVEL - {_BUNDLE_MANIFEST_NAME}):
                source = staging_root / name
                if not source.exists():
                    continue
                destination = root / name
                shutil.move(str(source), str(destination))
                moved.append(destination)

            imported_manifest = root / ".odt-env" / "imported-bundle-manifest.json"
            _write_text_file(
                imported_manifest,
                json.dumps(manifest, indent=2, sort_keys=False) + "\n",
                private=True,
                atomic=True,
            )
            moved.append(imported_manifest.parent)
    except Exception:
        for path in reversed(moved):
            try:
                if path.is_dir():
                    _rmtree(path)
                elif path.exists():
                    path.unlink()
            except OSError:
                pass
        raise

    ini_path = root / _DEFAULT_PROJECT_INI_NAME
    _logger.info("Imported workspace bundle: %s -> %s", bundle_path, root)
    return ini_path, manifest


def _default_bundle_output(root: Path) -> Path:
    workspace_name = root.name.strip() or "odoo-workspace"
    return root / "dist" / f"{workspace_name}{_BUNDLE_DEFAULT_SUFFIX}"


def _resolve_bundle_output(raw_output: str, root: Path) -> Path:
    if raw_output == "":
        return _default_bundle_output(root)
    candidate = Path(raw_output).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        return candidate.resolve()
    except Exception:
        return candidate.absolute()


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
        no_data_dir: bool = False,
        target: str = "docker",
        create_docker_deploy_requested: bool = False,
        bundle_output: Optional[Path] = None,
        allow_dirty_bundle: bool = False,
        vars_overrides: Optional[Dict[str, str]] = None,
        ini_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        source_ini_layers: Optional[list[str]] = None,
        project_ini_status: Optional[str] = None,
        preserved_db_name: Optional[str] = None,
) -> None:
    if target not in ("docker", "venv"):
        raise ValueError(f"Unsupported target: {target!r}")
    if target == "docker" and (create_venv or reuse_wheelhouse or bundle_output is not None):
        raise ValueError("venv provisioning and workspace bundles require odoo-compose-venv")
    if target == "venv" and create_docker_deploy_requested:
        raise ValueError("--create-docker-deploy requires odoo-compose")

    root = (root_override or ini_path.parent).resolve()
    if root.exists() and not root.is_dir():
        raise Exception(f"ROOT exists but is not a directory: {root}")
    layout = Layout.from_root(root)

    cfg = load_project_config(
        ini_path,
        vars_overrides=vars_overrides,
        ini_overrides=ini_overrides,
    )

    if target == "venv":
        db_name = str(cfg.config.get("db_name") or "").strip()
        if not db_name:
            db_name = preserved_db_name or _generated_workspace_db_name(layout.root)
            _persist_native_db_name(
                layout.root, ini_path, db_name, vars_overrides, ini_overrides,
            )
            cfg.config["db_name"] = db_name
        elif not _same_path(ini_path, layout.root / _DEFAULT_PROJECT_INI_NAME):
            # When an external local file is the source, persist the effective
            # config in ROOT as well. A newly explicit name replaces the old one.
            cp = _read_ini(
                ini_path, vars_overrides=vars_overrides,
                ini_overrides=ini_overrides, log_overrides=False,
            )
            _write_text_file(
                layout.root / _DEFAULT_PROJECT_INI_NAME,
                _ini_for_effective_file(cp), private=True, atomic=True,
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

    docker_requested = target == "docker"

    # Only explicit venv provisioning flags may create or recreate ROOT/venv.
    # Selecting the venv target alone only generates its configs and scripts.
    venv_dir = layout.root / "venv"
    venv_python = venv_dir / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
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
                    "No sync target selected; regenerating workspace configuration only (skipping venv/repo operations)."
                )

    if target == "venv":
        layout.configs_dir.mkdir(parents=True, exist_ok=True)
        layout.backups_dir.mkdir(parents=True, exist_ok=True)
        if not no_data_dir:
            layout.data_dir.mkdir(parents=True, exist_ok=True)
    layout.addons_root.mkdir(parents=True, exist_ok=True)

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
            checkout_branch(odoo_dest, cfg.odoo.branch, fetch_all=False, depth=1, already_fetched=True)
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
                checkout_branch(dest, spec.branch, fetch_all=False, depth=1, already_fetched=True)
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
        constraints_path = layout.wheelhouse_dir / "constraints.txt"
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

            if cfg.virtualenv.constraints and not constraints_path.is_file():
                raise Exception(
                    f"--create-venv-from-wheelhouse and constraints set in INI but constraints file not found: {constraints_path} "
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
            # Write dependency and build constraints to files.
            if cfg.virtualenv.constraints:
                _write_constraints(
                    constraints_path,
                    cfg.virtualenv.constraints,
                    cfg.virtualenv.explicit_requirements,
                )
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
                constraints_path=constraints_path,
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

    local_docker_artifacts: Optional[dict[str, Any]] = None
    deploy_docker_artifacts: Optional[dict[str, Any]] = None

    if target == "docker":
        _logger.info("Generating local-development Docker artifacts.")
        effective_ini = _ini_for_effective_file(_read_ini(
            ini_path,
            vars_overrides=vars_overrides,
            ini_overrides=ini_overrides,
            log_overrides=False,
        ))
        local_docker_artifacts = create_local_docker_artifacts(
            layout=layout,
            cfg=cfg,
            addon_requirement_files=addon_req_files,
            source_ini=ini_path,
            effective_ini_sha256=hashlib.sha256(effective_ini.encode("utf-8")).hexdigest(),
        )
    else:
        _logger.info("Skipping Docker artifacts (target=venv).")

    if create_docker_deploy_requested:
        _logger.info("Generating Docker deploy build context.")
        requirements_source_dir = (
            local_docker_artifacts.get("requirements_dir")
            if local_docker_artifacts is not None
            else None
        )
        deploy_docker_artifacts = create_deploy_docker_artifacts(
            layout=layout,
            cfg=cfg,
            addon_requirement_files=addon_req_files,
            requirements_source_dir=requirements_source_dir,
        )

    # Generate config (unless disabled).
    if target == "venv" and not no_configs:
        addon_paths: list[Path] = [
            _validate_local_addon_path(layout, addon_name, spec)
            for addon_name, spec in cfg.addons.items()
        ]
        conf_text = render_odoo_conf(cfg.config, layout, addon_paths)
        layout.conf_path.write_text(conf_text, encoding="utf-8")
    else:
        _logger.info("Skipping native config generation (target=%s, --no-configs=%s).", target, no_configs)

    # Remove obsolete generated helpers when upgrading an existing native workspace.
    if target == "venv":
        for script_name in (*_BACKUP_RESTORE_SCRIPT_NAMES, "neutralize-db", "run", "shell", "update", "test", "instance"):
            for ext in ("sh", "bat"):
                (layout.root / "odoo-scripts" / f"{script_name}.{ext}").unlink(missing_ok=True)

    bundle_created: Optional[Path] = None
    if bundle_output is not None:
        bundle_created = create_workspace_bundle(
            layout=layout,
            cfg=cfg,
            ini_path=ini_path,
            output_path=bundle_output,
            vars_overrides=vars_overrides,
            ini_overrides=ini_overrides,
            allow_dirty=allow_dirty_bundle,
        )

    synced: list[str] = []
    if sync_odoo:
        synced.append("odoo")
    if sync_addons:
        synced.append("addons")

    _logger.info("OK")
    _logger.info(f"  Version:            {__version__}")
    if synced:
        synced_label = ", ".join(synced)
    else:
        synced_label = "none"

    generated: list[str] = []
    if target == "venv" and not no_configs:
        generated.append("configs")
    if target == "docker":
        generated.append("docker/local + compose")
    if create_docker_deploy_requested:
        generated.append("docker/deploy")
    if generated:
        synced_label = f"{synced_label} (generated: {', '.join(generated)})"
    else:
        synced_label = f"{synced_label} (no generated workspace artifacts)"

    _logger.info(f"  Synced:             {synced_label}")
    _logger.info(f"  ROOT:               {layout.root}")
    _logger.info(f"  Odoo:               {layout.odoo_dir}")
    _logger.info(f"  Addons:             {layout.addons_root}")
    if target == "venv":
        _logger.info(f"  Backups:            {layout.backups_dir}")
        if no_data_dir:
            _logger.info("  Data:               SKIPPED (--no-data-dir)")
        else:
            _logger.info(f"  Data:               {layout.data_dir}")
    else:
        _logger.info("  Native Data/Backups: SKIPPED (target=docker)")
    if target == "docker":
        _logger.info("  Native Config:      SKIPPED (target=docker)")
    elif no_configs:
        _logger.info(f"  Config:             SKIPPED (--no-configs) [{layout.conf_path}]")
    else:
        _logger.info(f"  Config:             {layout.conf_path}")
    if target == "venv" and (venv_py is not None or venv_python.is_file()):
        _logger.info(f"  Venv:               {layout.root / 'venv'}")
        lock_path = layout.wheelhouse_dir / "all-requirements.lock.txt"
        if lock_path.exists():
            _logger.info(f"  Requirements:       {lock_path}")
            _logger.info(f"  Wheelhouse:         {layout.wheelhouse_dir}")
        if cfg.virtualenv.constraints:
            constraints_path = layout.wheelhouse_dir / "constraints.txt"
            if constraints_path.exists():
                _logger.info(f"  Constraints:        {constraints_path}")
        if cfg.virtualenv.build_constraints:
            bc = layout.wheelhouse_dir / "build-constraints.txt"
            if bc.exists():
                _logger.info(f"  Build Constraints:  {bc}")

    if bundle_created is not None:
        _logger.info(f"  Bundle:            {bundle_created}")

    if local_docker_artifacts is not None:
        _logger.info(f"  Docker Local:       {layout.docker_local_dir}")
        _logger.info(f"  Dockerfile:         {_dockerfile_path(layout.docker_local_dir)}")
        docker_conf_path = _docker_odoo_conf_path(layout.docker_local_dir)
        if docker_conf_path.exists():
            _logger.info(f"  Docker Config:      {docker_conf_path}")
        docker_compose_path = _docker_compose_path(layout)
        if docker_compose_path.exists():
            _logger.info(f"  Docker Compose:     {docker_compose_path}")
        docker_requirements_input = _docker_requirements_input_path(layout.docker_local_dir)
        if docker_requirements_input.exists():
            _logger.info(f"  Docker Requirements: {docker_requirements_input}")
    elif target == "venv":
        _logger.info(f"  Docker Local:       SKIPPED (target=venv) [{layout.docker_local_dir}]")

    if deploy_docker_artifacts is not None:
        _logger.info(f"  Docker Deploy:      {layout.docker_deploy_dir}")
        _logger.info(f"  Deploy Dockerfile:  {_dockerfile_path(layout.docker_deploy_dir)}")
        deploy_conf_path = _docker_odoo_conf_path(layout.docker_deploy_dir)
        if deploy_conf_path.exists():
            _logger.info(f"  Deploy Config:      {deploy_conf_path}")
        deploy_requirements_input = _docker_requirements_input_path(layout.docker_deploy_dir)
        if deploy_requirements_input.exists():
            _logger.info(f"  Deploy Requirements: {deploy_requirements_input}")
        staged_modules = deploy_docker_artifacts.get("staged_module_count", 0)
        _logger.info(f"  Deploy Addons:      {staged_modules} staged module(s)")
        build_cmd = ["docker", "build"]
        build_cmd.append(_compose_host_path(layout, layout.docker_deploy_dir))
        _logger.info(f"  Deploy Build:       {_format_cmd(build_cmd)}")

    if project_ini_status:
        _logger.info("  Project INI:")
        _logger.info("  - %s", project_ini_status)

    effective_source_ini_layers = list(source_ini_layers or [str(ini_path)])
    if effective_source_ini_layers:
        _logger.info("  Source INI:")
        for layer_idx, source_ini_layer in enumerate(effective_source_ini_layers):
            _logger.info("  - layer #%s: %s", layer_idx+1, source_ini_layer)


def sync_project(
        ini_path: Path,
        sync_odoo: bool,
        sync_addons: bool,
        root_override: Optional[Path] = None,
        reuse_wheelhouse: bool = False,
        create_venv: bool = False,
        clear_pip_wheel_cache: bool = False,
        no_configs: bool = False,
        no_data_dir: bool = False,
        target: str = "docker",
        create_docker_deploy_requested: bool = False,
        bundle_output: Optional[Path] = None,
        allow_dirty_bundle: bool = False,
        vars_overrides: Optional[Dict[str, str]] = None,
        ini_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        no_provisioning_log: bool = False,
        cli_argv: Optional[list[str]] = None,
        source_ini_layers: Optional[list[str]] = None,
        project_ini_status: Optional[str] = None,
        preserved_db_name: Optional[str] = None,
        source_snapshot_ready: bool = False,
) -> None:
    root = (root_override or ini_path.parent).resolve()
    layout = Layout.from_root(root)
    _refresh_last_ini_snapshots(
        layout, ini_path, vars_overrides, ini_overrides,
        source_snapshot_ready=source_snapshot_ready,
    )

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
            no_data_dir=no_data_dir,
            target=target,
            create_docker_deploy_requested=create_docker_deploy_requested,
            bundle_output=bundle_output,
            allow_dirty_bundle=allow_dirty_bundle,
            vars_overrides=vars_overrides,
            ini_overrides=ini_overrides,
            source_ini_layers=source_ini_layers,
            project_ini_status=project_ini_status,
            preserved_db_name=preserved_db_name,
        )
        if target == "venv":
            workspace_ini = root / _DEFAULT_PROJECT_INI_NAME
            if workspace_ini.is_file():
                _save_resolved_project_ini_copy(
                    workspace_ini,
                    _ini_for_effective_file(_read_ini(workspace_ini, log_overrides=False)),
                )
        return

    run_id = _safe_run_id()
    started_at = _utc_now_iso()
    argv = _redact_cli_args(cli_argv or sys.argv)
    run_path, source_ini_path, resolved_ini_path, last_path = _provisioning_paths(layout, run_id)
    effective_source_ini_layers = list(source_ini_layers or [_path_for_report(ini_path)])

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
            "project_ini_status": project_ini_status,
            "source_ini_layers": _redact_mapping(effective_source_ini_layers),
        },
        "options": {
            "sync_odoo": sync_odoo,
            "sync_addons": sync_addons,
            "reuse_wheelhouse": reuse_wheelhouse,
            "create_venv": create_venv,
            "clear_pip_wheel_cache": clear_pip_wheel_cache,
            "no_configs": no_configs,
            "no_data_dir": no_data_dir,
            "target": target,
            "create_docker_deploy": create_docker_deploy_requested,
            "create_bundle": str(bundle_output) if bundle_output is not None else None,
            "allow_dirty_bundle": allow_dirty_bundle,
        },
        "vars_overrides": _redact_mapping(vars_overrides or {}),
        "ini_overrides": _redact_mapping(ini_overrides or {}),
        "config": None,
        "artifacts": {
            "manifest": str(run_path),
            "last_manifest": str(last_path),
            "source_ini": None,
            "resolved_ini": None,
            "bundle": str(bundle_output) if bundle_output is not None else None,
        },
        "error": None,
    }

    _write_provisioning_record(layout, run_id, record)

    copied_source_ini_path = _copy_source_ini_for_manifest(
        layout, run_id, layout.root / ".odt-env" / "last-source-project.ini",
    )
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
            no_data_dir=no_data_dir,
            target=target,
            create_docker_deploy_requested=create_docker_deploy_requested,
            bundle_output=bundle_output,
            allow_dirty_bundle=allow_dirty_bundle,
            vars_overrides=vars_overrides,
            ini_overrides=ini_overrides,
            source_ini_layers=effective_source_ini_layers,
            project_ini_status=project_ini_status,
            preserved_db_name=preserved_db_name,
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

    effective_ini = (root / _DEFAULT_PROJECT_INI_NAME) if target == "venv" else ini_path
    if target == "venv" and effective_ini.is_file():
        effective_resolved = _resolved_ini_for_manifest(effective_ini, None, None)
        if effective_resolved is not None:
            _write_text_file(resolved_ini_path, effective_resolved, private=True, atomic=True)
            _save_resolved_project_ini_copy(
                effective_ini,
                _ini_for_effective_file(_read_ini(effective_ini, log_overrides=False)),
            )

    cfg = load_project_config(
        effective_ini if effective_ini.is_file() else ini_path,
        vars_overrides=vars_overrides if target != "venv" else None,
        ini_overrides=ini_overrides if target != "venv" else None,
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

def _match_git_ini_prefix(raw_value: str) -> Optional[str]:
    """Return the supported Git-backed INI prefix used by ``raw_value``."""
    if raw_value.startswith(_GIT_INI_PREFIX):
        return _GIT_INI_PREFIX
    return None


def _parse_git_ini_source(raw_ini: str) -> Optional[GitIniSource]:
    """Parse a Git-backed remote INI source.

    Supported syntax:
      git+REPO_URL//PATH/TO/PROJECT.ini?ref=BRANCH_OR_TAG_OR_COMMIT

    The path after ``//`` is a repository-internal path and therefore uses
    POSIX separators on every operating system.
    """
    raw_value = (raw_ini or "").strip()
    git_prefix = _match_git_ini_prefix(raw_value)
    if git_prefix is None:
        return None

    source_value = raw_value[len(git_prefix):].strip()
    if not source_value:
        raise Exception(
            "Invalid Git INI source: missing repository URL after 'git+'."
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
            "Invalid Git INI source: expected 'git+REPO_URL//PATH/TO/PROJECT.ini'."
        )

    repo, raw_path = source_without_query.rsplit("//", 1)
    repo = repo.strip()
    raw_path = raw_path.strip()

    # ``https://...`` without the repository-internal ``//path`` would otherwise
    # split at the URL scheme separator and look superficially valid.
    if not repo or repo.endswith(":") or not raw_path:
        raise Exception(
            "Invalid Git INI source: expected 'git+REPO_URL//PATH/TO/PROJECT.ini'."
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
        headers={"User-Agent": f"odoo-compose/{__version__}"},
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
    if re.search(r"<\s*!doctype\s+html|<\s*html[\s>]", text[:1024], flags=re.IGNORECASE):
        _logger.info("Downloaded INI (first 1024 chars): %s", text[:1024])
        raise Exception(
            "Downloaded INI looks like an HTML page, not a raw INI file. "
            "Use a raw file URL instead."
        )

    probe = configparser.ConfigParser(interpolation=_IniInterpolation())
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


def _redact_ini_source_for_report(raw_source: str) -> str:
    """Redact secrets in local/URL/Git INI source labels shown in logs and manifests."""
    raw_value = (raw_source or "").strip()
    git_prefix = _match_git_ini_prefix(raw_value)
    if git_prefix is not None:
        source_value = raw_value[len(git_prefix):]
        source_without_query, query_sep, query = source_value.partition("?")
        if "//" in source_without_query:
            repo, raw_path = source_without_query.rsplit("//", 1)
            redacted = f"{_GIT_INI_PREFIX}{_redact_url_secret(repo)}//{raw_path}"
            if query_sep:
                redacted = f"{redacted}?{query}"
            return redacted
    return _redact_url_secret(raw_value)


def _describe_cli_ini_source_for_report(parser: argparse.ArgumentParser, raw_ini: str) -> str:
    """Return the human-facing INI source label, preserving source order."""
    raw_value = (raw_ini or "").strip()
    source_kind = _source_kind_for_cli_ini(parser, raw_value)
    if source_kind == "local":
        return _path_for_report(_resolve_local_ini_path(parser, raw_value))
    return _redact_ini_source_for_report(raw_value)


def _explicit_source_uses_default_project_ini(
        parser: argparse.ArgumentParser,
        raw_sources: list[str],
        default_ini_path: Path,
) -> Optional[int]:
    """Return 1-based layer index if ROOT/odoo-project.ini is explicitly listed."""
    for index, raw_source in enumerate(raw_sources, start=1):
        raw_value = (raw_source or "").strip()
        if not raw_value:
            continue
        if _source_kind_for_cli_ini(parser, raw_value) != "local":
            continue
        source_path = _resolve_local_ini_path(parser, raw_value)
        if _same_path(source_path, default_ini_path):
            return index
    return None


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

    cp = configparser.ConfigParser(interpolation=_IniInterpolation())
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
        if vars_overrides or ini_overrides:
            _save_workspace_ini_overrides(
                output_path, output_path, vars_overrides, ini_overrides,
            )
        else:
            _save_effective_ini_copy(
                output_path,
                vars_overrides=vars_overrides,
                ini_overrides=ini_overrides,
            )
        return output_path


def build_parser(prog: str = "odoo-compose", target: str = "docker", *, setup_command: bool = False) -> argparse.ArgumentParser:
    """Build the top-level command list or the full workspace setup parser."""
    if not setup_command:
        parser = argparse.ArgumentParser(
            prog=prog,
            description=f"{prog} {__version__} — build reproducible Odoo workspaces",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=("Commands:\n" + "\n".join(
                f"  {name:<27} {description}" for name, description in _CLI_COMMANDS
            ) + f"\n\nUse '{prog} COMMAND --help' for command-specific help.\n"),
        )
        parser.add_argument("--version", action="version", version=f"{prog} {__version__}")
        return parser

    if target == "docker":
        epilog = """Create or update an Odoo Docker workspace.

Examples:
  odoo-compose setup ./odoo19 --sync
  odoo-compose setup --sync --create-docker-deploy
  odoo-compose setup ./odoo-project.ini --show-last-run
"""
    else:
        epilog = """Create or update a native Odoo workspace.
The virtual environment is recreated only when explicitly requested.

Examples:
  odoo-compose-venv setup ./odoo19 --sync --create-venv
  odoo-compose-venv setup -i base.ini -i addons.ini --sync --create-venv
  odoo-compose-venv setup --create-from-bundle ./workspace.odt.zip --root ./restored
"""

    parser = argparse.ArgumentParser(
        prog=prog,
        description=f"{prog} {__version__} — create or update an Odoo workspace",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    parser.add_argument(
        "source", nargs="?", metavar="ROOT|INI",
        help=("Workspace directory (created if needed), an existing local INI file, "
              "a git+REPO_URL//PATH.ini?ref=REF source, or a remote INI URL. "
              "An existing file or a path ending in .ini is treated as an INI source."),
    )
    parser.add_argument(
        "-i", "--include", dest="include_inis", action="append", default=[], metavar="INI",
        help="Include an additional project INI layer; repeat for multiple layers.",
    )
    parser.add_argument(
        "--root",
        metavar="ROOT",
        default=None,
        help=(
            "Override workspace ROOT directory. "
            "When INI is omitted, ROOT defaults to the current working directory. "
            "Explicit ROOT is created automatically if needed."
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
            "Set or override a supported INI option. Missing supported sections/options are created. "
            f"Can be passed multiple times. Example: --set {'docker:odoo_source=workspace' if target == 'docker' else 'odoo:version=19.0'}"
        ),
    )

    sync_group = parser.add_mutually_exclusive_group()
    sync_group.add_argument(
        "--sync", action="store_true",
        help=("Sync Git sources used by this workflow: Odoo for native venv or Docker "
              "odoo_source=workspace, plus configured Git addons"),
    )
    sync_group.add_argument("--sync-odoo", dest="odoo", action="store_true", help="Sync only Odoo repository")
    sync_group.add_argument("--sync-addons", dest="addons", action="store_true", help="Sync only addon repositories")
    sync_group.add_argument("--sync-all", dest="all", action="store_true", help="Sync Odoo + addons")

    # Keep the Namespace shape compatible with the shared provisioning code,
    # even though the two commands expose different CLI options.
    parser.set_defaults(
        create_venv=False,
        create_venv_from_wheelhouse=False,
        clear_pip_wheel_cache=False,
        create_bundle=None,
        allow_dirty_bundle=False,
        create_from_bundle=None,
        create_docker_deploy=False,
        no_configs=False,
    )

    if target == "venv":
        parser.add_argument(
            "--create-venv",
            action="store_true",
            help=(
                "odoo-compose-venv only: force recreation of ROOT/venv and refresh wheelhouse. "
                "If ROOT/venv already exists, it is deleted and created again. "
            ),
        )
        parser.add_argument(
            "--create-venv-from-wheelhouse",
            action="store_true",
            help=(
                "Offline deployment helper: recreate ROOT/venv from existing ROOT/wheelhouse, then install strictly offline. "
                "Useful after copying a prepared workspace to a target machine without internet access."
            ),
        )
        parser.add_argument(
            "--clear-pip-wheel-cache",
            action="store_true",
            help="Remove all items from the pip's wheel cache.",
        )

        parser.add_argument(
            "--create-bundle",
            nargs="?",
            const="",
            default=None,
            metavar="BUNDLE",
            help=(
                "Create a portable ZIP containing Odoo sources, configured addon sources, "
                "a sanitized project INI, and the offline wheelhouse. When BUNDLE is omitted, "
                "write ROOT/dist/ROOT-NAME.odt.zip."
            ),
        )
        parser.add_argument(
            "--allow-dirty-bundle",
            action="store_true",
            help=(
                "Allow --create-bundle to snapshot Git repositories with uncommitted changes. "
                "The dirty state is recorded in the bundle manifest."
            ),
        )
        parser.add_argument(
            "--create-from-bundle",
            metavar="BUNDLE",
            default=None,
            help=(
                "Create an empty workspace from a portable bundle, verify its manifest and checksums, "
                "then recreate ROOT/venv strictly from the bundled wheelhouse. "
                "Local-development Docker artifacts are intentionally not generated in this offline workflow."
            ),
        )

        parser.add_argument(
            "--no-configs",
            action="store_true",
            help="Do not (re)generate native config files (odoo-compose-venv only).",
        )
        parser.add_argument(
            "--no-data-dir",
            action="store_true",
            help="Do not generate odoo data folder.",
        )
    else:
        parser.add_argument(
            "--create-docker-deploy",
            action="store_true",
            help=(
                "Generate a self-contained Docker deployment build context under ROOT/docker/deploy/. "
                "The image itself is built separately with docker build or by CI/CD."
            ),
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
            "Print ROOT/.odt-env/last-provisioning.json to stdout and exit without provisioning."
        ),
    )

    return parser


def _validate_root_override(
        parser: argparse.ArgumentParser,
        raw_root: str,
        option_name: str = "--root",
) -> Path:
    """Validate, normalize, and prepare an explicit workspace ROOT override.

    - Expands '~'
    - Resolves to an absolute path
    - Creates the directory if it does not exist
    - Ensures it is a directory

    Returns the normalized Path.
    """
    _logger.info('CLI %s provided: %s', option_name, raw_root)

    candidate = Path(raw_root).expanduser()

    # Resolve to an absolute path for consistent workspace layout and logging.
    try:
        resolved = candidate.resolve()
    except Exception:
        # Fallback: make it absolute without resolving symlinks.
        resolved = candidate.absolute()

    if resolved.exists() and not resolved.is_dir():
        parser.error(f'{option_name} path is not a directory: {resolved}')

    if not resolved.exists():
        try:
            _logger.info('Creating %s directory: %s', option_name, resolved)
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            parser.error(f'Failed to create {option_name} directory {resolved}: {e}')

    if not resolved.is_dir():
        parser.error(f'{option_name} path is not a directory: {resolved}')

    _logger.info('Validated %s: %s', option_name, resolved)
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


def _root_for_show_last_run(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Path:
    """Resolve ROOT for --show-last-run without materializing or parsing project INI sources."""
    if getattr(args, "include_inis", None):
        parser.error(
            "--include/-i cannot be used together with --show-last-run; "
            "use --root or a local positional INI path."
        )

    raw_root = getattr(args, "root", None)
    if raw_root:
        root_candidate = Path(raw_root).expanduser()
    else:
        raw_ini = (getattr(args, "ini", None) or "").strip()
        if raw_ini:
            git_ini_source, url_ini_source = _parse_remote_ini_sources(parser, raw_ini)
            if git_ini_source is not None or url_ini_source is not None:
                parser.error(
                    "Remote INI sources cannot be used with --show-last-run; "
                    "use --root to select the workspace."
                )

            ini_path = _resolve_local_ini_path(parser, raw_ini)
            root_candidate = ini_path.parent
        else:
            root_candidate = Path.cwd()

    try:
        root = root_candidate.resolve()
    except Exception:
        root = root_candidate.absolute()

    if not root.exists():
        parser.error(f"ROOT path does not exist: {root}")
    if not root.is_dir():
        parser.error(f"ROOT path is not a directory: {root}")

    return root


class _NeutralizeSqlError(Exception):
    """Invalid or unresolved SQL substitution."""


def _neutralize_sql_literal(value: str) -> str:
    """Render one PostgreSQL text literal, independent of string settings."""
    if "\x00" in value:
        raise _NeutralizeSqlError("NUL bytes are not valid PostgreSQL text")
    escaped = (value.replace("\\", "\\\\").replace("'", "''")
               .replace("\r", "\\r").replace("\n", "\\n"))
    return "E'" + escaped + "'"


def _render_neutralize_sql(sql: str, cp: configparser.ConfigParser) -> str:
    """Resolve ${section:option} using the existing INI parser; quote SQL values."""
    output: list[str] = []
    cursor = 0
    while True:
        pos = sql.find("${", cursor)
        if pos < 0:
            output.append(sql[cursor:])
            break
        prefix = sql[cursor:pos]
        slash_count = len(prefix) - len(prefix.rstrip("\\"))
        output.append(prefix[:-slash_count] if slash_count else prefix)
        output.append("\\" * (slash_count // 2))
        end = sql.find("}", pos + 2)
        if slash_count % 2:
            # The reference is literal, even if it names a missing variable.
            if end < 0:
                output.append("${")
                cursor = pos + 2
            else:
                output.append(sql[pos:end + 1])
                cursor = end + 1
            continue
        if end < 0:
            raise _NeutralizeSqlError("unclosed interpolation reference")
        ref = sql[pos + 2:end]
        if ref.count(":") != 1 or not all(ref.split(":")):
            raise _NeutralizeSqlError("invalid interpolation reference")
        section, option = ref.split(":", 1)
        if not cp.has_section(section) or not cp.has_option(section, option):
            raise _NeutralizeSqlError("missing interpolation reference")
        # _IniInterpolation also resolves nested references.
        value = cp.get(section, option)
        output.append(_neutralize_sql_literal(value))
        cursor = end + 1
    return "".join(output)


def _run_neutralize_command(
        command: list[str], root: Path, *, env: Optional[dict[str, str]] = None,
        input_sql: Optional[str] = None,
) -> None:
    """Run an Odoo/SQL command without leaking database output or substituted values."""
    try:
        result = subprocess.run(
            command, cwd=root, env=env,
            input=input_sql.encode("utf-8") if input_sql is not None else None,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    except OSError as exc:
        raise _NeutralizeSqlError("could not launch neutralization command") from exc
    if result.returncode:
        raise _NeutralizeSqlError(f"neutralization command failed (exit {result.returncode})")


def _build_neutralize_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{prog} neutralize",
        description="Neutralize an existing workspace database or preview interpolated workspace SQL.",
    )
    parser.add_argument("--root", default=".", metavar="ROOT", help="Workspace directory (default: current directory).")
    parser.add_argument(
        "--dry-run", nargs="?", const="", default=None, metavar="OUTPUT_DIR",
        help="Only save interpolated neutralize/*.sql to OUTPUT_DIR; default: .odt-env/neutralize-preview.",
    )
    return parser


def _run_neutralize_cli(target: str, args: list[str], prog: str) -> int:
    """Neutralize an existing workspace directly from the installed CLI."""
    parser = _build_neutralize_parser(prog)
    opts = parser.parse_args(args)
    root = Path(opts.root).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"workspace ROOT does not exist: {root}")
    ini_path = root / _DEFAULT_PROJECT_INI_NAME
    if not ini_path.is_file():
        parser.error(f"workspace project file not found: {ini_path}")

    try:
        cfg = load_project_config(ini_path, log_resolved=False, log_overrides=False)
        db_name = _docker_default_db_name(cfg) if target == "docker" else _native_default_db_name(cfg)
        version = _parse_odoo_version(cfg.odoo.version)
        cp = _read_ini(ini_path, log_overrides=False)
        # Resolve every SQL file before touching the database. Never change sources.
        sql_files = sorted((root / "neutralize").glob("*.sql"))
        sql_contents = [
            (path, _render_neutralize_sql(path.read_text(encoding="utf-8"), cp))
            for path in sql_files if path.is_file()
        ]

        if opts.dry_run is not None:
            directory = Path(opts.dry_run).expanduser() if opts.dry_run else Path(".odt-env/neutralize-preview")
            if not directory.is_absolute():
                directory = root / directory
            directory = directory.resolve()
            for source, _ in sql_contents:
                if (directory / source.name).resolve() == source.resolve():
                    raise _NeutralizeSqlError("preview directory cannot overwrite source SQL")
            # Preview may contain secrets: create files atomically with mode 0600.
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            for source, sql in sql_contents:
                temp_path = None
                try:
                    fd, name = tempfile.mkstemp(prefix=".neutralize-", dir=directory)
                    temp_path = Path(name)
                    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                        stream.write(sql)
                    os.replace(temp_path, directory / source.name)
                finally:
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)
                _logger.info("Prepared neutralization SQL: %s", source.name)
            _logger.info("SQL preview saved to: %s", directory)
            return 0

        if target == "docker":
            docker_name = "docker.exe" if os.name == "nt" else "docker"
            docker = [shutil.which(docker_name) or docker_name]
            if os.name != "nt" and os.environ.get("ODOO_DOCKER_SUDO") == "1":
                docker.insert(0, "sudo")
            pg_base = docker + ["compose", "exec", "-T", "db", "psql"]
            pg_query = pg_base + ["-U", "odoo", "-d", "postgres", "-Atqc",
                                  "SELECT datname FROM pg_database"]
            pg_execute = pg_base + ["-v", "ON_ERROR_STOP=1", "-U", "odoo", "-d", db_name]
            env = None
        else:
            bin_dir = os.environ.get("POSTGRES_BIN", "")
            psql = str(Path(bin_dir) / ("psql.exe" if os.name == "nt" else "psql")) if bin_dir else "psql"
            pg_query = [psql, "-X", "-d", "postgres", "-Atqc", "SELECT datname FROM pg_database"]
            pg_execute = [psql, "-X", "-v", "ON_ERROR_STOP=1", "-d", db_name]
            env = {**os.environ, **_native_pg_env(cfg)}

        try:
            databases = subprocess.run(
                pg_query, cwd=root, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, check=False,
            )
        except OSError as exc:
            raise _NeutralizeSqlError(
                f"could not check target database ({type(exc).__name__})"
            ) from exc
        if databases.returncode:
            raise _NeutralizeSqlError(
                f"could not check target database (exit code {databases.returncode})"
            )
        if db_name.encode("utf-8") not in databases.stdout.splitlines():
            raise _NeutralizeSqlError(f"target database does not exist: {db_name}")

        _logger.info("Neutralizing Odoo database: %s", db_name)
        if version >= 16:
            if target == "docker":
                command = docker + ["compose", "run", "--rm", "--no-deps", "-T",
                                    "--entrypoint", "odoo", "odoo", "neutralize",
                                    "-c", "/etc/odoo/odoo.conf", "-d", db_name]
            else:
                executable = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                layout = Layout.from_root(root)
                odoo_bin = _resolve_odoo_path(layout, cfg.odoo) / "odoo-bin"
                conf = layout.conf_path
                if not executable.is_file() or not odoo_bin.is_file() or not conf.is_file():
                    raise _NeutralizeSqlError("native Odoo neutralization requires venv python, odoo-bin and Odoo config")
                command = [str(executable), str(odoo_bin), "neutralize", "-c", str(conf), "-d", db_name]
            _logger.info("Using native Odoo neutralization for Odoo %s", version)
            _run_neutralize_command(command, root, env=env)
        else:
            _logger.info("Using legacy minimal neutralization for Odoo %s", version)
            legacy_sql = (
                "UPDATE ir_cron SET active = FALSE WHERE active = TRUE;",
                "UPDATE ir_mail_server SET active = FALSE WHERE active = TRUE;",
                "DO $$ BEGIN IF to_regclass('fetchmail_server') IS NOT NULL THEN "
                "UPDATE fetchmail_server SET active = FALSE WHERE active = TRUE; END IF; END $$;",
            )
            for sql in legacy_sql:
                _run_neutralize_command(pg_execute + ["-c", sql], root, env=env)

        for source, sql in sql_contents:
            _logger.info("Running workspace neutralization SQL: %s", source.name)
            _run_neutralize_command(pg_execute, root, env=env, input_sql=sql)
        _logger.info("Database neutralization completed: %s", db_name)
        return 0
    except (OSError, UnicodeError, configparser.Error, ValueError, _NeutralizeSqlError) as exc:
        # SQL content and resolved INI values must never appear in errors/logs.
        if isinstance(exc, _NeutralizeSqlError):
            _logger.error("%s", exc)
        else:
            _logger.error("Could not process workspace neutralization configuration")
        return 1
    except Exception:
        _logger.error("Could not complete database neutralization")
        return 1


class _WorkspaceOperationError(Exception):
    """Expected backup, restore or Restic failure."""


def _workspace_command(
        command: list[str], root: Path, *, env: Optional[dict[str, str]] = None,
        stdin=None, stdout=None, capture: bool = False,
) -> bytes:
    """Run an external command without accidentally printing credentials."""
    try:
        process = subprocess.run(
            command, cwd=root, env=env, stdin=stdin,
            stdout=subprocess.PIPE if capture else stdout,
            stderr=subprocess.DEVNULL if capture else None, check=False,
        )
    except OSError as exc:
        raise _WorkspaceOperationError(f"could not execute {Path(command[0]).name} ({type(exc).__name__})") from exc
    if process.returncode:
        raise _WorkspaceOperationError(
            f"{Path(command[0]).name} exited with status {process.returncode}"
        )
    return process.stdout or b"" if capture else b""


def _workspace_docker_command() -> list[str]:
    name = "docker.exe" if os.name == "nt" else "docker"
    command = [shutil.which(name) or name]
    if os.name != "nt" and os.environ.get("ODOO_DOCKER_SUDO") == "1":
        command.insert(0, "sudo")
    return command


def _workspace_postgres_command(program: str) -> str:
    bin_dir = os.environ.get("POSTGRES_BIN", "")
    executable = program + (".exe" if os.name == "nt" else "")
    return str(Path(bin_dir) / executable) if bin_dir else executable


def _workspace_db_command(target: str, program: str, *args: str) -> list[str]:
    if target == "docker":
        return (_workspace_docker_command() + ["compose", "exec", "-T", "db", program,
                "-U", "odoo", *args])
    return [_workspace_postgres_command(program), *args]


def _workspace_db_exists(target: str, root: Path, env: Optional[dict[str, str]], db_name: str) -> bool:
    if target == "docker":
        cmd = _workspace_docker_command() + ["compose", "exec", "-T", "db", "psql",
                                             "-U", "odoo", "-d", "postgres", "-Atqc",
                                             "SELECT datname FROM pg_database"]
    else:
        cmd = [_workspace_postgres_command("psql"), "-X", "-d", "postgres", "-Atqc",
               "SELECT datname FROM pg_database"]
    databases = _workspace_command(cmd, root, env=env, capture=True)
    return db_name.encode("utf-8") in databases.splitlines()


def _workspace_restic_command(target: str, root: Path, args: list[str]) -> list[str]:
    repository = os.environ.get("RESTIC_REPOSITORY", "")
    if not repository:
        raise _WorkspaceOperationError("RESTIC_REPOSITORY is not set")
    password_file = os.environ.get("RESTIC_PASSWORD_FILE", "")
    if password_file:
        password_file_path = Path(password_file).expanduser().resolve()
        if not password_file_path.is_file():
            raise _WorkspaceOperationError("RESTIC_PASSWORD_FILE does not exist")
    elif not os.environ.get("RESTIC_PASSWORD"):
        raise _WorkspaceOperationError("set RESTIC_PASSWORD_FILE or RESTIC_PASSWORD")

    if target == "venv":
        command = os.environ.get("RESTIC_BIN", "restic")
        return [shutil.which(command) or command, *args]

    cmd = _workspace_docker_command() + [
        "compose", "run", "--rm", "--no-deps", "-T", "--user", "0:0",
        "-e", "RESTIC_CACHE_DIR=/var/lib/odoo/.cache/restic",
    ]
    # Docker Desktop requires Windows host paths to be mounted at Linux paths.
    local_repository = (Path(repository).is_absolute() if os.name == "nt" else repository.startswith("/"))
    if local_repository:
        host_repo = Path(repository).expanduser().resolve()
        host_repo.mkdir(parents=True, exist_ok=True)
        container_repo = "/restic-repository" if os.name == "nt" else str(host_repo)
        cmd += ["-v", f"{host_repo}:{container_repo}", "-e", f"RESTIC_REPOSITORY={container_repo}"]
    else:
        if ":" not in repository:
            raise _WorkspaceOperationError("local RESTIC_REPOSITORY must be an absolute path")
        cmd += ["-e", f"RESTIC_REPOSITORY={repository}"]
    if password_file:
        container_password = "/run/secrets/odt-env-restic-password" if os.name == "nt" else str(password_file_path)
        cmd += ["-v", f"{password_file_path}:{container_password}:ro",
                "-e", f"RESTIC_PASSWORD_FILE={container_password}"]
    else:
        cmd += ["-e", "RESTIC_PASSWORD"]
    return cmd + ["--entrypoint", "restic", "odoo", *args]


def _workspace_filestore_path(target: str, root: Path, cfg: ProjectConfig, db_name: str) -> Any:
    if target == "docker":
        return f"/var/lib/odoo/filestore/{db_name}"
    layout = Layout.from_root(root)
    raw_dir = str(cfg.config.get("data_dir") or "").strip()
    if raw_dir:
        specified_dir = Path(raw_dir).expanduser()
        layout = replace(layout, data_dir=(specified_dir if specified_dir.is_absolute()
                                           else root / specified_dir).resolve())
    return layout.data_dir / "filestore" / db_name


def _workspace_tar_command(target: str, arguments: list[str]) -> list[str]:
    if target == "docker":
        return _workspace_docker_command() + ["compose", "run", "--rm", "--no-deps", "-T",
                                              "--entrypoint", "tar", "odoo", *arguments]
    return ["tar", *arguments]


def _workspace_temp_backup(output: Path, command: list[str], root: Path, env: Optional[dict[str, str]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    tmp_path = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            _workspace_command(command, root, env=env, stdout=stream)
        os.replace(tmp_path, output)
    finally:
        tmp_path.unlink(missing_ok=True)


def _build_backup_restore_parser(action: str, prog: str) -> argparse.ArgumentParser:
    descriptions = {
        "backup-db": "Create a PostgreSQL custom-format database backup.",
        "restore-db": "Restore a PostgreSQL dump into the workspace database.",
        "backup-filestore": "Create a compressed backup of the database filestore.",
        "restore-filestore": "Restore the database filestore from a compressed archive.",
        "restic": "Run a Restic command against the configured repository.",
        "backup-filestore-restic": "Create an incremental filestore backup using Restic.",
        "restore-filestore-restic": "Restore the database filestore from a Restic snapshot.",
    }
    parser = argparse.ArgumentParser(
        prog=f"{prog} {action}", description=descriptions[action],
    )
    parser.add_argument("--root", default=".", metavar="ROOT", help="Workspace root (default: current directory).")
    if action in ("restore-db", "restore-filestore", "restore-filestore-restic"):
        parser.add_argument("--force", action="store_true", help="Replace an existing destination (destructive).")
    if action == "restore-db":
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--copy", action="store_true", help="Regenerate database identity (default).")
        mode.add_argument("--move", action="store_true", help="Preserve database identity.")
    if action in ("backup-db", "backup-filestore"):
        parser.add_argument("output", nargs="?", metavar="OUTPUT", help="Backup destination (default: timestamped file under odoo-backups/).")
    elif action in ("restore-db", "restore-filestore"):
        parser.add_argument("source", metavar="BACKUP", help="Path to the backup file to restore.")
    elif action == "restore-filestore-restic":
        parser.add_argument("snapshot", nargs="?", default="latest", metavar="SNAPSHOT", help="Restic snapshot ID (default: latest).")
    elif action in ("restic", "backup-filestore-restic"):
        parser.add_argument("restic_args", nargs=argparse.REMAINDER, metavar="RESTIC_ARGS", help="Arguments forwarded to Restic.")
    return parser


def _run_backup_restore_cli(target: str, action: str, args: list[str], prog: str) -> int:
    """Execute backup/restore tasks directly from the installed CLI (no generated scripts)."""
    parser = _build_backup_restore_parser(action, prog)
    if action in ("restic", "backup-filestore-restic"):
        opts, extra = parser.parse_known_args(args)
        opts.restic_args = extra + opts.restic_args
    else:
        opts = parser.parse_args(args)
    root = Path(opts.root).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"workspace ROOT does not exist: {root}")
    ini_path = root / _DEFAULT_PROJECT_INI_NAME
    if not ini_path.is_file():
        parser.error(f"workspace project file not found: {ini_path}")

    try:
        cfg = load_project_config(ini_path, log_resolved=False, log_overrides=False)
        db_name = _docker_default_db_name(cfg) if target == "docker" else _native_default_db_name(cfg)
        env = {**os.environ, **_native_pg_env(cfg)} if target == "venv" else None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filestore = _workspace_filestore_path(target, root, cfg, db_name)

        if action in ("backup-db", "backup-filestore"):
            ext = ".dump" if action == "backup-db" else "_filestore.tar.gz"
            output = Path(opts.output).expanduser() if opts.output else root / "odoo-backups" / f"{db_name}_{stamp}{ext}"
            if not output.is_absolute():
                output = root / output
            output = output.absolute()
            if action == "backup-db":
                if target == "docker":
                    command = _workspace_docker_command() + ["compose", "exec", "-T", "db", "pg_dump",
                        "--format=custom", "--no-owner", "--no-acl", "-U", "odoo", db_name]
                else:
                    command = [_workspace_postgres_command("pg_dump"), "--format=custom", "--no-owner", "--no-acl", db_name]
            else:
                command = _workspace_tar_command(target, ["-C", str(filestore), "-czf", "-", "."])
            _logger.info("Backing up %s to %s", action, output)
            _workspace_temp_backup(output, command, root, env)
            _logger.info("Backup created: %s", output)
            return 0

        if action in ("restore-db", "restore-filestore"):
            source = Path(opts.source).expanduser()
            if not source.is_absolute():
                source = root / source
            if not source.is_file():
                raise _WorkspaceOperationError(f"backup not found: {source}")
            source = source.resolve()
            if action == "restore-db":
                exists = _workspace_db_exists(target, root, env, db_name)
                if exists and not opts.force:
                    raise _WorkspaceOperationError(f"target database '{db_name}' already exists. Use --force to replace it.")
                if target == "venv" and not opts.move:
                    click = root / "venv" / ("Scripts/click-odoo.exe" if os.name == "nt" else "bin/click-odoo")
                    conf = root / "odoo-configs/odoo-server.conf"
                    if not click.is_file() or not conf.is_file():
                        raise _WorkspaceOperationError("--copy requires venv click-odoo and Odoo config")
                _logger.info("Restoring database %s from %s", db_name, source)
                if exists:
                    drop = _workspace_db_command(target, "dropdb", "--force", "--maintenance-db=postgres", db_name)
                    _workspace_command(drop, root, env=env)
                _workspace_command(_workspace_db_command(target, "createdb", "--maintenance-db=postgres", db_name), root, env=env)
                cmd = _workspace_db_command(target, "pg_restore", "--verbose", "--exit-on-error", "--no-owner", "--no-acl", "-d", db_name)
                with source.open("rb") as stream:
                    _workspace_command(cmd, root, env=env, stdin=stream)
                if not opts.move:
                    identity = 'env["ir.config_parameter"].init(force=True)\n'.encode("utf-8")
                    if target == "docker":
                        cmd = _workspace_docker_command() + ["compose", "run", "--rm", "--no-deps", "-T",
                            "--entrypoint", "click-odoo", "odoo", "-c", "/etc/odoo/odoo.conf",
                            "-d", db_name, "--log-level=error"]
                    else:
                        cmd = [str(click), "-c", str(conf), "-d", db_name, "--log-level=error"]
                    try:
                        result = subprocess.run(cmd, cwd=root, env=env, input=identity, check=False)
                    except OSError as exc:
                        raise _WorkspaceOperationError("could not run database identity reset") from exc
                    if result.returncode:
                        raise _WorkspaceOperationError("database identity reset failed")
                    reset_sql = ("DELETE FROM ir_config_parameter WHERE key = 'database.enterprise_code';\n"
                        "UPDATE ir_config_parameter SET value = 'copy' WHERE key = 'database.expiration_reason' AND value != 'demo';\n"
                        "UPDATE ir_config_parameter SET value = CURRENT_DATE + INTERVAL '2 month' WHERE key = 'database.expiration_date';\n").encode("utf-8")
                    if target == "docker":
                        sql_cmd = _workspace_docker_command() + ["compose", "exec", "-T", "db", "psql", "-v", "ON_ERROR_STOP=1", "-U", "odoo", "-d", db_name]
                    else:
                        sql_cmd = [_workspace_postgres_command("psql"), "-X", "-v", "ON_ERROR_STOP=1", "-d", db_name]
                    result = subprocess.run(sql_cmd, cwd=root, env=env, input=reset_sql, check=False)
                    if result.returncode:
                        raise _WorkspaceOperationError("database identity SQL reset failed")
                _logger.info("Database restore completed: %s", db_name)
                return 0

            path = str(filestore)
            if target == "docker":
                exists_cmd = _workspace_docker_command() + ["compose", "run", "--rm", "--no-deps", "-T",
                    "-e", f"DB_NAME={db_name}", "--entrypoint", "sh", "odoo",
                    "-c", 'if [ -d "/var/lib/odoo/filestore/${DB_NAME}" ]; then echo exists; else echo missing; fi']
                exists = _workspace_command(exists_cmd, root, capture=True).strip() == b"exists"
            else:
                exists = Path(path).exists()
            if exists and not opts.force:
                raise _WorkspaceOperationError(f"target filestore for database '{db_name}' already exists. Use --force to replace it.")
            _logger.info("Restoring filestore for %s from %s", db_name, source)
            if target == "docker":
                cmd = _workspace_docker_command() + ["compose", "run", "--rm", "--no-deps", "-T",
                    "-e", f"DB_NAME={db_name}", "--entrypoint", "sh", "odoo", "-c",
                    'set -eu; target="/var/lib/odoo/filestore/${DB_NAME}"; rm -rf "${target}"; mkdir -p "${target}"; tar -xzf - -C "${target}"']
                with source.open("rb") as stream:
                    _workspace_command(cmd, root, stdin=stream)
            else:
                if Path(path).exists():
                    if Path(path).is_dir() and not Path(path).is_symlink():
                        shutil.rmtree(path)
                    else:
                        Path(path).unlink()
                Path(path).mkdir(parents=True, exist_ok=True)
                with source.open("rb") as stream:
                    _workspace_command(["tar", "-xzf", "-", "-C", path], root, stdin=stream, env=env)
            _logger.info("Filestore restore completed: %s", db_name)
            return 0

        if action in ("backup-filestore-restic", "restore-filestore-restic", "restic"):
            host = os.environ.get("RESTIC_HOST") or socket.getfqdn()
            if action == "restic":
                if not opts.restic_args:
                    parser.error("RESTIC_COMMAND is required")
                command = _workspace_restic_command(target, root, opts.restic_args)
            elif action == "backup-filestore-restic":
                extra_tags = (["--tag", f"backup-id:{os.environ['RESTIC_BACKUP_ID']}"]
                              if os.environ.get("RESTIC_BACKUP_ID") else [])
                command = _workspace_restic_command(target, root, ["backup", "--host", host,
                    "--tag", "odoo", "--tag", "filestore", "--tag", f"db:{db_name}",
                    *extra_tags, *opts.restic_args, str(filestore)])
            else:
                # Respect the source database name from the original backup.
                source_db = os.environ.get("RESTIC_SOURCE_DB_NAME", db_name)
                if len(source_db) > 63 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", source_db):
                    raise _WorkspaceOperationError("invalid RESTIC_SOURCE_DB_NAME")
                source_filestore = _workspace_filestore_path(target, root, cfg, source_db)
                path = str(filestore)
                if target == "docker":
                    exists_cmd = _workspace_docker_command() + ["compose", "run", "--rm", "--no-deps", "-T", "--user", "0:0",
                        "-e", f"DB_NAME={db_name}", "--entrypoint", "sh", "odoo",
                        "-c", 'if [ -d "/var/lib/odoo/filestore/${DB_NAME}" ]; then echo exists; else echo missing; fi']
                    exists = _workspace_command(exists_cmd, root, capture=True).strip() == b"exists"
                else:
                    exists = Path(path).exists()
                if exists and not opts.force:
                    raise _WorkspaceOperationError(f"target filestore for database '{db_name}' already exists. Use --force to replace it.")
                _logger.info("Restoring filestore for %s from Restic snapshot %s", db_name, opts.snapshot)
                if target == "docker":
                    cmd = _workspace_docker_command() + ["compose", "run", "--rm", "--no-deps", "-T", "--user", "0:0",
                        "-e", f"DB_NAME={db_name}", "--entrypoint", "sh", "odoo", "-c",
                        'set -eu; rm -rf "/var/lib/odoo/filestore/${DB_NAME}"; mkdir -p "/var/lib/odoo/filestore/${DB_NAME}"']
                    _workspace_command(cmd, root)
                else:
                    if Path(path).exists():
                        if Path(path).is_dir() and not Path(path).is_symlink():
                            shutil.rmtree(path)
                        else:
                            Path(path).unlink()
                    Path(path).mkdir(parents=True, exist_ok=True)
                restic_args = ["restore", f"{opts.snapshot}:{source_filestore}", "--target", path]
                if opts.snapshot == "latest":
                    restic_args += ["--host", host, "--path", str(source_filestore)]
                command = _workspace_restic_command(target, root, restic_args)
            _workspace_command(command, root, env=os.environ.copy())
            _logger.info("Restic operation completed")
            return 0
        parser.error("unsupported command")
    except (_WorkspaceOperationError, OSError, ValueError, configparser.Error) as exc:
        if isinstance(exc, _WorkspaceOperationError):
            _logger.error("%s", exc)
        else:
            _logger.error("Could not complete %s", action)
        return 1
    except Exception:
        _logger.error("Could not complete %s", action)
        return 1


def _build_odoo_parser(action: str, prog: str) -> argparse.ArgumentParser:
    descriptions = {
        "run": "Run Odoo in the selected workspace.",
        "shell": "Open an interactive Odoo shell in the selected workspace.",
        "update": "Update installed Odoo modules using click-odoo-update.",
        "test": "Run Odoo tests (--test-enable --stop-after-init).",
    }
    epilog = "Additional options are forwarded to Odoo."
    if action == "update":
        epilog = "Additional options are forwarded to click-odoo-update."
    parser = argparse.ArgumentParser(
        prog=f"{prog} {action}", description=descriptions[action],
        epilog=epilog,
    )
    parser.add_argument("--root", default=".", metavar="ROOT", help="Workspace root (default: current directory).")
    if action == "update":
        parser.add_argument("--all", action="store_true", help="Update all installed modules.")
    return parser


def _run_odoo_cli(target: str, action: str, args: list[str], prog: str) -> int:
    """Run an Odoo operation directly, preserving its terminal and exit status."""
    parser = _build_odoo_parser(action, prog)
    opts, passthrough = parser.parse_known_args(args)
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]
    root = Path(opts.root).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"workspace ROOT does not exist: {root}")
    ini = root / _DEFAULT_PROJECT_INI_NAME
    if not ini.is_file():
        parser.error(f"workspace project file not found: {ini}")
    try:
        cfg = load_project_config(ini, log_resolved=False, log_overrides=False)
        if target == "docker":
            db_name = _docker_default_db_name(cfg)
            docker = _workspace_docker_command() + ["compose", "run", "--rm", "--no-deps"]
            conf = "/etc/odoo/odoo.conf"
            if action == "run":
                command = docker + ["odoo", "--", "-c", conf, "-d", db_name, *passthrough]
            elif action == "shell":
                command = docker + ["--entrypoint", "odoo", "odoo", "shell", "-c", conf, "-d", db_name, *passthrough]
            elif action == "test":
                command = docker + ["-T", "odoo", "--", "-c", conf, "-d", db_name,
                                    "--test-enable", "--stop-after-init", *passthrough]
            else:
                command = docker + ["-T", "--entrypoint", "click-odoo-update", "odoo",
                                    "-c", conf, "-d", db_name, "--log-level", "debug"]
                if opts.all:
                    command.append("--update-all")
                command.extend(passthrough)
        else:
            layout = Layout.from_root(root)
            conf = layout.conf_path
            venv = root / "venv"
            if not venv.is_dir() or not conf.is_file():
                raise _WorkspaceOperationError("native virtualenv or Odoo config does not exist")
            if action == "update":
                executable = venv / ("Scripts/click-odoo-update.exe" if os.name == "nt" else "bin/click-odoo-update")
                command = [str(executable), "-c", str(conf), "--log-level", "debug"]
                if opts.all:
                    command.append("--update-all")
                command.extend(passthrough)
            else:
                python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                odoo_bin = _resolve_odoo_path(layout, cfg.odoo) / "odoo-bin"
                if not python.is_file() or not odoo_bin.is_file():
                    raise _WorkspaceOperationError("native Odoo Python or odoo-bin not found")
                command = [str(python), str(odoo_bin)]
                if action == "shell":
                    command.append("shell")
                command += ["-c", str(conf)]
                if action == "test":
                    command += ["--test-enable", "--stop-after-init"]
                command.extend(passthrough)
        # Inherit stdin/stdout/stderr (interactive shells, Ctrl+C and CI output).
        return subprocess.run(command, cwd=root, check=False).returncode
    except (OSError, ValueError, configparser.Error, _WorkspaceOperationError) as exc:
        _logger.error("Could not run %s: %s", action, exc)
        return 1
    except KeyboardInterrupt:
        return 130


# -----------------------------
# Workspace lifecycle: Docker Compose and native virtualenv
# -----------------------------

def _build_lifecycle_parser(action: str, prog: str, target: str) -> argparse.ArgumentParser:
    descriptions = {
        "up": "Regenerate the workspace configuration and start Odoo in the background.",
        "down": "Stop the running workspace without deleting databases or filestores.",
        "status": "Show the current workspace runtime status without changing anything.",
    }
    parser = argparse.ArgumentParser(prog=f"{prog} {action}", description=descriptions[action])
    parser.add_argument("--root", default=".", metavar="ROOT", help="Existing workspace root (default: current directory).")
    if action == "up" and target == "docker":
        parser.add_argument("--build", action="store_true", help="Build the Docker image before starting services.")
        parser.add_argument("--recreate", action="store_true", help="Force recreation of the Odoo container only.")
    return parser


def _native_runtime_paths(root: Path) -> tuple[Path, Path, Path]:
    return (root / ".odt-env" / _NATIVE_RUNTIME_STATE,
            root / ".odt-env" / _NATIVE_RUNTIME_LOCK,
            root / "odoo-logs" / _NATIVE_RUNTIME_LOG)


@contextlib.contextmanager
def _native_runtime_lock(root: Path):
    """Serialize up/down across CLI invocations; never leave a stale lock owner."""
    _, lock_path, _ = _native_runtime_paths(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt
            lock_file.seek(0)
            lock_file.write(b"0")
            lock_file.flush()
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise _WorkspaceOperationError("another native lifecycle operation is in progress") from exc
        else:
            import fcntl
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise _WorkspaceOperationError("another native lifecycle operation is in progress") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _native_windows_process_identity(pid: int) -> Optional[str]:
    """Read the creation time of a live Windows process to guard against PID reuse."""
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        status = wintypes.DWORD()
        creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value != 259:
            return None  # STILL_ACTIVE
        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                        ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        return f"windows:{(creation.dwHighDateTime << 32) | creation.dwLowDateTime}"
    finally:
        kernel32.CloseHandle(handle)


def _native_process_identity(pid: int) -> Optional[str]:
    """Return a stable per-process creation token; refuse unverified PID reuse."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            fields = raw[raw.rfind(")") + 2:].split()
            if fields[0] in ("Z", "X"):
                return None
            try:
                boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            except OSError:
                boot_id = "unknown"
            return f"linux:{boot_id}:{fields[19]}"  # field 22: starttime in clock ticks
        except (OSError, IndexError, ValueError):
            return None
    if os.name == "nt":
        return _native_windows_process_identity(pid)
    # macOS and other POSIX systems: start timestamp from ps.
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    created = result.stdout.strip()
    return f"posix:{created}" if result.returncode == 0 and created else None


def _native_read_process(root: Path) -> Optional[dict[str, Any]]:
    state_path, _, _ = _native_runtime_paths(root)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as exc:
        raise _WorkspaceOperationError(f"cannot read native process state: {state_path}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("pid"), int) or not isinstance(state.get("identity"), str):
        raise _WorkspaceOperationError(f"invalid native process state: {state_path}")
    # Compare filesystem identity instead of path spelling: Windows 8.3 aliases
    # and symlinks may represent the same workspace under different names.
    saved_root = state.get("root")
    try:
        same_workspace = isinstance(saved_root, str) and Path(saved_root).samefile(root)
    except (OSError, ValueError):
        same_workspace = False
    if not same_workspace:
        raise _WorkspaceOperationError("native process state belongs to a different workspace")
    return state


def _native_process_running(state: dict[str, Any]) -> bool:
    return _native_process_identity(state["pid"]) == state["identity"]


def _native_wait_stopped(state: dict[str, Any], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _native_process_running(state):
            return True
        time.sleep(0.2)
    return not _native_process_running(state)


def _native_stop_process(state: dict[str, Any]) -> None:
    """Only terminate our verified Odoo process/process group, never arbitrary PID matches."""
    if not _native_process_running(state):
        return
    pid = state["pid"]
    if os.name == "nt":
        # A new process group permits a graceful CTRL_BREAK when a console exists.
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        except OSError:
            # A non-interactive Windows session may have no console to signal.
            subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        # up starts a dedicated session; refuse to signal any unrelated group.
        if os.getpgid(pid) != pid:
            raise _WorkspaceOperationError("Odoo is no longer the leader of its recorded process group")
        os.killpg(pid, signal.SIGTERM)
    if _native_wait_stopped(state, 10):
        return
    if not _native_process_running(state):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        if os.getpgid(pid) != pid:
            raise _WorkspaceOperationError("Odoo process group changed while stopping")
        os.killpg(pid, signal.SIGKILL)
    if not _native_wait_stopped(state, 5):
        raise _WorkspaceOperationError(f"Odoo process {pid} did not stop")


def _native_start_process(root: Path, cfg: ProjectConfig, conf: Path) -> dict[str, Any]:
    layout = Layout.from_root(root)
    python = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    odoo_bin = _resolve_odoo_path(layout, cfg.odoo) / "odoo-bin"
    if not python.is_file() or not odoo_bin.is_file():
        raise _WorkspaceOperationError("native Odoo Python or odoo-bin not found; run setup first")
    if not conf.is_file():
        raise _WorkspaceOperationError("generated native Odoo config is missing")
    state_path, _, log_path = _native_runtime_paths(root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [str(python), str(odoo_bin), "-c", str(conf)]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with log_path.open("ab") as log_file:
        proc = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                stdout=log_file, stderr=subprocess.STDOUT, close_fds=True,
                                creationflags=flags, start_new_session=(os.name != "nt"))
    # A process may exit immediately (bad interpreter, invalid config, etc.).
    time.sleep(0.3)
    identity = _native_process_identity(proc.pid)
    if proc.poll() is not None or identity is None:
        raise _WorkspaceOperationError(f"Odoo exited during startup; check {log_path}")
    state = {"pid": proc.pid, "identity": identity, "root": str(root),
             "config_sha256": _sha256_file(conf)}
    try:
        _write_json_atomic(state_path, state)
    except Exception:
        # Never leave a detached process unmanaged if recording it fails.
        _native_stop_process(state)
        raise
    print(f"Odoo started (PID {proc.pid}); logs: {log_path}")
    return state


def _run_lifecycle_cli(target: str, action: str, args: list[str], prog: str) -> int:
    parser = _build_lifecycle_parser(action, prog, target)
    opts = parser.parse_args(args)
    root = Path(opts.root).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"workspace ROOT does not exist: {root}")
    ini = root / _DEFAULT_PROJECT_INI_NAME
    if not ini.is_file():
        parser.error(f"workspace project file not found: {ini}; run setup first")
    try:
        if target == "docker":
            compose = _workspace_docker_command() + ["compose"]
            if action == "up":
                conf = _docker_odoo_conf_path(Layout.from_root(root).docker_local_dir)
                runtime_path = root / ".odt-env" / "docker-odoo-runtime.json"
                try:
                    previous = json.loads(runtime_path.read_text(encoding="utf-8"))
                    if not isinstance(previous, dict):
                        previous = {}
                except (OSError, ValueError):
                    previous = {}
                # Provision existing sources only; never clone repos, install packages or create a new project.
                sync_project(ini, sync_odoo=False, sync_addons=False, root_override=root,
                             target="docker", no_provisioning_log=True)
                new_hash = _sha256_file(conf)
                preexisting = subprocess.run(compose + ["ps", "-q", "odoo"], cwd=root,
                                             text=True, capture_output=True, check=False)
                if preexisting.returncode:
                    raise _WorkspaceOperationError("could not query Docker Compose Odoo container")
                up = compose + ["up", "-d", "--wait"]
                if opts.build:
                    up.append("--build")
                _workspace_command(up, root)
                current = subprocess.run(compose + ["ps", "-q", "odoo"], cwd=root,
                                         text=True, capture_output=True, check=False)
                if current.returncode or not current.stdout.strip():
                    raise _WorkspaceOperationError("could not find running Docker Compose Odoo container")
                container_id = current.stdout.strip()
                if (preexisting.stdout.strip() == container_id and
                        (opts.recreate or previous.get("config_sha256") != new_hash or
                         previous.get("container_id") != container_id)):
                    # A bind-mounted odoo.conf change is invisible to Compose config hashing.
                    # Recreate only Odoo; do not restart PostgreSQL or delete its data.
                    _workspace_command(compose + ["up", "-d", "--wait", "--no-deps",
                                                  "--force-recreate", "odoo"], root)
                    current = subprocess.run(compose + ["ps", "-q", "odoo"], cwd=root,
                                             text=True, capture_output=True, check=False)
                    if current.returncode or not current.stdout.strip():
                        raise _WorkspaceOperationError("could not query recreated Odoo container")
                    container_id = current.stdout.strip()
                _write_json_atomic(runtime_path, {"container_id": container_id, "config_sha256": new_hash})
                return 0
            if action == "down":
                _workspace_command(compose + ["down"], root)
                (root / ".odt-env" / "docker-odoo-runtime.json").unlink(missing_ok=True)
                return 0
            _workspace_command(compose + ["ps", "-a"], root)
            return 0

        state_path, _, log_path = _native_runtime_paths(root)
        if action == "status":
            state = _native_read_process(root)
            if state and _native_process_running(state):
                print(f"Odoo: running (PID {state['pid']})\nLogs: {log_path}")
            else:
                print("Odoo: stopped" + (" (stale process state)" if state else ""))
            return 0
        with _native_runtime_lock(root):
            state = _native_read_process(root)
            running = bool(state and _native_process_running(state))
            if action == "down":
                if running:
                    _native_stop_process(state)
                    print("Odoo: stopped")
                else:
                    print("Odoo: already stopped")
                state_path.unlink(missing_ok=True)
                return 0
            conf = Layout.from_root(root).conf_path
            # Native up requires a provisioned environment. Never recreate the venv.
            if not (root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")).is_file():
                raise _WorkspaceOperationError("native virtualenv not found; run setup --create-venv first")
            sync_project(ini, sync_odoo=False, sync_addons=False, root_override=root,
                         target="venv", no_provisioning_log=True)
            if running and state.get("config_sha256") == _sha256_file(conf):
                print(f"Odoo: already running (PID {state['pid']})")
                return 0
            cfg = load_project_config(ini, log_resolved=False, log_overrides=False)
            if running:
                _native_stop_process(state)
                state_path.unlink(missing_ok=True)
            elif state:
                state_path.unlink(missing_ok=True)  # never signal a reused PID
            _native_start_process(root, cfg, conf)
            return 0
    except (OSError, ValueError, configparser.Error, _WorkspaceOperationError) as exc:
        _logger.error("Could not %s workspace: %s", action, exc)
        return 1


def _completion_options(parser: argparse.ArgumentParser) -> list[str]:
    return list(dict.fromkeys(option for action in parser._actions for option in action.option_strings))


def _completion_ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _completion_bash(name: str, commands: list[str], options: dict[str, list[str]]) -> str:
    fn = "_" + name.replace("-", "_") + "_completion"
    output = [
        f"# Bash completion for {name}",
        f"{fn}() {{",
        '    local cur="${COMP_WORDS[COMP_CWORD]}"',
        '    local prev="" cmd="" opts="" candidates=""',
        '    (( COMP_CWORD > 0 )) && prev="${COMP_WORDS[COMP_CWORD-1]}"',
        '    COMPREPLY=()',
        '    if (( COMP_CWORD == 1 )); then',
        '        if [[ "$cur" == -* ]]; then',
        f'            candidates={shlex.quote(" ".join(options[""]))}',
        '        else',
        f'            candidates={shlex.quote(" ".join(commands))}',
        '        fi',
        '        mapfile -t COMPREPLY < <(compgen -W "$candidates" -- "$cur")',
        '        if [[ "$cur" != -* ]]; then',
        '            mapfile -t -O "${#COMPREPLY[@]}" COMPREPLY < <(compgen -f -- "$cur")',
        '            compopt -o filenames 2>/dev/null || true',
        '        fi',
        '        return',
        '    fi',
        '    if [[ "${COMP_WORDS[1]}" == completion && $COMP_CWORD -eq 2 ]]; then',
        '        mapfile -t COMPREPLY < <(compgen -W "' + " ".join(_COMPLETION_SHELLS) + '" -- "$cur")',
        '        return',
        '    fi',
        '    cmd="${COMP_WORDS[1]}"',
        '    case "$cmd" in',
    ]
    for cmd, opts in options.items():
        if cmd == "":
            continue
        output.extend([f"        {shlex.quote(cmd)}) opts={shlex.quote(' '.join(opts))} ;;"])
    output.extend([
        f"        *) opts={shlex.quote(' '.join(options['']))} ;;",
        '    esac',
        '    case "$prev" in',
        '        --root) mapfile -t COMPREPLY < <(compgen -d -- "$cur"); compopt -o filenames 2>/dev/null || true; return ;;',
        '        --create-bundle|--dry-run)',
        '            if [[ "$cur" != -* ]]; then',
        '                mapfile -t COMPREPLY < <(compgen -f -- "$cur")',
        '                compopt -o filenames 2>/dev/null || true; return',
        '            fi ;;',
        '        --create-from-bundle|-i|--include)',
        '            mapfile -t COMPREPLY < <(compgen -f -- "$cur")',
        '            compopt -o filenames 2>/dev/null || true; return ;;',
        '        -e|--extra-var|-S|--set) return ;;',
        '    esac',
        '    if [[ "$cur" == --root=* ]]; then',
        '        local item',
        '        mapfile -t COMPREPLY < <(compgen -d -- "${cur#--root=}")',
        '        for item in "${!COMPREPLY[@]}"; do COMPREPLY[item]="--root=${COMPREPLY[item]}"; done',
        '        compopt -o filenames 2>/dev/null || true; return',
        '    fi',
        '    if [[ "$cur" == -* ]]; then',
        '        mapfile -t COMPREPLY < <(compgen -W "$opts" -- "$cur")',
        '    else',
        '        mapfile -t COMPREPLY < <(compgen -f -- "$cur")',
        '        compopt -o filenames 2>/dev/null || true',
        '    fi',
        '}',
        f'complete -F {fn} {name}',
        '',
    ])
    return "\n".join(output)


def _completion_zsh(name: str, commands: list[str], options: dict[str, list[str]]) -> str:
    fn = "_" + name.replace("-", "_") + "_completion"
    output = [
        f"#compdef {name}",
        f"{fn}() {{",
        '    local -a candidates',
        '    if (( CURRENT == 2 )); then',
        '        if [[ "$PREFIX" == -* ]]; then',
        '            candidates=(' + ' '.join(shlex.quote(x) for x in options['']) + ')',
        '        else',
        '            candidates=(' + ' '.join(shlex.quote(x) for x in commands) + ')',
        '        fi',
        '        compadd -- "${candidates[@]}"',
        '        [[ "$PREFIX" != -* ]] && _files',
        '        return',
        '    fi',
        '    if [[ "${words[2]}" == completion && CURRENT -eq 3 ]]; then',
        '        candidates=(' + ' '.join(_COMPLETION_SHELLS) + ')',
        '        compadd -- "${candidates[@]}"',
        '        return',
        '    fi',
        '    case "${words[2]}" in',
    ]
    for cmd, opts in options.items():
        if cmd:
            output.append(f"        {shlex.quote(cmd)}) candidates=({' '.join(shlex.quote(x) for x in opts)}) ;;")
    output.extend([
        '        *) candidates=(' + ' '.join(shlex.quote(x) for x in options['']) + ') ;;',
        '    esac',
        '    case "${words[CURRENT-1]}" in',
        '        --root) _files -/; return ;;',
        '        --create-bundle|--dry-run) [[ "$PREFIX" != -* ]] && _files && return ;;',
        '        --create-from-bundle|-i|--include) _files; return ;;',
        '        -e|--extra-var|-S|--set) return ;;',
        '    esac',
        '    if [[ "$PREFIX" == -* ]]; then',
        '        compadd -- "${candidates[@]}"',
        '    else',
        '        _files',
        '    fi',
        '}',
        f'compdef {fn} {name}',
        '',
    ])
    return "\n".join(output)


def _completion_fish(name: str, commands: list[str], options: dict[str, list[str]]) -> str:
    fn = "__" + name.replace("-", "_") + "_completion_command"
    output = [
        f"# Fish completion for {name}",
        f"function {fn}",
        '    set -l tokens (commandline -opc)',
        '    if test (count $tokens) -lt 2',
        '        test (count $argv) -eq 0',
        '        return $status',
        '    end',
        '    test (count $argv) -gt 0; and test "$tokens[2]" = "$argv[1]"',
        'end',
    ]
    output.append(f"complete -c {name} -n '{fn}' -f -a {shlex.quote(' '.join(commands))} -d 'CLI command'")
    for cmd, opts in options.items():
        cond = fn if not cmd else f"{fn} {cmd}"
        for opt in opts:
            # Action descriptions are provided by --help; avoid duplicating them here.
            if opt.startswith('--'):
                flag = '-l ' + opt[2:]
            elif len(opt) == 2:
                flag = '-s ' + opt[1:]
            else:
                continue
            kind = _COMPLETION_PATH_OPTIONS.get(opt)
            if kind == "directory":
                extra = " -a '(__fish_complete_directories)'" if opt == '--dry-run' else " -r -a '(__fish_complete_directories)'"
            elif kind == "file":
                extra = ' -F' if opt == '--create-bundle' else ' -r -F'
            elif opt in ('--set', '-S', '--extra-var', '-e'):
                extra = ' -r -f'
            else:
                extra = ''
            output.append(f"complete -c {name} -n '{cond}' {flag}{extra}")
    output.append(f"complete -c {name} -n '{fn} completion' -f -a '" + " ".join(_COMPLETION_SHELLS) + "' -d 'Target shell'")
    output.append('')
    return '\n'.join(output)


def _completion_powershell(name: str, commands: list[str], options: dict[str, list[str]]) -> str:
    result = [
        f'# PowerShell completion for {name}',
        f'Register-ArgumentCompleter -Native -CommandName {_completion_ps_quote(name)} -ScriptBlock {{',
        '    param($wordToComplete, $commandAst, $cursorPosition)',
        '    $optionSets = @{',
    ]
    for cmd, opts in options.items():
        result.append('        '+_completion_ps_quote(cmd)+' = @('+', '.join(_completion_ps_quote(x) for x in opts)+')')
    result += [
        '    }',
        '    $commands = @('+', '.join(_completion_ps_quote(x) for x in commands)+')',
        '    $shells = @('+', '.join(_completion_ps_quote(x) for x in _COMPLETION_SHELLS)+')',
        '    $parts = @($commandAst.CommandElements | ForEach-Object { $_.Extent.Text })',
        '    $before = $parts',
        '    if ($wordToComplete.Length -gt 0 -and $parts.Count -gt 0) {',
        '        $before = @($parts | Select-Object -SkipLast 1)',
        '    }',
        '    $action = if ($before.Count -ge 2) { $before[1] } else { "" }',
        '    $previous = if ($before.Count -ge 2) { $before[-1] } else { "" }',
        '    $candidates = @()',
        '    $pathType = ""',
        '    if ($before.Count -lt 2) {',
        '        $candidates = if ($wordToComplete.StartsWith("-")) { $optionSets[""] } else { $commands }',
        '        if (-not $wordToComplete.StartsWith("-")) { $pathType = "file" }',
        '    } elseif ($action -eq "completion" -and $before.Count -eq 2) {',
        '        $candidates = $shells',
        '    } elseif ($previous -eq "--root") {',
        '        $pathType = "directory"',
        '    } elseif ($previous -in @("--create-bundle", "--dry-run") -and -not $wordToComplete.StartsWith("-")) {',
        '        $pathType = "file"',
        '    } elseif ($previous -in @("--create-from-bundle", "-i", "--include")) {',
        '        $pathType = "file"',
        '    } elseif ($previous -in @("-S", "--set", "-e", "--extra-var")) {',
        '        return',
        '    } elseif ($wordToComplete.StartsWith("-")) {',
        '        $candidates = if ($optionSets.ContainsKey($action)) { $optionSets[$action] } else { $optionSets[""] }',
        '    } else {',
        '        $pathType = "file"',
        '    }',
        '    foreach ($candidate in $candidates) {',
        '        if ($candidate.StartsWith($wordToComplete, [StringComparison]::OrdinalIgnoreCase)) {',
        '            [System.Management.Automation.CompletionResult]::new($candidate, $candidate, "ParameterValue", $candidate)',
        '        }',
        '    }',
        '    if ($pathType) {',
        '        $directory = "."',
        '        $leaf = $wordToComplete',
        '        if ($wordToComplete) {',
        '            $parent = Split-Path -Path $wordToComplete -Parent',
        '            if ($parent) { $directory = $parent }',
        '            $leaf = Split-Path -Path $wordToComplete -Leaf',
        '        }',
        '        Get-ChildItem -LiteralPath $directory -ErrorAction SilentlyContinue | Where-Object {',
        '            $_.Name.StartsWith($leaf, [StringComparison]::OrdinalIgnoreCase) -and',
        '            ($pathType -ne "directory" -or $_.PSIsContainer)',
        '        } | ForEach-Object {',
        '            $path = if ($directory -eq ".") { $_.Name } else { Join-Path $directory $_.Name }',
        '            $completion = if ($path -match "\\s") { "\'" + $path.Replace("\'", "\'\'") + "\'" } else { $path }',
        '            [System.Management.Automation.CompletionResult]::new($completion, $path, "ProviderItem", $path)',
        '        }',
        '    }',
        '}',
        '',
    ]
    return '\n'.join(result)


def _generate_completion(
    shell: str,
    program: str,
    command_descriptions: Iterable[tuple[str, str]],
    parsers: dict[str, argparse.ArgumentParser],
) -> str:
    """Return a script containing only available commands and argparse options."""
    if shell not in _COMPLETION_SHELLS:
        raise ValueError(f"Unsupported shell: {shell}")
    commands = [name for name, _ in command_descriptions]
    options = {name: _completion_options(parser) for name, parser in parsers.items()}
    if shell == 'bash':
        return _completion_bash(program, commands, options)
    if shell == 'zsh':
        return _completion_zsh(program, commands, options)
    if shell == 'fish':
        return _completion_fish(program, commands, options)
    return _completion_powershell(program, commands, options)


def _run_completion_cli(target: str, args: list[str], prog: str) -> int:
    """Print shell completion code without reading or provisioning a workspace."""
    parser = argparse.ArgumentParser(
        prog=f"{prog} completion", description="Generate shell completion for this CLI."
    )
    parser.add_argument("shell", choices=_COMPLETION_SHELLS)
    opts = parser.parse_args(args)
    parsers = {
        "": build_parser(prog, target),
        "setup": build_parser(f"{prog} setup", target, setup_command=True),
        **{name: _build_lifecycle_parser(name, prog, target) for name in ("up", "down", "status")},
        "completion": parser,
        **{name: _build_odoo_parser(name, prog) for name in ("run", "shell", "update", "test")},
        "neutralize": _build_neutralize_parser(prog),
        **{name: _build_backup_restore_parser(name, prog) for name in _BACKUP_RESTORE_SCRIPT_NAMES},
    }
    print(_generate_completion(opts.shell, prog, _CLI_COMMANDS, parsers), end="")
    return 0


def main(target: str = "docker", prog: str = "odoo-compose") -> None:
    # Standard logging to stdout only.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if sys.argv[1:2] and sys.argv[1] in ("up", "down", "status"):
        raise SystemExit(_run_lifecycle_cli(target, sys.argv[1], sys.argv[2:], prog))
    if sys.argv[1:2] == ["completion"]:
        raise SystemExit(_run_completion_cli(target, sys.argv[2:], prog))
    if sys.argv[1:2] and sys.argv[1] in ("run", "shell", "update", "test"):
        raise SystemExit(_run_odoo_cli(target, sys.argv[1], sys.argv[2:], prog))
    if sys.argv[1:2] == ["neutralize"]:
        raise SystemExit(_run_neutralize_cli(target, sys.argv[2:], prog))
    if sys.argv[1:2] and sys.argv[1] in _BACKUP_RESTORE_SCRIPT_NAMES:
        raise SystemExit(_run_backup_restore_cli(target, sys.argv[1], sys.argv[2:], prog))
    _logger.info("%s %s", prog, __version__)

    # All workspace provisioning is explicitly dispatched through `setup`.
    # Plain invocations only print CLI help, and never touch the workspace.
    if sys.argv[1:2] != ["setup"]:
        parser = build_parser(prog=prog, target=target)
        if len(sys.argv) == 1:
            parser.print_help()
            return
        parser.parse_args(sys.argv[1:])
        parser.print_help()
        return

    parser = build_parser(prog=f"{prog} setup", target=target, setup_command=True)
    args = parser.parse_args(sys.argv[2:])

    # `setup ROOT` creates or reuses ROOT/odoo-project.ini, while an INI file,
    # Git-backed INI, or URL remains an explicit project definition source.
    source = (args.source or "").strip()
    setup_root_raw = ""
    args.ini = None
    if source:
        candidate = Path(source).expanduser()
        if (source.startswith("git+") or "://" in source or
                (candidate.is_file() or (not candidate.is_dir() and candidate.suffix.lower() == ".ini"))):
            args.ini = source
        else:
            setup_root_raw = source

    if setup_root_raw and args.root:
        parser.error("workspace ROOT was specified both as setup ROOT and --root; use only one of them.")
    if setup_root_raw:
        args.root = setup_root_raw

    if bool(getattr(args, 'show_last_run', False)) and getattr(args, 'create_from_bundle', None):
        parser.error("--create-from-bundle cannot be used together with --show-last-run.")
    if bool(getattr(args, 'show_last_run', False)) and getattr(args, 'create_bundle', None) is not None:
        parser.error("--create-bundle cannot be used together with --show-last-run.")

    if bool(getattr(args, 'show_last_run', False)):
        root = _root_for_show_last_run(parser, args)
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
    no_data_dir = bool(getattr(args, 'no_data_dir', False))
    create_docker_deploy_requested = bool(getattr(args, 'create_docker_deploy', False))
    no_provisioning_log = bool(getattr(args, 'no_provisioning_log', False))
    create_bundle_raw = getattr(args, 'create_bundle', None)
    create_from_bundle_raw = getattr(args, 'create_from_bundle', None)
    allow_dirty_bundle = bool(getattr(args, 'allow_dirty_bundle', False))

    if allow_dirty_bundle and create_bundle_raw is None:
        parser.error("--allow-dirty-bundle requires --create-bundle.")

    try:
        vars_overrides = _parse_cli_vars(getattr(args, 'extra_vars', []) or [])
        ini_overrides = _parse_cli_ini_overrides(getattr(args, 'ini_overrides', []) or [])
    except Exception as e:
        parser.error(str(e))

    if create_from_bundle_raw:
        non_config_sections = sorted(section for section in ini_overrides if section != "config")
        if non_config_sections:
            parser.error(
                "--create-from-bundle only allows --set overrides in [config]; "
                "unsupported sections: " + ", ".join(non_config_sections)
            )

    if vars_overrides:
        _logger.info('CLI [vars] overrides enabled for keys: %s', ', '.join(sorted(vars_overrides)))
    if ini_overrides:
        override_targets = [f"{section}:{key}" for section, options in ini_overrides.items() for key in options]
        _logger.info('CLI INI overrides enabled for keys: %s', ', '.join(sorted(override_targets)))

    if create_from_bundle_raw:
        conflicting_options: list[str] = []
        if (args.ini or "").strip():
            conflicting_options.append("INI")
        if getattr(args, 'include_inis', None):
            conflicting_options.append("--include")
        if args.sync or args.all or args.odoo or args.addons:
            conflicting_options.append("--sync*")
        if bool(getattr(args, 'create_venv', False)) or create_venv_from_wheelhouse:
            conflicting_options.append("--create-venv*")
        if clear_pip_wheel_cache:
            conflicting_options.append("--clear-pip-wheel-cache")
        if create_docker_deploy_requested:
            conflicting_options.append("--create-docker-deploy")
        if create_bundle_raw is not None:
            conflicting_options.append("--create-bundle")
        if allow_dirty_bundle:
            conflicting_options.append("--allow-dirty-bundle")
        if conflicting_options:
            parser.error(
                "--create-from-bundle cannot be combined with: "
                + ", ".join(conflicting_options)
            )

        if args.root:
            bundle_root = _validate_root_override(parser, args.root)
        else:
            try:
                bundle_root = Path.cwd().resolve()
            except Exception:
                bundle_root = Path.cwd().absolute()

        bundle_path = Path(create_from_bundle_raw).expanduser()
        if not bundle_path.is_absolute():
            bundle_path = Path.cwd() / bundle_path

        try:
            ini_path, _bundle_manifest = extract_workspace_bundle(bundle_path, bundle_root)
            if vars_overrides or ini_overrides:
                _save_workspace_ini_overrides(
                    ini_path, ini_path, vars_overrides, ini_overrides,
                )
            sync_project(
                ini_path,
                sync_odoo=False,
                sync_addons=False,
                root_override=bundle_root,
                reuse_wheelhouse=True,
                create_venv=True,
                clear_pip_wheel_cache=False,
                no_configs=no_configs,
                no_data_dir=no_data_dir,
                # Bundle import is the offline deployment path. Local-development
                # Docker artifacts intentionally require online dependency resolution
                # and can be generated later by a normal odoo-compose run.
                target="venv",
                create_docker_deploy_requested=False,
                bundle_output=None,
                allow_dirty_bundle=False,
                vars_overrides=vars_overrides,
                ini_overrides=ini_overrides,
                no_provisioning_log=no_provisioning_log,
                cli_argv=sys.argv,
                source_ini_layers=[_path_for_report(bundle_path)],
                project_ini_status=(
                    f"created ROOT/{_DEFAULT_PROJECT_INI_NAME} from workspace bundle "
                    f"{_path_for_report(bundle_path)}"
                ),
                source_snapshot_ready=bool(vars_overrides or ini_overrides),
            )
        except Exception as e:
            _logger.error('%s', e)
            raise SystemExit(1)
        return

    root_override: Optional[Path] = None
    include_inis = [item for item in (getattr(args, 'include_inis', []) or []) if (item or "").strip()]
    has_positional_ini = bool((args.ini or "").strip())
    raw_explicit_sources = ([args.ini] if has_positional_ini else []) + include_inis
    implicit_ini = not raw_explicit_sources
    source_ini_layers: list[str] = []
    project_ini_status: Optional[str] = None
    preserved_db_name: Optional[str] = None
    source_snapshot_ready = False

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
        source_ini_layers = [_path_for_report(ini_path)]

        try:
            created_default_ini = not ini_path.exists()
            _write_default_project_ini_template(ini_path)
            if created_default_ini:
                project_ini_status = f"created and used ROOT/{_DEFAULT_PROJECT_INI_NAME}"
            else:
                project_ini_status = f"used existing ROOT/{_DEFAULT_PROJECT_INI_NAME}"
                _logger.info("Project INI already exists, leaving it in place: %s", ini_path)

            if vars_overrides or ini_overrides:
                _save_workspace_ini_overrides(
                    ini_path, ini_path, vars_overrides, ini_overrides,
                )
            elif created_default_ini:
                _save_effective_ini_copy(
                    ini_path, vars_overrides=vars_overrides, ini_overrides=ini_overrides,
                )
            source_snapshot_ready = bool(vars_overrides or ini_overrides or created_default_ini)

        except SystemExit:
            raise
        except Exception as e:
            _logger.error('%s', e)
            raise SystemExit(1)
    elif include_inis:
        root_override = _workspace_root_for_explicit_sources(parser, raw_explicit_sources, args.root)
        if root_override is None:
            parser.error("Internal error: failed to determine workspace ROOT for included INI layers.")

        default_ini_path = root_override / _DEFAULT_PROJECT_INI_NAME
        if target == "venv":
            preserved_db_name = _existing_workspace_db_name(root_override)
        default_ini_existed = default_ini_path.is_file()
        default_ini_layer_index = _explicit_source_uses_default_project_ini(
            parser,
            raw_explicit_sources,
            default_ini_path,
        )
        source_ini_layers = [
            _describe_cli_ini_source_for_report(parser, raw_source)
            for raw_source in raw_explicit_sources
        ]
        if default_ini_layer_index is not None:
            project_ini_status = (
                f"used existing ROOT/{_DEFAULT_PROJECT_INI_NAME} as explicit source layer "
                f"#{default_ini_layer_index}"
            )
        elif default_ini_existed:
            project_ini_status = (
                f"ignored existing ROOT/{_DEFAULT_PROJECT_INI_NAME}; "
                "explicit INI layer(s) were applied and the merged result was written there"
            )
        else:
            project_ini_status = (
                f"no existing ROOT/{_DEFAULT_PROJECT_INI_NAME}; "
                "explicit INI layer(s) were applied and the merged result was written there"
            )

        try:
            ini_path = _prepare_included_project_ini(
                parser,
                raw_explicit_sources,
                root_override,
                vars_overrides=vars_overrides,
                ini_overrides=ini_overrides,
            )
            source_snapshot_ready = True
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

            default_ini_path = root_override / _DEFAULT_PROJECT_INI_NAME
            if target == "venv":
                preserved_db_name = _existing_workspace_db_name(root_override)
            default_ini_existed = default_ini_path.is_file()
            source_ini_layers = [_describe_cli_ini_source_for_report(parser, args.ini)]
            if default_ini_existed:
                project_ini_status = (
                    f"ignored existing ROOT/{_DEFAULT_PROJECT_INI_NAME}; "
                    f"{source_kind} INI source was copied there"
                )
            else:
                project_ini_status = f"created ROOT/{_DEFAULT_PROJECT_INI_NAME} from {source_kind} INI source"

            try:
                if git_ini_source is not None:
                    ini_path = _copy_git_ini_to_root(git_ini_source, root_override)
                else:
                    ini_path = _copy_url_ini_to_root(url_ini_source, root_override)

                if vars_overrides or ini_overrides:
                    _save_workspace_ini_overrides(
                        ini_path, ini_path, vars_overrides, ini_overrides,
                    )
                else:
                    _save_effective_ini_copy(
                        ini_path, vars_overrides=vars_overrides, ini_overrides=ini_overrides,
                    )
                source_snapshot_ready = True
            except Exception as e:
                _logger.error('%s', e)
                raise SystemExit(1)
        else:
            ini_path = _resolve_local_ini_path(parser, args.ini)
            source_ini_layers = [_path_for_report(ini_path)]

            if args.root:
                root_override = _validate_root_override(parser, args.root)
            else:
                _logger.info('Workspace ROOT default (INI directory): %s', ini_path.parent.resolve())

            effective_root = root_override or ini_path.parent.resolve()
            if target == "venv":
                preserved_db_name = _existing_workspace_db_name(effective_root)
            default_ini_path = effective_root / _DEFAULT_PROJECT_INI_NAME
            if vars_overrides or ini_overrides:
                try:
                    _save_workspace_ini_overrides(
                        ini_path, default_ini_path, vars_overrides, ini_overrides,
                    )
                    ini_path = default_ini_path
                    source_snapshot_ready = True
                except Exception as e:
                    _logger.error('%s', e)
                    raise SystemExit(1)
            if _same_path(ini_path, default_ini_path):
                project_ini_status = f"used existing ROOT/{_DEFAULT_PROJECT_INI_NAME}"
            elif default_ini_path.is_file():
                project_ini_status = (
                    f"ignored existing ROOT/{_DEFAULT_PROJECT_INI_NAME}; "
                    "explicit local INI source was used"
                )
            else:
                project_ini_status = (
                    f"explicit local INI source was used; no existing ROOT/{_DEFAULT_PROJECT_INI_NAME}"
                )

    if args.sync:
        # Select only the Git sources required by the effective workspace.
        # Use the merged/resolved INI so --include, --set and -e affect selection.
        try:
            sync_cfg = load_project_config(
                ini_path, vars_overrides=vars_overrides, ini_overrides=ini_overrides,
                log_resolved=False, log_overrides=False,
            )
        except Exception as e:
            _logger.error('%s', e)
            raise SystemExit(1)
        sync_odoo = (
            not sync_cfg.odoo.is_local
            and (target == "venv" or _docker_uses_workspace_odoo(sync_cfg))
        )
        sync_addons = any(not addon.is_local for addon in sync_cfg.addons.values())
        _logger.info(
            "Auto sync (--sync): Odoo Git=%s, addon Git=%s",
            sync_odoo, sync_addons,
        )
    elif args.all:
        sync_odoo, sync_addons = True, True
    elif args.odoo:
        sync_odoo, sync_addons = True, False
    elif args.addons:
        sync_odoo, sync_addons = False, True
    else:
        # No sync target selected -> regenerate workspace artifacts without syncing repositories.
        sync_odoo, sync_addons = False, False

    workspace_root = (root_override or ini_path.parent).resolve()
    bundle_output = (
        _resolve_bundle_output(create_bundle_raw, workspace_root)
        if create_bundle_raw is not None
        else None
    )

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
            no_data_dir=no_data_dir,
            target=target,
            create_docker_deploy_requested=create_docker_deploy_requested,
            bundle_output=bundle_output,
            allow_dirty_bundle=allow_dirty_bundle,
            vars_overrides=vars_overrides,
            ini_overrides=ini_overrides,
            no_provisioning_log=no_provisioning_log,
            cli_argv=sys.argv,
            source_ini_layers=source_ini_layers,
            project_ini_status=project_ini_status,
            preserved_db_name=preserved_db_name,
            source_snapshot_ready=source_snapshot_ready,
        )
    except Exception as e:
        _logger.error(f'{e}')
        raise SystemExit(1)


def main_venv() -> None:
    """Entry point for native Odoo workspaces (venv is opt-in)."""
    main(target="venv", prog="odoo-compose-venv")


if __name__ == "__main__":
    main()
