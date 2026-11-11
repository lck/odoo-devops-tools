"""Git source selection and data-loss protections, using local repositories only."""

from __future__ import annotations

from pathlib import Path

from tests.common import DEFAULT_INI, WorkspaceTest, cli, fails, ok, run

GIT_ENV = dict(GIT_AUTHOR_NAME="CI", GIT_AUTHOR_EMAIL="ci@example.test", GIT_COMMITTER_NAME="CI", GIT_COMMITTER_EMAIL="ci@example.test", GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="maintenance.auto", GIT_CONFIG_VALUE_0="false")


def git(repo, *args):
    return ok(run("git", "-C", repo, *args, env=GIT_ENV))


def create_repo(path: Path, *, odoo=False) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "marker.txt").write_text("initial\n")
    if odoo:
        (path / "odoo-bin").write_text("#!/usr/bin/env python3\n")
        (path / "requirements.txt").write_text("")
    git(path, "add", ".")
    git(path, "commit", "-qm", "initial")
    return path


class GitSourceSelection(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.odoo = create_repo(self.root / "fixture-odoo", odoo=True)
        self.addon = create_repo(self.root / "fixture-addons")

    def config(self, odoo="git", addon="git", docker_source="image"):
        odoo_line = f"repo = {self.odoo}\nbranch = main\n" if odoo == "git" else f"path = {self.odoo}\n"
        addons_line = f"repo = {self.addon}\nbranch = main\n" if addon == "git" else f"path = {self.addon}\n"
        self.project("[odoo]\nversion = 19.0\n" + odoo_line + "[addons.one]\n" + addons_line + f"[docker]\nodoo_source = {docker_source}\n")

    def test_auto_sync_selects_sources_for_each_workflow(self):
        for flow, source, need_odoo in (("venv", "image", True), ("docker", "image", False), ("docker", "workspace", True)):
            with self.subTest(flow=flow, source=source):
                ws = self.root / (flow + "-" + source)
                ws.mkdir()
                self.project("[odoo]\nversion = 19.0\n" + f"repo = {self.odoo}\nbranch = main\n" + f"[addons.one]\nrepo = {self.addon}\nbranch = main\n" + f"[docker]\nodoo_source = {source}\n", ws)
                ok(cli(flow, "setup", "--root", ws, "--sync", "--no-provisioning-log"))
                self.assertTrue((ws / "odoo-addons/one/marker.txt").exists())
                self.assertEqual((ws / "odoo/odoo-bin").exists(), need_odoo)

    def test_explicit_sync_all_clones_odoo_even_in_image_mode(self):
        self.config()
        self.setup_ws("docker", "--sync-all")
        self.assertTrue((self.ws / "odoo/odoo-bin").exists())
        self.assertTrue((self.ws / "odoo-addons/one/marker.txt").exists())

    def test_local_sources_are_not_cloned(self):
        self.config(odoo="local", addon="git")
        self.setup_ws("venv", "--sync")
        self.assertFalse((self.ws / "odoo/.git").exists())
        self.assertTrue((self.ws / "odoo-addons/one/marker.txt").exists())
        fresh = self.root / "all-local"
        fresh.mkdir()
        self.project("[odoo]\nversion = 19.0\npath = " + str(self.odoo) + "\n[addons.one]\npath = " + str(self.addon) + "\n", fresh)
        ok(cli("venv", "setup", "--root", fresh, "--sync", "--no-provisioning-log"))
        self.assertFalse((fresh / "odoo-addons/one/.git").exists())

    def test_ini_includes_select_git_sources(self):
        self.project(DEFAULT_INI)
        extra = self.root / "addons.ini"
        extra.write_text(f"[addons.one]\nrepo = {self.addon}\nbranch = main\n")
        ok(cli("docker", "setup", self.ws / "odoo-project.ini", "--root", self.ws, "-i", extra, "--sync", "--no-provisioning-log"))
        self.assertTrue((self.ws / "odoo-addons/one/marker.txt").is_file())

    def test_variable_override_selects_workspace_odoo(self):
        self.config(docker_source="image")
        config = (self.ws / "odoo-project.ini").read_text()
        config += "[vars]\nsource = image\n"
        config = config.replace("odoo_source = image", "odoo_source = ${vars:source}")
        self.project(config)
        self.setup_ws("docker", "-e", "source=workspace", "--sync")
        self.assertTrue((self.ws / "odoo/odoo-bin").exists())

    def test_explicit_git_commit_pins_workspace_source(self):
        old = git(self.odoo, "rev-parse", "HEAD").strip()
        (self.odoo / "marker.txt").write_text("new version")
        git(self.odoo, "add", ".")
        git(self.odoo, "commit", "-qm", "new version")
        self.project(f"[odoo]\nversion = 19.0\nrepo = {self.odoo}\nbranch = main\ncommit = {old}\n")
        self.setup_ws("venv", "--sync")
        self.assertEqual(git(self.ws / "odoo", "rev-parse", "HEAD").strip(), old)
        self.assertEqual((self.ws / "odoo/marker.txt").read_text(), "initial\n")

    def test_unpublished_local_commit_is_never_reset(self):
        # One representative protected repository; shallow + both types below.
        self.config(docker_source="workspace")
        self.setup_ws("docker", "--sync")
        checkout = self.ws / "odoo"
        (checkout / "unpublished.txt").write_text("keep me")
        git(checkout, "add", ".")
        git(checkout, "commit", "-qm", "local unpublished")
        tip = git(checkout, "rev-parse", "HEAD").strip()
        fails(self.invoke("docker", "setup", "--sync", "--no-provisioning-log"), "Refusing to reset local Git branch")
        self.assertEqual(git(checkout, "rev-parse", "HEAD").strip(), tip)
        self.assertTrue((checkout / "unpublished.txt").exists())

    def test_dirty_checkout_is_not_overwritten(self):
        self.config(docker_source="workspace")
        self.setup_ws("docker", "--sync")
        (self.ws / "odoo/marker.txt").write_text("uncommitted")
        fails(self.invoke("docker", "setup", "--sync", "--no-provisioning-log"))
        self.assertEqual((self.ws / "odoo/marker.txt").read_text(), "uncommitted")

    def test_shallow_clone_fast_forward_and_divergence(self):
        # Tests real fetch/branch decisions without network calls.
        upstream = self.root / "upstream"
        create_repo(upstream, odoo=True)
        bare = self.root / "origin.git"
        ok(run("git", "clone", "-q", "--bare", upstream, bare, env=GIT_ENV))
        repo_url = bare.resolve().as_uri()
        self.project(f"[odoo]\nversion = 19.0\nrepo = {repo_url}\nbranch = main\nshallow = true\n[docker]\nodoo_source = workspace\n")
        self.setup_ws("docker", "--sync")
        checkout = self.ws / "odoo"
        self.assertEqual(git(checkout, "rev-parse", "--is-shallow-repository").strip(), "true")
        # Local unpublished commit is protected even after changing checkout branch.
        (checkout / "local.txt").write_text("mine")
        git(checkout, "add", ".")
        git(checkout, "commit", "-qm", "local")
        tip = git(checkout, "rev-parse", "HEAD").strip()
        git(checkout, "switch", "-q", "-c", "other")
        fails(self.invoke("docker", "setup", "--sync", "--no-provisioning-log"), "Refusing to reset local Git branch")
        self.assertEqual(git(checkout, "rev-parse", "main").strip(), tip)
        git(checkout, "switch", "-q", "main")
        git(checkout, "push", "-q", "origin", "main")
        self.setup_ws("docker", "--sync")
        publisher = self.root / "publisher"
        ok(run("git", "clone", "-q", bare, publisher, env=GIT_ENV))
        (publisher / "marker.txt").write_text("remote")
        git(publisher, "add", ".")
        git(publisher, "commit", "-qm", "remote")
        git(publisher, "push", "-q", "origin", "main")
        remote_tip = git(publisher, "rev-parse", "HEAD").strip()
        self.setup_ws("docker", "--sync")
        self.assertEqual(git(checkout, "rev-parse", "HEAD").strip(), remote_tip)
        (checkout / "divergent.txt").write_text("mine again")
        git(checkout, "add", ".")
        git(checkout, "commit", "-qm", "divergent")
        new_tip = git(checkout, "rev-parse", "HEAD").strip()
        (publisher / "marker.txt").write_text("another remote")
        git(publisher, "add", ".")
        git(publisher, "commit", "-qm", "remote 2")
        git(publisher, "push", "-q", "origin", "main")
        fails(self.invoke("docker", "setup", "--sync", "--no-provisioning-log"), "Refusing to reset local Git branch")
        self.assertEqual(git(checkout, "rev-parse", "HEAD").strip(), new_tip)
