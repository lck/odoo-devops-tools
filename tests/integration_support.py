"""Shared helpers for real Odoo integration tests (no test scenarios here)."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

CLI = Path(__file__).resolve().parents[1] / "odoo_devops_tools" / "odoo_compose.py"


def execute(*args, cwd=None, env=None, timeout=3600, expect_failure=False, capture=False):
    args = [str(arg) for arg in args]
    print("\n$", subprocess.list2cmdline(args), flush=True)
    # Inherit the terminal streams so Git, Docker and uv can print live progress.
    # Capture only commands whose output is used by assertions (e.g. SQL queries).
    proc = subprocess.run(
        args, cwd=cwd, env={**os.environ, **(env or {})},
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if capture and proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n", flush=True)
    if (proc.returncode == 0) == expect_failure:
        raise AssertionError(f"Expected {'failure' if expect_failure else 'success'}; exit={proc.returncode}: {args}")
    return proc.stdout or ""


def require(path):
    assert Path(path).exists(), f"Missing: {path}"


def cli(workflow, command, *args, cwd=None, **kw):
    # No package installation needed for local test execution.
    env = {"ODT_GIT_PROGRESS": "1", **(kw.pop("env", None) or {})}
    return execute(sys.executable, "-S", CLI, workflow, command, *args, cwd=cwd, env=env, **kw)


def pg(database, sql):
    return execute("psql", "-h", os.getenv("ODT_TEST_DB_HOST", "127.0.0.1"), "-p", "5432", "-U", "odoo", "-d", database, "-tAc", sql, env={"PGPASSWORD": "odoo"}, capture=True).strip()


def expect_modules(query):
    sql = "SELECT string_agg(wanted.name, ',') FROM (VALUES ('web_notify'), ('helpdesk_mgmt')) wanted(name) LEFT JOIN ir_module_module actual ON actual.name = wanted.name AND actual.state = 'installed' WHERE actual.name IS NULL"
    missing = query(sql)
    assert not missing, f"Missing installed addons: {missing}"


def write_ini(path: Path, *, db="odoo", native=True):
    requirements = "[virtualenv]\nrequirements =\n  psycopg2-binary==2.9.9\nrequirements_ignore =\n  psycopg2\n\n" if native else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        requirements + "[odoo]\nversion = 18.0\n\n"
        "[addons.oca-web]\nrepo = https://github.com/OCA/web.git\nbranch = ${odoo:version}\n\n"
        "[addons.oca-helpdesk]\nrepo = https://github.com/OCA/helpdesk.git\nbranch = ${odoo:version}\n\n"
        f"[config]\ndb_host = {os.getenv('ODT_TEST_DB_HOST', '127.0.0.1')}\ndb_port = 5432\ndb_user = odoo\ndb_password = odoo\ndb_name = {db}\n",
        encoding="utf-8",
    )
    return path


def _remove_readonly(func, path, exc_info):
    """Retry Windows cleanup when Git creates read-only pack files."""
    error = exc_info[1]
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    func(path)


def run_scenario(scenario):
    """Run an integration scenario in a temporary workspace; retain failures."""
    root = Path(tempfile.mkdtemp(prefix="odt-e2e-", dir=os.getenv("RUNNER_TEMP") or None))
    print(f"Integration workspace: {root}", flush=True)
    try:
        scenario(root)
    except Exception:
        print(f"FAILED. Workspace retained for diagnostics: {root}", file=sys.stderr)
        raise
    else:
        shutil.rmtree(root, onerror=_remove_readonly if os.name == "nt" else None)
