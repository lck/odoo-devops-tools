"""Direct regression tests for the extracted, independently testable helpers."""

import argparse
import configparser
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import pytest

from odoo_devops_tools import odoo_compose as odt


def project(tmp_path, extra=""):
    ini = tmp_path / "odoo-project.ini"
    ini.write_text("[odoo]\nversion = 19.0\n" + extra, encoding="utf-8")
    return odt.load_project_config(ini, log_resolved=False)


def test_main_does_not_mutate_argv(capsys):
    original = sys.argv[:]
    for workflow in ("docker", "venv"):
        with pytest.raises(SystemExit) as exc:
            odt.main([workflow, "setup", "--help"])
        assert exc.value.code == 0
        assert "setup" in capsys.readouterr().out
        assert sys.argv == original


def test_explicit_ini_keys_exclude_defaults():
    cp = configparser.ConfigParser()
    cp.read_string("[DEFAULT]\nshared = x\n[config]\ndb_name = test_db\n")
    assert list(odt._explicit_ini_keys(cp, "config")) == ["db_name"]
    assert odt._parse_odoo_config(cp) == {"db_name": "test_db"}
    assert list(odt._explicit_ini_keys(cp, "missing")) == []


def test_parse_project_sections(tmp_path):
    cfg = project(tmp_path, "\n[virtualenv]\nrequirements = demo==1\n"
                  "[addons.example]\nrepo = https://example.com/repo.git\nbranch = 19.0\n"
                  "[docker.share.logs]\nsource = ./logs\ntarget = /mnt/logs\n"
                  "[config]\ndb_name = example\n")
    assert cfg.odoo.branch == "19.0"
    assert cfg.virtualenv.explicit_requirements == ["demo==1"]
    assert cfg.addons["example"].branch == "19.0"
    assert cfg.docker.shares["logs"].target == "/mnt/logs"
    assert cfg.config == {"db_name": "example"}


@pytest.mark.parametrize("name,valid", [("a_1-b.c", True), ("a" * 63, True),
    ("a" * 64, False), ("", False), ("../db", False), ("a/b", False), ("ž", False)])
def test_db_name_validation(name, valid):
    assert odt._valid_db_name(name) is valid
    if valid:
        assert odt._validate_db_name(name) == name
    else:
        with pytest.raises(Exception, match="Invalid.*db_name"):
            odt._validate_db_name(name)


def test_db_name_default_policies(tmp_path):
    cfg = project(tmp_path)
    assert odt._docker_default_db_name(cfg) == "odoo"
    with pytest.raises(Exception, match="Native database commands require"):
        odt._native_default_db_name(cfg)


@pytest.mark.parametrize("shallow,commit", [(True, None), (False, None), (False, "abc123")])
@pytest.mark.parametrize("spec_type", [odt.OdooSpec, odt.AddonSpec])
def test_sync_git_source_keeps_checkout_policy(tmp_path, shallow, commit, spec_type):
    kwargs = dict(repo="example", branch="19.0", shallow=shallow, commit=commit)
    if spec_type is odt.OdooSpec:
        kwargs["version"] = "19.0"
    spec = spec_type(**kwargs)
    with patch.object(odt, "ensure_repo") as ensure, \
            patch.object(odt, "checkout_branch") as branch, \
            patch.object(odt, "checkout_commit") as checkout:
        odt._sync_git_source(tmp_path, spec)
    short = bool(commit or shallow)
    depth = 1 if short else None
    ensure.assert_called_once_with("example", tmp_path, branch="19.0", depth=depth,
                                   single_branch=short, fetch_all=not short)
    if commit:
        checkout.assert_called_once_with(tmp_path, commit, branch="19.0", fetch_all=False, depth=1)
        branch.assert_not_called()
    else:
        expected = dict(fetch_all=not short, depth=depth)
        if shallow:
            expected["already_fetched"] = True
        branch.assert_called_once_with(tmp_path, "19.0", **expected)
        checkout.assert_not_called()


@pytest.mark.parametrize("platform_name,folder,extension", [("nt", "Scripts", ".exe"), ("posix", "bin", "")])
def test_venv_executable_platform_paths(tmp_path, platform_name, folder, extension):
    # Use an already constructed Path; no platform-specific filesystem needed.
    expected = tmp_path / folder / ("click-odoo" + extension)
    with patch.object(odt.os, "name", platform_name):
        actual = odt._venv_executable(tmp_path, "click-odoo")
    assert actual == expected


def test_workspace_data_dir_resolution(tmp_path):
    cfg = project(tmp_path, "\n[config]\ndata_dir = data/custom\ndb_name = db\n")
    layout = odt.Layout.from_root(tmp_path)
    assert layout.venv_dir == tmp_path / "venv"
    assert layout.project_ini == tmp_path / "odoo-project.ini"
    assert layout.state_dir == tmp_path / ".odt-env"
    assert odt._workspace_filestore_path("venv", tmp_path, cfg, "db") == tmp_path.resolve() / "data/custom/filestore/db"
    assert odt._workspace_filestore_path("docker", tmp_path, cfg, "db") == "/var/lib/odoo/filestore/db"


