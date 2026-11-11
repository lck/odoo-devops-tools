"""Docker workspace generation, command forwarding and real integrations."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

import pytest

from tests.common import DEFAULT_INI, WorkspaceTest, cli, fails, ok
from tests.integration_support import (
    cli as e2e_cli, execute, expect_modules, require, run_scenario, write_ini,
)


class DockerGeneration(WorkspaceTest):

    def test_invalid_docker_images(self):
        for value in ("odoo:19 RUN echo INJECTED", "odoo:19\\RUN", "odoo:19\nRUN echo INJECTED"):
            with self.subTest(image=value):
                fails(self.invoke("docker", "setup", "--no-provisioning-log", "--set", f"docker:base_image={value}"), "Invalid option 'base_image'")

    def test_invalid_docker_resources(self):
        for value in ("odoo_cpus=0", "db_cpus=-1", "mailpit_cpus=nan", "odoo_mem_limit=-1g", "db_mem_limit=0", "mailpit_mem_limit=invalid"):
            with self.subTest(value=value):
                name = value.split("=")[0]
                fails(self.invoke("docker", "setup", "--no-provisioning-log", "--set", "docker:" + value), f"Invalid option '{name}'")

    def test_restart_policy_validation(self):
        for value in ("", "yes", "false", "on-fail", "always; echo BAD"):
            with self.subTest(value=value):
                fails(self.invoke("docker", "setup", "--no-provisioning-log", "--set", "docker:restart=" + value), "Invalid option 'restart'")
        for value in ("no", "always", "unless-stopped", "on-failure"):
            with self.subTest(value=value):
                self.setup_ws("docker", "--set", "docker:restart=" + value)
                content = (self.ws / "compose.yaml").read_text()
                self.assertIn("restart:", content)

    def test_docker_generation_and_mailpit(self):
        self.setup_ws("docker", "--set", "docker:mailpit_enabled=true", "--set", "docker:odoo_cpus=1.5", "--set", "docker:odoo_mem_limit=2g")
        for path in ("compose.yaml", "docker/local/Dockerfile", "docker/local/.dockerignore", "docker/local/configs/odoo.conf", "docker/local/requirements/all-requirements.in.txt"):
            self.assertTrue((self.ws / path).is_file(), path)
        compose = (self.ws / "compose.yaml").read_text()
        self.assertIn("mailpit", compose)
        self.assertIn("cpus", compose)
        self.assertIn("mem_limit", compose)
        self.assertFalse((self.ws / "venv").exists())

    def test_compose_provenance_and_stable_timestamp(self):
        self.project()
        self.setup_ws("docker")
        compose = self.ws / "compose.yaml"
        generated = compose.read_text()
        self.assertIn("# AUTO-GENERATED FILE - DO NOT EDIT MANUALLY", generated)
        self.assertIn("# Source: odoo-project.ini", generated)
        self.assertRegex(generated, r"# Source SHA256: [0-9a-f]{64}")
        self.assertRegex(generated, r"# Effective SHA256: [0-9a-f]{64}")
        compose.write_text(re.sub(r"^# Generated: .*", "# Generated: 2001-01-01T00:00:00Z", generated, flags=re.M))
        self.setup_ws("docker")
        self.assertIn("# Generated: 2001-01-01T00:00:00Z", compose.read_text())
        self.project(DEFAULT_INI + "# harmless source edit\n")
        self.setup_ws("docker")
        self.assertNotIn("# Generated: 2001-01-01T00:00:00Z", compose.read_text())
        self.assertIn(hashlib.sha256((self.ws / "odoo-project.ini").read_bytes()).hexdigest(), compose.read_text())

    def test_docker_custom_source_and_deploy_context(self):
        # Local sources are supported without downloading real Odoo/OCA repositories.
        odoo = self.root / "odoo"
        odoo.mkdir()
        (odoo / "odoo-bin").write_text("#!/usr/bin/env python3\n")
        (odoo / "requirements.txt").write_text("")
        addons = self.root / "addons"
        addons.mkdir()
        (addons / "sample").mkdir()
        (addons / "sample/__manifest__.py").write_text("{'name': 'sample'}")
        self.project(DEFAULT_INI + f"path = {odoo}\n[addons.local]\npath = {addons}\n[docker]\nodoo_source = workspace\n")
        self.setup_ws("docker", "--create-docker-deploy")
        for path in ("docker/deploy/Dockerfile", "docker/deploy/.dockerignore", "docker/deploy/requirements/all-requirements.in.txt"):
            self.assertTrue((self.ws / path).exists(), path)
        self.assertIn("COPY addons/", (self.ws / "docker/deploy/Dockerfile").read_text())


class DockerActions(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.project(DEFAULT_INI + "[config]\ndb_name = direct_test\n")
        folder = self.root / "bin"
        folder.mkdir()
        self.log = self.root / "log"
        if os.name != "nt":
            script = folder / "docker"
            script.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$ODT_DIRECT_LOG\"\ncase \"$*\" in\n  *\"SELECT datname FROM pg_database\"*) [ \"${ODT_MOCK_DB_EXISTS:-0}\" != 1 ] || echo direct_test ;;\n  *\"if [ -d\"*) [ \"${ODT_MOCK_FILESTORE_EXISTS:-0}\" != 1 ] && echo missing || echo exists ;;\nesac\nexit 0\n")
            script.chmod(0o755)
        else:
            stub = shutil.which("docker.exe")
            if not stub or "odt-mock-docker" not in Path(stub).parts:
                self.skipTest("Windows Docker mock is available in CI only")
        self.env = dict(ODT_DIRECT_LOG=str(self.log), PATH=str(folder) + os.pathsep + os.environ["PATH"])
        ok(cli("docker", "setup", "--root", self.ws, "--no-provisioning-log", env=self.env))

    def run_cli(self, command, *args):
        return cli("docker", command, "--root", self.ws, *args, env=self.env)


    def test_run_shell_test_and_update_are_forwarded(self):
        ok(self.run_cli("run", "-i", "base", "--stop-after-init"))
        ok(self.run_cli("shell", "--no-http"))
        ok(self.run_cli("test", "-i", "base"))
        ok(self.run_cli("update", "--all"))
        calls = self.log.read_text()
        self.assertIn("compose run", calls)
        self.assertIn("-i base", calls)
        self.assertIn("--entrypoint odoo", calls)
        self.assertIn("click-odoo-update", calls)
        self.assertIn("--update-all", calls)
        self.assertIn("--test-enable", calls)


    def test_reject_missing_backup_before_any_docker_command(self):
        before = self.log.read_text() if self.log.exists() else ""
        fails(self.run_cli("restore-db", "does-not-exist.dump"), "backup not found")
        fails(self.run_cli("restore-filestore", "does-not-exist.tar.gz"), "backup not found")
        self.assertEqual(self.log.read_text() if self.log.exists() else "", before)


    def test_neutralize_preview_requires_no_container(self):
        output = ok(self.run_cli("neutralize", "--dry-run"))
        self.assertIn("SQL preview", output)
        self.assertTrue((self.ws / ".odt-env/neutralize-preview").is_dir())


    def test_restore_database_requires_force_for_existing_database(self):
        dump = self.ws / "odoo-backups/test.dump"
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_bytes(b"fake pg dump")
        self.env["ODT_MOCK_DB_EXISTS"] = "1"
        fails(self.run_cli("restore-db", dump), "Use --force")
        calls = self.log.read_text()
        self.assertNotIn("dropdb", calls)
        ok(self.run_cli("restore-db", "--force", "--move", dump))
        calls = self.log.read_text()
        self.assertIn("dropdb", calls)
        self.assertIn("createdb", calls)
        self.assertIn("pg_restore", calls)


    def test_restore_filestore_requires_force(self):
        archive = self.ws / "odoo-backups/store.tar.gz"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"fake tar archive")
        self.env["ODT_MOCK_FILESTORE_EXISTS"] = "1"
        fails(self.run_cli("restore-filestore", archive), "Use --force")
        ok(self.run_cli("restore-filestore", "--force", archive))
        self.assertIn("--entrypoint sh", self.log.read_text())


    def test_restic_requires_credentials_and_repository(self):
        fails(self.run_cli("backup-filestore-restic"), "RESTIC_REPOSITORY is not set")
        fails(cli("docker", "restic", "--root", self.ws, "snapshots", env=self.env), "RESTIC_REPOSITORY is not set")


    def test_restic_commands_forward_repository_safely(self):
        self.env.update(RESTIC_REPOSITORY=str(self.root / "restic"), RESTIC_PASSWORD="test-secret")
        ok(self.run_cli("backup-filestore-restic"))
        self.assertIn("--entrypoint restic", self.log.read_text())
        self.assertIn("RESTIC_REPOSITORY", self.log.read_text())
        self.env["ODT_MOCK_FILESTORE_EXISTS"] = "1"
        fails(self.run_cli("restore-filestore-restic", "latest"), "Use --force")
        ok(self.run_cli("restore-filestore-restic", "--force", "latest"))
        self.assertIn("restore latest:", self.log.read_text())

def docker(root: Path):
    """Build real local & deploy images; Odoo+OCA run against PostgreSQL."""
    ws = root / "docker"
    ws.mkdir()
    ini = write_ini(ws / "odoo-project.ini", native=False)
    ini.write_text(ini.read_text() + "\n[docker]\nmailpit_enabled = true\nmailpit_webui_port = 18025\n")
    network = "odt-e2e-" + str(os.getpid())
    db = network + "-db"
    image = network + "-deploy"
    try:
        e2e_cli("docker", "setup", "--root", ws, "--sync-addons", "--create-docker-deploy")
        require(ws / "odoo-addons/oca-web/.git")
        require(ws / "docker/local/Dockerfile")
        require(ws / "docker/deploy/Dockerfile")
        require(ws / "docker/deploy/addons/web_notify")
        require(ws / "docker/deploy/addons/helpdesk_mgmt")
        assert not (ws / "docker/local/addons").exists(), "Local mode should bind-mount addons"
        execute("docker", "compose", "config", "--quiet", cwd=ws)
        execute("docker", "compose", "build", "odoo", cwd=ws)
        (ws / "odoo-addons/oca-web/.ci-bind-check").write_text("mounted")
        execute("docker", "compose", "run", "--rm", "--no-deps", "--entrypoint", "sh", "odoo", "-c", "test -f /mnt/extra-addons/oca-web/.ci-bind-check", cwd=ws)
        execute("docker", "compose", "up", "-d", "db", "mailpit", cwd=ws)
        import urllib.request
        for _ in range(30):
            try:
                with urllib.request.urlopen("http://127.0.0.1:18025/readyz", timeout=2):
                    break
            except Exception:
                time.sleep(2)
        else:
            raise AssertionError("Mailpit failed to become ready")
        execute("docker", "compose", "run", "--rm", "--no-deps", "--entrypoint", "python3", "odoo", "-c", "import smtplib; s=smtplib.SMTP('mailpit',1025); s.sendmail('ci@example.test',['test@example.test'],'Subject: ODT integration\\r\\n\\r\\nmailpit test'); s.quit()", cwd=ws)
        with urllib.request.urlopen("http://127.0.0.1:18025/api/v1/messages") as response:
            assert b"ODT integration" in response.read(), "SMTP message not stored in Mailpit"
        execute("docker", "compose", "run", "--rm", "--no-deps", "--entrypoint", "python3", "odoo", "-c", "import poplib; p=poplib.POP3('mailpit',1110); p.user('odoo'); p.pass_('odoo'); assert p.stat()[0] > 0; p.quit()", cwd=ws)
        execute("docker", "compose", "restart", "mailpit", cwd=ws)
        for _ in range(30):
            try:
                with urllib.request.urlopen("http://127.0.0.1:18025/api/v1/messages", timeout=2) as response:
                    if b"ODT integration" in response.read():
                        break
            except Exception:
                pass
            time.sleep(2)
        else:
            raise AssertionError("Mailpit messages were lost after restart")
        execute("docker", "compose", "run", "--rm", "odoo", "--", "-c", "/etc/odoo/odoo.conf", "-d", "odoo", "-i", "web_notify,helpdesk_mgmt", "--without-demo=all", "--stop-after-init", cwd=ws)
        expect_modules(lambda sql: execute("docker", "compose", "exec", "-T", "db", "psql", "-U", "odoo", "-d", "odoo", "-tAc", sql, cwd=ws, capture=True).strip())
        execute("docker", "compose", "up", "-d", "--wait", "--wait-timeout", "240", "odoo", cwd=ws)
        execute("docker", "compose", "exec", "-T", "odoo", "click-odoo-update", "-c", "/etc/odoo/odoo.conf", "-d", "odoo", cwd=ws)
        # Real Docker backup and safe restoration, independent of the host PostgreSQL.
        execute("docker", "compose", "run", "--rm", "--no-deps", "--entrypoint", "sh", "odoo", "-c", "mkdir -p /var/lib/odoo/filestore/odoo && echo marker > /var/lib/odoo/filestore/odoo/ci-marker", cwd=ws)
        db_backup = ws / "odoo-backups/docker.dump"
        fs_backup = ws / "odoo-backups/docker.tar.gz"
        e2e_cli("docker", "backup-db", db_backup, cwd=ws)
        e2e_cli("docker", "backup-filestore", fs_backup, cwd=ws)
        require(db_backup)
        require(fs_backup)
        e2e_cli("docker", "setup", "--root", ws, "--set", "config:db_name=odoo_restore")
        e2e_cli("docker", "restore-db", "--move", db_backup, cwd=ws)
        e2e_cli("docker", "restore-db", db_backup, cwd=ws, expect_failure=True)
        e2e_cli("docker", "restore-filestore", fs_backup, cwd=ws)
        e2e_cli("docker", "restore-filestore", fs_backup, cwd=ws, expect_failure=True)
        e2e_cli("docker", "restore-filestore", "--force", fs_backup, cwd=ws)
        restic_env = {"RESTIC_REPOSITORY": str(root / "restic"), "RESTIC_PASSWORD": "test-password", "RESTIC_HOST": "odoo-compose-ci"}
        e2e_cli("docker", "restic", "--root", ws, "init", env=restic_env)
        e2e_cli("docker", "backup-filestore-restic", cwd=ws, env=restic_env)
        e2e_cli("docker", "setup", "--root", ws, "--set", "config:db_name=odoo_restic_restore")
        restic_env["RESTIC_SOURCE_DB_NAME"] = "odoo_restore"
        e2e_cli("docker", "restore-filestore-restic", "latest", cwd=ws, env=restic_env)
        e2e_cli("docker", "restore-filestore-restic", "latest", cwd=ws, env=restic_env, expect_failure=True)
        e2e_cli("docker", "restore-filestore-restic", "--force", "latest", cwd=ws, env=restic_env)
        execute("docker", "compose", "run", "--rm", "--no-deps", "--entrypoint", "sh", "odoo", "-c", "test -f /var/lib/odoo/filestore/odoo_restic_restore/ci-marker", cwd=ws)
        execute("docker", "build", "-t", image, ws / "docker/deploy")
        execute("docker", "run", "--rm", "--entrypoint", "sh", image, "-c", "test -d /mnt/extra-addons/web_notify && test -d /mnt/extra-addons/helpdesk_mgmt")
        # Verify the deploy image can initialize its own database, not just build.
        execute("docker", "network", "create", network)
        execute("docker", "run", "-d", "--name", db, "--network", network, "-e", "POSTGRES_DB=postgres", "-e", "POSTGRES_USER=odoo", "-e", "POSTGRES_PASSWORD=odoo", "postgres:16")
        for _ in range(30):
            ready = subprocess.run(["docker", "exec", db, "pg_isready", "-U", "odoo", "-d", "postgres"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            if ready:
                break
            time.sleep(2)
        else:
            raise AssertionError("Deploy PostgreSQL did not become ready")
        execute("docker", "run", "--rm", "--network", network, "-e", f"HOST={db}", "-e", "PORT=5432", "-e", "USER=odoo", "-e", "PASSWORD=odoo", image, "--", "-d", "odoo_deploy", "-i", "web_notify,helpdesk_mgmt", "--without-demo=all", "--stop-after-init")
        expect_modules(lambda sql: execute("docker", "exec", db, "psql", "-U", "odoo", "-d", "odoo_deploy", "-tAc", sql).strip())
    finally:
        # Restic runs as root in Docker and creates 0700 directories on the host.
        # Give them back to the pytest user so the temporary workspace can be removed.
        restic_repo = root / "restic"
        if restic_repo.is_dir():
            subprocess.run(
                ["docker", "compose", "run", "--rm", "--no-deps", "-T",
                 "--user", "0:0", "-v", f"{restic_repo}:/odt-test-restic",
                 "--entrypoint", "sh", "odoo", "-c",
                 f"chown -R {os.getuid()}:{os.getgid()} /odt-test-restic"],
                cwd=ws, check=False,
            )
        # Cleanup always; preserve original failure when diagnostics also fail.
        for cmd in (("docker", "compose", "logs", "--no-color", "--tail=80"), ("docker", "compose", "down", "-v", "--remove-orphans")):
            subprocess.run(cmd, cwd=ws, check=False)
        subprocess.run(["docker", "rm", "-f", db], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        subprocess.run(["docker", "network", "rm", network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    print("Docker integration passed", flush=True)


@pytest.mark.integration
def test_real_docker_workflow(linux_docker):
    """Real Docker + Odoo, OCA modules, Mailpit, backups, restores, Restic."""
    run_scenario(docker)
