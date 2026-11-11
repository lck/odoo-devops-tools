"""Native workspace setup, filestore safety and real integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import unittest

import pytest

from tests.common import DEFAULT_INI, WorkspaceTest, cli, fails, ok
from tests.conftest import command, windows_native_available
from tests.integration_support import (
    cli as e2e_cli, execute, expect_modules, pg, require, run_scenario, write_ini,
)


class NativeSetup(WorkspaceTest):

    def test_native_workspace_is_idempotent(self):
        self.setup_ws()
        ini = self.ws / "odoo-project.ini"
        original = ini.read_bytes()
        self.setup_ws()
        self.assertEqual(ini.read_bytes(), original)
        self.assertTrue((self.ws / "odoo-configs/odoo-server.conf").exists())
        self.assertFalse((self.ws / "compose.yaml").exists())
        self.assertFalse((self.ws / "venv").exists())
        self.assertFalse((self.ws / "odoo-scripts").exists())


class NativeFiles(WorkspaceTest):

    def test_custom_data_dir_filestore_copy_restore_and_force(self):
        self.project(DEFAULT_INI + "[config]\ndb_name = source_db\ndata_dir = custom-data\n")
        self.setup_ws("venv")
        store = self.ws / "custom-data/filestore/source_db"
        store.mkdir(parents=True)
        (store / "ci-marker").write_text("preserved", encoding="utf-8")
        archive = self.ws / "odoo-backups/store.tar.gz"
        ok(self.invoke("venv", "backup-filestore", archive))
        self.assertTrue(archive.is_file())
        self.setup_ws("venv", "--set", "config:db_name=restored_db")
        ok(self.invoke("venv", "restore-filestore", archive))
        restored = self.ws / "custom-data/filestore/restored_db/ci-marker"
        self.assertEqual(restored.read_text(encoding="utf-8"), "preserved")
        fails(self.invoke("venv", "restore-filestore", archive), "Use --force")
        restored.write_text("modified", encoding="utf-8")
        ok(self.invoke("venv", "restore-filestore", "--force", archive))
        self.assertEqual(restored.read_text(encoding="utf-8"), "preserved")
        self.assertFalse((self.ws / "odoo-data/filestore/restored_db").exists())


    def test_missing_backup_is_rejected_without_modifying_workspace(self):
        self.project()
        self.setup_ws("venv")
        fails(self.invoke("venv", "restore-db", "missing.dump"), "backup not found")
        fails(self.invoke("venv", "restore-filestore", "missing.tar.gz"), "backup not found")
        self.assertFalse((self.ws / "odoo-data/filestore").exists())


    @unittest.skipIf(os.name == "nt", "Windows PostgreSQL commands are exercised by native end-to-end job")

    def test_postgres_binary_override_rejects_invalid_binary(self):
        self.project()
        self.setup_ws("venv")
        backup = self.ws / "odoo-backups/database.dump"
        bad_bin = self.root / "postgres-bin"
        bad_bin.mkdir()
        (bad_bin / "pg_dump").mkdir()  # Deliberately a directory, not an executable.
        result = cli("venv", "backup-db", "--root", self.ws, backup, env={"POSTGRES_BIN": str(bad_bin)})
        fails(result)
        self.assertFalse(backup.exists())

def native(root: Path):
    """Real native provisioning, addons, update, DB/filestore backup and restore."""
    ini = write_ini(root / "project.ini")
    ws = root / "native"
    ws.mkdir()
    e2e_cli("venv", "setup", ini, "--root", ws, "--sync-all", "--create-venv")
    require(ws / "odoo/odoo-bin")
    require(ws / "odoo-addons/oca-web/.git")
    require(ws / "odoo-addons/oca-helpdesk/.git")
    require(ws / "odoo-configs/odoo-server.conf")
    assert not (ws / "compose.yaml").exists()
    assert json.loads((ws / ".odt-env/last-provisioning.json").read_text())["status"] == "success"

    e2e_cli("venv", "run", "-i", "web_notify,helpdesk_mgmt", "--stop-after-init", cwd=ws)
    expect_modules(lambda sql: pg("odoo", sql))
    e2e_cli("venv", "update", cwd=ws)

    # Archive backup/restore and protection against accidental overwrite.
    pg("odoo", "UPDATE ir_config_parameter SET value='source-identity' WHERE key='database.uuid'")
    data = ws / "odoo-data/filestore/odoo"
    data.mkdir(parents=True, exist_ok=True)
    (data / "ci-marker").write_text("filestore-ci")
    backup = ws / "odoo-backups/database.dump"
    filestore = ws / "odoo-backups/filestore.tar.gz"
    e2e_cli("venv", "backup-db", backup, cwd=ws)
    e2e_cli("venv", "backup-filestore", filestore, cwd=ws)
    require(backup)
    require(filestore)
    e2e_cli("venv", "setup", "--root", ws, "--set", "config:db_name=odoo_restore")
    e2e_cli("venv", "restore-db", "--copy", backup, cwd=ws)
    assert pg("odoo_restore", "SELECT value FROM ir_config_parameter WHERE key='database.uuid'") != "source-identity"
    e2e_cli("venv", "restore-db", backup, cwd=ws, expect_failure=True)
    e2e_cli("venv", "restore-db", "--force", "--move", backup, cwd=ws)
    assert pg("odoo_restore", "SELECT value FROM ir_config_parameter WHERE key='database.uuid'") == "source-identity"
    e2e_cli("venv", "restore-filestore", filestore, cwd=ws)
    e2e_cli("venv", "restore-filestore", filestore, cwd=ws, expect_failure=True)
    e2e_cli("venv", "restore-filestore", "--force", filestore, cwd=ws)
    assert (ws / "odoo-data/filestore/odoo_restore/ci-marker").read_text() == "filestore-ci"
    e2e_cli("venv", "neutralize", "--dry-run", cwd=ws)
    # Round-trip through the actual Restic backend on Linux.
    if shutil.which("restic"):
        restic_env = {"RESTIC_REPOSITORY": str(root / "restic"), "RESTIC_PASSWORD": "ci-test-password", "RESTIC_HOST": "odoo-compose-ci"}
        execute("restic", "init", env=restic_env)
        e2e_cli("venv", "backup-filestore-restic", cwd=ws, env=restic_env)
        e2e_cli("venv", "setup", "--root", ws, "--set", "config:db_name=odoo_restic_restore")
        restic_env["RESTIC_SOURCE_DB_NAME"] = "odoo_restore"
        e2e_cli("venv", "restore-filestore-restic", "latest", cwd=ws, env=restic_env)
        e2e_cli("venv", "restore-filestore-restic", "latest", cwd=ws, env=restic_env, expect_failure=True)
        e2e_cli("venv", "restore-filestore-restic", "--force", "latest", cwd=ws, env=restic_env)
        assert (ws / "odoo-data/filestore/odoo_restic_restore/ci-marker").read_text() == "filestore-ci"
    print("Native integration passed", flush=True)


@pytest.mark.integration
def test_real_native_workflow(integration_stack):
    """Native virtualenv + PostgreSQL inside the test container."""
    command(*integration_stack, "run", "--rm", "-T", "--no-deps", "runner",
            "python3", "-m", "tests.test_venv")


@pytest.mark.integration
def test_real_windows_native_workflow(monkeypatch):
    """Exercise Windows virtualenv against isolated PostgreSQL in Windows CI."""
    if os.name != "nt":
        pytest.skip("Windows only")
    if not windows_native_available():
        pytest.skip("Isolated Windows PostgreSQL + uv unavailable")
    pg_dump = shutil.which("pg_dump")
    assert pg_dump, "PostgreSQL binaries must be on PATH in Windows CI"
    monkeypatch.setenv("POSTGRES_BIN", str(Path(pg_dump).parent))
    run_scenario(native)


if __name__ == "__main__":
    run_scenario(native)