def test_render_dockerfile_matches_writer(tmp_path):
    cfg = project(tmp_path)
    kwargs = dict(has_constraints=True, has_build_constraints=True, include_addons=True, include_config=True)
    content = odt.render_dockerfile(cfg, **kwargs)
    assert "COPY requirements/constraints.txt" in content
    assert "COPY requirements/build-constraints.txt" in content
    path = odt.write_dockerfile(tmp_path, cfg, **kwargs)
    assert path.read_text(encoding="utf-8") == content


def test_docker_command_builder(tmp_path):
    cfg = project(tmp_path, "\n[config]\ndb_name = demo\n")
    with patch.object(odt, "_workspace_docker_command", return_value=["docker"]):
        cmd = odt._build_docker_odoo_command("test", cfg, argparse.Namespace(), ["-i", "base"])
        assert cmd == ["docker", "compose", "run", "--rm", "--no-deps", "-T", "odoo", "--", "-c",
                       "/etc/odoo/odoo.conf", "-d", "demo", "--test-enable", "--stop-after-init", "-i", "base"]
        cmd = odt._build_docker_odoo_command("update", cfg, argparse.Namespace(all=True), [])
        assert cmd[-1] == "--update-all"


def test_native_command_builder(tmp_path):
    cfg = project(tmp_path)
    layout = odt.Layout.from_root(tmp_path)
    layout.conf_path.parent.mkdir()
    layout.conf_path.touch()
    python = odt._venv_executable(layout.venv_dir)
    python.parent.mkdir(parents=True)
    python.touch()
    layout.odoo_dir.mkdir()
    odoo_bin = layout.odoo_dir / "odoo-bin"
    odoo_bin.touch()
    cmd = odt._build_native_odoo_command("test", tmp_path, cfg, argparse.Namespace(), ["-i", "base"])
    assert cmd == [str(python), str(odoo_bin), "-c", str(layout.conf_path),
                   "--test-enable", "--stop-after-init", "-i", "base"]


def test_filestore_guard_keeps_docker_user_distinction(tmp_path):
    with patch.object(odt, "_workspace_docker_command", return_value=["docker"]), \
            patch.object(odt, "_workspace_command", return_value=b"exists") as run:
        with pytest.raises(odt._WorkspaceOperationError, match="Use --force"):
            odt._check_filestore_restore("docker", tmp_path, "db", "/path", False)
        assert "--user" not in run.call_args.args[0]
        odt._check_filestore_restore("docker", tmp_path, "db", "/path", True, user_args=("--user", "0:0"))
        cmd = run.call_args.args[0]
        assert cmd[cmd.index("--user") + 1] == "0:0"


def test_native_filestore_replacement(tmp_path):
    store = tmp_path / "filestore"
    store.mkdir()
    (store / "old").touch()
    with pytest.raises(odt._WorkspaceOperationError, match="Use --force"):
        odt._check_filestore_restore("venv", tmp_path, "db", str(store), False)
    assert (store / "old").exists()
    odt._prepare_native_filestore(str(store))
    assert store.is_dir() and not list(store.iterdir())


@pytest.mark.parametrize("name", ["../escape", "/odoo/file", "odoo/../escape", "odoo\\file", "other/file"])
def test_bundle_rejects_unsafe_paths(name):
    with pytest.raises(Exception, match="(Unsafe|Unsupported)"):
        odt._validate_bundle_member(zipfile.ZipInfo(name))


def test_bundle_rejects_normalized_backslash_path():
    info = zipfile.ZipInfo("odoo\\file")
    # Simulate Windows normalization even when running on Linux.
    info.filename = "odoo/file"
    with pytest.raises(Exception, match="Unsafe"):
        odt._validate_bundle_member(info)


def test_bundle_member_and_checksum(tmp_path):
    assert odt._validate_bundle_member(zipfile.ZipInfo("odoo/requirements.txt")) == PurePosixPath("odoo/requirements.txt")
    link = zipfile.ZipInfo("odoo/link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(Exception, match="Symbolic links"):
        odt._validate_bundle_member(link)
    for name in ("odoo", "odoo-addons", "wheelhouse"):
        (tmp_path / name).mkdir()
    (tmp_path / "odoo-project.ini").write_text("[odoo]\nversion = 19.0\n", encoding="utf-8")
    lock = tmp_path / "wheelhouse" / "all-requirements.lock.txt"
    lock.write_bytes(b"package==1\n")
    manifest = {"files": odt._bundle_file_manifest(tmp_path)}
    odt._verify_extracted_bundle(tmp_path, manifest)
    lock.write_bytes(b"package==2\n")
    with pytest.raises(Exception, match="checksum failed"):
        odt._verify_extracted_bundle(tmp_path, manifest)
