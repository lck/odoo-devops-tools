"""Small cross-platform helpers for black-box CLI tests (standard library only)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "odoo_devops_tools" / "odoo_compose.py"
DEFAULT_INI = "[odoo]\nversion = 19.0\n"


def run(*argv: object, cwd: Path | None = None, env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    if env:
        merged.update({k: str(v) for k, v in env.items()})
    return subprocess.run(
        [str(item) for item in argv], cwd=cwd or REPO, env=merged, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
        encoding="utf-8", errors="replace",
    )


def cli(*args: object, **kw) -> subprocess.CompletedProcess:
    return run(sys.executable, "-S", CLI, *args, **kw)


def ok(result: subprocess.CompletedProcess) -> str:
    if result.returncode:
        raise AssertionError(f"Exit {result.returncode}: {result.args!r}\n{result.stdout}")
    return result.stdout


def fails(result: subprocess.CompletedProcess, text: str | None = None) -> str:
    if result.returncode == 0:
        raise AssertionError(f"Unexpected success: {result.args!r}\n{result.stdout}")
    if text is not None and text not in result.stdout:
        raise AssertionError(f"Expected {text!r} in failure:\n{result.stdout}")
    return result.stdout


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="odt-ci-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / "workspace"
        self.ws.mkdir()

    def project(self, ini: str = DEFAULT_INI, where: Path | None = None) -> Path:
        path = (where or self.ws) / "odoo-project.ini"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ini, encoding="utf-8")
        return path

    def invoke(self, workflow="venv", command="setup", *args, root=None, **kw):
        return cli(workflow, command, "--root", root or self.ws, *args, **kw)

    def setup_ws(self, workflow="venv", *args, **kw) -> str:
        return ok(self.invoke(workflow, "setup", "--no-provisioning-log", *args, **kw))
