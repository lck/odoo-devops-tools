"""Shared pytest fixtures for real Odoo integration tests."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "tests" / "compose.yaml"


def docker_available() -> bool:
    """Only a running Linux Docker engine with Compose can run integrations."""
    if not shutil.which("docker"):
        return False
    try:
        info = subprocess.run(
            ["docker", "info", "--format", "{{.OSType}}"],
            capture_output=True, text=True, timeout=10,
        )
        compose = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return info.returncode == 0 and info.stdout.strip() == "linux" and compose.returncode == 0


def windows_native_available() -> bool:
    """Never touch a user's PostgreSQL unless the test instance is explicit."""
    if os.name != "nt" or os.getenv("ODT_TEST_ISOLATED_POSTGRES") != "1":
        return False
    if not all(shutil.which(tool) for tool in ("uv", "psql", "pg_dump")):
        return False
    try:
        result = subprocess.run(
            ["psql", "-h", "127.0.0.1", "-p", "5432", "-U", "odoo",
             "-d", "postgres", "-tAc", "SELECT 1"],
            env={**os.environ, "PGPASSWORD": "odoo"},
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "1"


def command(*args: object) -> None:
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run([str(a) for a in args], cwd=ROOT, check=True)


@pytest.fixture(scope="session")
def linux_docker() -> None:
    if not docker_available():
        pytest.skip("Linux Docker Engine + Docker Compose unavailable")


@pytest.fixture(scope="session")
def integration_stack(linux_docker: None):
    """Build once, share PostgreSQL, always clean up containers and volumes."""
    compose = ["docker", "compose", "-f", COMPOSE, "-p", f"odt-test-{os.getpid()}"]
    try:
        command(*compose, "build", "runner")
        command(*compose, "up", "-d", "--wait", "db")
        yield compose
    finally:
        subprocess.run([*map(str, compose), "down", "-v", "--remove-orphans"],
                       cwd=ROOT, check=False)
