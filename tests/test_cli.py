"""Command-line interface and workflow selection."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from tests.common import WorkspaceTest, cli, fails, ok, run


class CLIContract(WorkspaceTest):

    def test_workflow_is_mandatory(self):
        for command in ("setup", "up", "down", "status", "run", "update", "backup-db"):
            with self.subTest(command=command):
                fails(cli(command), "Missing workflow")
        self.assertIn("Workflows", ok(cli("--help")))
        self.assertRegex(ok(cli("--version")), r"odoo-compose [0-9]+\.[0-9]+\.[0-9]+")
        self.assertIn("Workflows", ok(cli()))
        fails(cli("docker", "completion", "bash"))

    def test_workflow_specific_help_and_standalone(self):
        for workflow, present, absent in (
            ("docker", "--create-docker-deploy", "--create-venv"),
            ("venv", "--create-venv", "--create-docker-deploy"),
        ):
            help_text = ok(cli(workflow, "setup", "--help"))
            self.assertIn(present, help_text)
            self.assertNotIn(absent, help_text)
            self.assertIn("Commands:", ok(cli(workflow, "--help")))
            self.assertIn(workflow, ok(cli(workflow, "--version")))
        self.assertIn("--create-venv", ok(cli("venv", "setup", "--help")))
        self.assertIn("--all", ok(cli("venv", "update", "--help")))
        self.assertIn("--create-venv", ok(run(sys.executable, "-S", str(Path(__file__).resolve().parents[1] / "odoo_devops_tools/odoo_compose.py"), "venv", "setup", "--help")))

    def test_workspace_root_shorthand_and_duplicate_flags(self):
        path = self.root / "new"
        ok(cli("venv", "setup", path, "--no-provisioning-log"))
        self.assertTrue((path / "odoo-project.ini").exists())
        fails(cli("venv", "setup", path, "--root", path))
        fails(cli("venv", "setup", "--sync", "--sync-addons", "--root", path))
        fails(cli("venv", "setup", "--sync", "--create-from-bundle", "missing.zip", "--root", path))

    def test_invalid_docker_only_options_for_venv(self):
        fails(cli("venv", "setup", "--root", self.ws, "--create-docker-deploy"))
        fails(cli("docker", "setup", "--root", self.ws, "--create-venv"))
