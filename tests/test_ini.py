"""INI settings, validation, merging and external templates."""

from __future__ import annotations

import os
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import threading

from tests.common import DEFAULT_INI, WorkspaceTest, cli, fails, ok
from tests.test_git import git


class IniConfiguration(WorkspaceTest):

    def test_ini_layers_and_overrides(self):
        base = self.root / "base.ini"
        base.write_text("[odoo]\nversion = 19.0\n[vars]\nsuffix = 42\n[config]\ndb_name = test${vars:suffix}\n", encoding="utf-8")
        addons = self.root / "extras.ini"
        addons.write_text("[config]\nlog_level = info\n", encoding="utf-8")
        ok(cli("venv", "setup", base, "-i", addons, "--root", self.ws, "-e", "suffix=99", "--set", "config:log_level=debug", "--no-provisioning-log"))
        ini = (self.ws / "odoo-project.ini").read_text()
        conf = (self.ws / "odoo-configs/odoo-server.conf").read_text()
        self.assertIn("db_name = test99", conf)
        self.assertIn("log_level = debug", conf)
        self.assertIn("suffix = 99", ini)
        self.assertIn("log_level = debug", ini)
        self.assertTrue((self.ws / ".odt-env/last-resolved-project.ini").exists())

    def test_overrides_do_not_duplicate_existing_sections(self):
        self.project("[odoo]\nversion = 19.0\n[config]\ndb_name = a\n")
        self.setup_ws("venv", "--set", "config:db_name=b")
        ini = (self.ws / "odoo-project.ini").read_text()
        self.assertEqual(ini.count("[config]"), 1)
        self.assertIn("db_name = b", ini)

    def test_default_database_name_and_custom_data_dir(self):
        self.project("[odoo]\nversion = 19.0\n[config]\ndata_dir = custom-data\n")
        self.setup_ws()
        conf = (self.ws / "odoo-configs/odoo-server.conf").read_text()
        self.assertIn("data_dir", conf)
        self.assertIn("custom-data", conf)
        self.assertIn("db_name", (self.ws / "odoo-project.ini").read_text())

    def test_unsafe_addon_section_names(self):
        for name in ("..", "../outside", "nested/repo", r"nested\repo", "C:drive", "repo&other"):
            with self.subTest(name=name):
                self.project(DEFAULT_INI + f"[addons.{name}]\nrepo = https://example.invalid/addon.git\n")
                fails(self.invoke("venv", "setup", "--no-provisioning-log"), "Invalid addon section name")

    @unittest.skipIf(os.name == "nt", "Windows symlinks require developer-mode privileges")

    def test_symlink_escape_rejected_for_implicit_addons(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.ws / "odoo-addons").mkdir()
        (self.ws / "odoo-addons/safe").symlink_to(outside, target_is_directory=True)
        self.project(DEFAULT_INI + "[addons.safe]\nrepo = https://example.invalid/repo\nbranch = 19.0\n")
        fails(self.invoke("venv", "setup", "--no-provisioning-log"), "Unsafe addon destination")
        self.assertFalse((outside / ".git").exists())
        self.project(DEFAULT_INI + f"[addons.safe]\npath = {outside}\n")
        self.setup_ws()
        self.assertIn(str(outside), (self.ws / "odoo-configs/odoo-server.conf").read_text())

    def test_reject_invalid_database_names(self):
        for value in ("a; echo BAD", "name/other", "../db"):
            with self.subTest(value=value):
                self.project(DEFAULT_INI + f"[config]\ndb_name = {value}\n")
                fails(self.invoke("docker", "setup", "--no-provisioning-log"))


