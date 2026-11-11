"""Portable bundle creation, integrity checks and offline restoration."""

import hashlib
import json
from pathlib import Path
import shutil
import zipfile

import pytest

from tests.conftest import command
from tests.integration_support import (
    cli as e2e_cli, expect_modules, pg, require, run_scenario, write_ini,
)


def verify_bundle(path: Path):
    with zipfile.ZipFile(path) as archive:
        members = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == "odt-env-workspace-bundle"
        assert manifest["format_version"] == 1
        assert "odoo-project.ini" in members
        assert "wheelhouse/all-requirements.lock.txt" in members
        assert any(name.endswith(".whl") for name in members)
        for name in members:
            assert not {".git", "venv", "odoo-data", "odoo-logs", "odoo-backups"}.intersection(Path(name).parts), name
        expected = {item["path"]: item for item in manifest["files"]}
        assert set(expected) == {n for n in members if not n.endswith("/") and n != "manifest.json"}
        for name, entry in expected.items():
            content = archive.read(name)
            assert len(content) == entry["size"], name
            assert hashlib.sha256(content).hexdigest() == entry["sha256"], name
        assert "db_password = odoo" not in archive.read("odoo-project.ini").decode()
        assert manifest["sources"]["addons"]["oca-web"]["snapshot"]["dirty"]


def bundle(root: Path):
    """Portable bundle: dirty-tree protection, SHA256, tamper rejection, offline restore."""
    ini = write_ini(root / "project.ini")
    source = root / "source"
    source.mkdir()
    e2e_cli("venv", "setup", ini, "--root", source, "--sync-all", "--create-venv")
    (source / "odoo-addons/oca-web/odt-bundle-dirty-marker.txt").write_text("dirty")
    e2e_cli("venv", "setup", ini, "--root", source, "--create-bundle", expect_failure=True)
    expected = source / "bundles/source-bundle.zip"
    assert not expected.exists(), "Dirty bundle must be rejected"
    e2e_cli("venv", "setup", ini, "--root", source, "--create-bundle", "--allow-dirty-bundle")
    require(expected)
    verify_bundle(expected)
    artifact = root / "bundle.zip"
    shutil.copy2(expected, artifact)
    tampered = root / "tampered.zip"
    with zipfile.ZipFile(artifact) as original, zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as out:
        for member in original.infolist():
            content = original.read(member.filename)
            if member.filename == "odoo-project.ini":
                content += b"\n# modified after checksum\n"
            out.writestr(member, content)
    e2e_cli("venv", "setup", "--root", root / "tampered", "--create-from-bundle", tampered, expect_failure=True, env={"UV_OFFLINE": "1"})
    assert not list((root / "tampered").iterdir()), "Tampered archive left workspace files"

    # Delete checkout to prove the bundle is self-contained. uv must not access the network.
    shutil.rmtree(source)
    restored = root / "restored"
    e2e_cli("venv", "setup", "--root", restored, "--create-from-bundle", artifact,
        "--set", "config:db_password=odoo", "--set", "config:db_name=odoo_bundle", env={"UV_OFFLINE": "1", "PIP_NO_INDEX": "1"})
    require(restored / "odoo/odoo-bin")
    require(restored / "odoo-addons/oca-web/odt-bundle-dirty-marker.txt")
    require(restored / ".odt-env/imported-bundle-manifest.json")
    assert not (restored / "odoo/.git").exists()
    assert not (restored / "odoo-addons/oca-web/.git").exists()
    e2e_cli("venv", "run", "-i", "web_notify,helpdesk_mgmt", "--stop-after-init", cwd=restored)
    expect_modules(lambda sql: pg("odoo_bundle", sql))
    print("Portable bundle integration passed", flush=True)


@pytest.mark.integration
def test_portable_bundle(integration_stack):
    """Create and restore a verified portable bundle without network access."""
    command(*integration_stack, "run", "--rm", "-T", "--no-deps", "runner",
            "python3", "-m", "tests.test_bundle")


if __name__ == "__main__":
    run_scenario(bundle)
