"""Docker workflow with Odoo sources mounted from the workspace."""

import subprocess
from pathlib import Path

import pytest

from tests.integration_support import cli as e2e_cli, execute, require, run_scenario


def docker_source(root: Path):
    """Docker image running a Git checkout of Odoo 19 instead of the packaged Odoo."""
    ws = root / "docker-source"
    ws.mkdir()
    try:
        e2e_cli("docker", "setup", "--root", ws, "--sync-all",
            "--set", "odoo:version=19.0",
            "--set", "virtualenv:constraints=packaging<1000",
            "--set", "docker:odoo_source=workspace")
        require(ws / "odoo/.git")
        require(ws / "odoo/odoo-bin")
        constraints = ws / "docker/local/requirements/constraints.txt"
        require(constraints)
        assert "packaging<1000" in constraints.read_text()
        dockerfile = (ws / "docker/local/Dockerfile").read_text()
        assert "ENV PYTHONPATH=/opt/odoo" in dockerfile
        execute("docker", "compose", "config", "--quiet", cwd=ws)
        execute("docker", "compose", "build", "odoo", cwd=ws)
        # The Odoo image entrypoint waits for PostgreSQL even with --version.
        # --no-deps skips starting it, so make the database healthy first.
        execute("docker", "compose", "up", "-d", "--wait", "db", cwd=ws)
        execute("docker", "compose", "run", "--rm", "--no-deps", "--entrypoint", "python3", "odoo", "-c",
            "import odoo.release; assert odoo.release.__file__.startswith('/opt/odoo/'), odoo.release.__file__", cwd=ws)
        # Windows-style line endings must not break the generated Odoo launcher.
        odoo_bin = ws / "odoo/odoo-bin"
        original = odoo_bin.read_bytes()
        try:
            odoo_bin.write_bytes(original.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            execute("docker", "compose", "run", "--rm", "--no-deps", "odoo", "--version", cwd=ws)
        finally:
            odoo_bin.write_bytes(original)
        execute("docker", "compose", "run", "--rm", "odoo", "--", "-c", "/etc/odoo/odoo.conf", "-d", "odoo_workspace", "-i", "base", "--without-demo=all", "--stop-after-init", cwd=ws)
        status = execute("docker", "compose", "exec", "-T", "db", "psql", "-U", "odoo", "-d", "odoo_workspace", "-tAc", "SELECT state FROM ir_module_module WHERE name='base'", cwd=ws, capture=True).strip()
        assert status == "installed", f"Workspace Odoo did not initialize base: {status}"
    finally:
        subprocess.run(["docker", "compose", "logs", "--no-color", "--tail=80"], cwd=ws, check=False)
        subprocess.run(["docker", "compose", "down", "-v", "--remove-orphans"], cwd=ws, check=False)
    print("Docker workspace Odoo integration passed", flush=True)


@pytest.mark.integration
def test_real_docker_workspace_source(linux_docker):
    """Build and run Odoo from workspace sources."""
    run_scenario(docker_source)
