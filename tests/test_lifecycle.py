"""Runtime lifecycle with cheap stand-ins; real Odoo runs in integration jobs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest

from tests.common import WorkspaceTest, cli, fails, ok, run


class NativeLifecycle(WorkspaceTest):
    def test_up_down_status_and_stale_pid(self):
        odoo = self.ws / "odoo"
        odoo.mkdir()
        (odoo / "odoo-bin").write_text("import time\nprint('Odoo started', flush=True)\nwhile True: time.sleep(.5)\n")
        self.project("[odoo]\nversion = 19.0\npath = odoo\n[config]\ndb_name = lifecycle_test\n")
        ok(run(sys.executable, "-m", "venv", self.ws / "venv", timeout=180))

        def invoke(*args):
            return self.invoke("venv", *args)

        state = self.ws / ".odt-env/odoo-process.json"
        self.addCleanup(lambda: cli("venv", "down", "--root", self.ws))
        self.assertIn("stopped", ok(invoke("status")))
        self.assertIn("started", ok(invoke("up")))
        first = json.loads(state.read_text())
        self.assertIn("running", ok(invoke("status")))
        self.assertIn("already running", ok(invoke("up")))
        self.project("[odoo]\nversion = 19.0\npath = odoo\n[config]\ndb_name = lifecycle_test\nlog_level = debug\n")
        self.assertIn("started", ok(invoke("up")))
        second = json.loads(state.read_text())
        self.assertNotEqual(first["identity"], second["identity"])
        self.assertIn("stopped", ok(invoke("down")))
        self.assertIn("already stopped", ok(invoke("down")))
        state.write_text(json.dumps(dict(pid=os.getpid(), root=str(self.ws), identity="wrong")))
        self.assertIn("stale", ok(invoke("status")))
        self.assertIn("already stopped", ok(invoke("down")))


@unittest.skipIf(os.name == "nt", "Fake POSIX docker executable; Windows generation is tested elsewhere")
class DockerLifecycle(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.project()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "docker.log"
        (self.bin / "docker").write_text("""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  'compose ps -q odoo')
    [[ ! -f "${FAKE_DOCKER_LOG}.started" ]] || echo fake-container
    ;;
  'compose up '*) touch "${FAKE_DOCKER_LOG}.started" ;;
  'compose down') rm -f "${FAKE_DOCKER_LOG}.started" ;;
esac
""")
        (self.bin / "docker").chmod(0o755)
        self.env = dict(FAKE_DOCKER_LOG=str(self.log), PATH=str(self.bin) + os.pathsep + os.environ["PATH"])

    def command(self, name, *args):
        return ok(cli("docker", name, *args, "--root", self.ws, env=self.env))

    def test_regeneration_selective_recreate_build_and_down(self):
        self.command("status")
        self.assertIn("compose ps -a", self.log.read_text())
        self.command("up")
        self.assertIn("compose up -d --wait", self.log.read_text())
        self.assertTrue((self.ws / "compose.yaml").exists())
        self.command("up")
        self.assertNotIn("--force-recreate", self.log.read_text())
        self.project("[odoo]\nversion = 19.0\n[config]\ndev_mode = all\n")
        self.command("setup")
        self.command("up")
        self.assertIn("--no-deps --force-recreate odoo", self.log.read_text())
        self.command("up", "--build", "--recreate")
        self.assertIn("compose up -d --wait --build", self.log.read_text())
        self.assertEqual(self.log.read_text().count("--force-recreate odoo"), 2)
        self.command("down")
        self.assertIn("compose down", self.log.read_text())
        self.assertNotIn("--volumes", self.log.read_text())