class IniSourceTests(WorkspaceTest):

    def test_layers_apply_in_order_then_cli_overrides(self):
        base = self.root / "base.ini"
        base.write_text(DEFAULT_INI + "[config]\ndb_name = base_db\n[docker]\nrestart = unless-stopped\n")
        extra = self.root / "extra.ini"
        extra.write_text("[config]\ndb_name = second_db\n[docker]\nrestart = always\n")
        ok(cli("venv", "setup", base, "-i", extra, "--root", self.ws, "--no-provisioning-log"))
        source = (self.ws / "odoo-project.ini").read_text()
        self.assertIn("db_name = second_db", source)
        self.assertIn("restart = always", source)
        ok(cli("venv", "setup", base, "-i", extra, "--root", self.ws, "--no-provisioning-log", "-S", "docker:restart=no", "--set", "config:db_name=cli_db"))
        source = (self.ws / "odoo-project.ini").read_text()
        self.assertIn("db_name = cli_db", source)
        self.assertIn("restart = no", source)
        self.assertTrue((self.ws / ".odt-env/last-source-project.d").is_dir())


    def test_invalid_overrides_leave_project_unchanged(self):
        path = self.project(DEFAULT_INI + "[config]\ndb_name = good_db\n")
        before = path.read_bytes()
        for args in (
            ("--set", "docker:restart=invalid"),
            ("--set", "docker:odoo_cpus=-1"),
            ("--set", "docker:not_a_supported_key=123"),
            ("--set", "config:addons_path=unsafe"),
            ("--set", "malformed"),
            ("-e", "malformed"),
        ):
            with self.subTest(args=args):
                fails(self.invoke("venv", "setup", "--no-provisioning-log", *args))
                self.assertEqual(path.read_bytes(), before)


    def test_ini_interpolation_preserves_literals(self):
        self.project(
            DEFAULT_INI + "[vars]\nn = 96\n"
            "[config]\n"
            "dbfilter = ^db${vars:n}$\n"
            "price = $100\n"
            "path_text = $HOME/odoo\n"
            "escaped = \\${vars:n}\n"
        )
        self.setup_ws("venv")
        resolved = (self.ws / ".odt-env/last-resolved-project.ini").read_text()
        self.assertIn("dbfilter = ^db96$", resolved)
        self.assertIn("price = $100", resolved)
        self.assertIn("path_text = $HOME/odoo", resolved)
        self.assertIn(r"escaped = \${vars:n}", resolved)
        self.setup_ws("venv", "-e", "n=97")
        self.assertIn("dbfilter = ^db97$", (self.ws / "odoo-configs/odoo-server.conf").read_text())
        self.assertIn("dbfilter = ^db${vars:n}$", (self.ws / "odoo-project.ini").read_text())


    def test_comments_unicode_and_crlf_are_preserved(self):
        text = "# príklad\r\n[odoo]\r\nversion = 19.0\r\n\r\n[docker]\r\n# keep this comment\r\nrestart = always\r\n"
        self.project(text)
        self.setup_ws("docker", "--set", "docker:restart=no")
        content = (self.ws / "odoo-project.ini").read_bytes()
        self.assertIn("# príklad".encode(), content)
        self.assertIn(b"# keep this comment", content)
        self.assertIn(b"\r\n", content)
        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))


    def test_git_ini_source_and_refresh(self):
        repository = self.root / "ini-repo"
        repository.mkdir()
        git(repository, "init", "-q", "-b", "main")
        ini = repository / "project.ini"
        ini.write_text("# template comment\n" + DEFAULT_INI + "[docker]\nrestart = unless-stopped\n")
        git(repository, "add", ".")
        git(repository, "commit", "-qm", "fixture")
        url = "git+" + repository.resolve().as_uri() + "//project.ini?ref=main"
        ok(cli("venv", "setup", url, "--root", self.ws, "--set", "docker:restart=no", "--no-provisioning-log"))
        project = (self.ws / "odoo-project.ini").read_text()
        self.assertIn("# template comment", project)
        self.assertIn("restart = no", project)
        self.assertIn("restart = unless-stopped", ini.read_text())
        ok(cli("venv", "setup", url, "--root", self.ws, "--no-provisioning-log"))
        self.assertIn("restart = unless-stopped", (self.ws / "odoo-project.ini").read_text())


    def test_http_ini_source_and_refresh(self):
        document = self.root / "served"
        document.mkdir()
        ini = document / "project.ini"
        ini.write_text("# template comment\n" + DEFAULT_INI + "[docker]\nrestart = unless-stopped\n")
        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kw):
                super().__init__(*args, directory=str(document), **kw)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/project.ini"
        ok(cli("venv", "setup", url, "--root", self.ws, "--set", "docker:restart=no", "--no-provisioning-log"))
        self.assertIn("restart = no", (self.ws / "odoo-project.ini").read_text())
        self.assertIn("restart = unless-stopped", ini.read_text())
        ok(cli("venv", "setup", url, "--root", self.ws, "--no-provisioning-log"))
        self.assertIn("restart = unless-stopped", (self.ws / "odoo-project.ini").read_text())


    def test_success_manifest_and_last_run(self):
        self.project()
        ok(self.invoke("venv", "setup"))
        state = self.ws / ".odt-env/last-provisioning.json"
        self.assertTrue(state.is_file())
        self.assertEqual(__import__("json").loads(state.read_text())["status"], "success")
        output = ok(self.invoke("venv", "setup", "--show-last-run"))
        self.assertIn("success", output)
