"""Installer checks use a temporary app, real SQLite, and a fake service runner."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

SPEC = importlib.util.spec_from_file_location("hedge_installer", Path(__file__).parents[1] / "tools/install_hedge_release.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.fixture()
def installation(tmp_path, monkeypatch):
    source, target = tmp_path / "release", tmp_path / "installed"
    source.mkdir(); target.mkdir()
    (source / "app.py").write_text("new app")
    (source / "new.py").write_text("new module")
    (target / "app.py").write_text("old app")
    (target / ".env").write_text("SECRET=keep-private")
    (target / "templates").mkdir()
    (target / "templates/licensed.pdf").write_bytes(b"licensed-test-placeholder")
    (target / ".venv/bin").mkdir(parents=True)
    (target / ".venv/bin/python").write_text("old interpreter")
    database = target / "data.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE customer (name TEXT)")
        conn.execute("INSERT INTO customer VALUES ('existing test record')")
    manifest = {"files":[
        {"path":"app.py", "before":installer.blob_sha(target / "app.py"), "after":installer.blob_sha(source / "app.py")},
        {"path":"new.py", "before":None, "after":installer.blob_sha(source / "new.py")},
    ]}
    args = argparse.Namespace(db_path=str(database), service="witforms", backup_root=str(tmp_path / "backups"), health_url="http://127.0.0.1:8097/healthz")
    calls = []
    def run(*parts, capture=False):
        calls.append(parts)
        if "--property=WorkingDirectory" in parts: return str(target)
        if "--property=ExecStart" in parts: return str(target / ".venv/bin/gunicorn")
        if "is-active" in parts: return "active"
        if "venv" in parts:
            Path(parts[-1]).mkdir()
        return ""
    monkeypatch.setattr(installer, "run", run)
    monkeypatch.setattr(installer.os, "geteuid", lambda:0)
    monkeypatch.setattr(installer.time, "sleep", lambda _:None)
    return source, target, manifest, args, calls


def test_plan_is_read_only_and_rejects_local_drift(installation):
    source, target, manifest, args, calls = installation
    assert installer.plan(source,target,manifest) == [Path("app.py"),Path("new.py")]
    assert not calls and not (target / "new.py").exists()
    (target / "app.py").write_text("locally customized")
    with pytest.raises(ValueError,match="Local changes"):
        installer.plan(source,target,manifest)


def test_plan_reports_all_conflicts_without_printing_contents(installation):
    source, target, manifest, args, calls = installation
    private_value = "private-local-configuration-value"
    (target / "app.py").write_text(private_value)
    (target / "new.py").write_text("existing customized module")
    with pytest.raises(ValueError) as failure:
        installer.plan(source, target, manifest)
    assert "app.py" in str(failure.value) and "new.py" in str(failure.value)
    assert private_value not in str(failure.value)
    assert not calls and (target / "app.py").read_text() == private_value


def test_release_preserves_customized_examples_docs_and_tests(tmp_path, monkeypatch):
    source = Path(__file__).parents[1]
    manifest = json.loads((source / "tools/hedge-release-manifest.json").read_text())
    target = tmp_path / "installed"
    # Build an already-updated app using the real release manifest.
    for item in manifest["files"]:
        destination = target / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / item["path"], destination)
    preserved = [".env", ".env.example", "README.md", "docs/HEDGE-SETUP.md", "tests/test_hedge.py"]
    for name in preserved:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("private local customization")
    original_hash = installer.blob_sha
    def guarded_hash(path):
        if path.is_relative_to(target):
            assert str(path.relative_to(target)) not in preserved
        return original_hash(path)
    monkeypatch.setattr(installer, "blob_sha", guarded_hash)
    assert installer.plan(source, target, manifest) == []
    assert all((target / name).read_text() == "private local customization" for name in preserved)


def test_plan_rejects_corruption_and_linked_targets(installation):
    source, target, manifest, args, calls = installation
    (target / "new.py").symlink_to(source / "new.py")
    with pytest.raises(ValueError,match="linked destination"):
        installer.plan(source,target,manifest)
    (target / "new.py").unlink()
    (source / "new.py").write_text("corrupted")
    with pytest.raises(ValueError,match="integrity"):
        installer.plan(source,target,manifest)


def test_install_keeps_credentials_templates_and_database(installation,monkeypatch):
    source,target,manifest,args,calls=installation
    monkeypatch.setattr(installer,"healthy",lambda *a:True)
    installer.apply_release(source,target,installer.plan(source,target,manifest),args)
    assert (target / "app.py").read_text()=="new app"
    assert (target / ".env").read_text()=="SECRET=keep-private"
    assert (target / "templates/licensed.pdf").read_bytes()==b"licensed-test-placeholder"
    assert (target / ".venv").is_symlink()
    backup=next(Path(args.backup_root).iterdir())
    with sqlite3.connect(backup / "database.sqlite3") as conn:
        assert conn.execute("SELECT name FROM customer").fetchone()[0]=="existing test record"
    assert (backup / "code/app.py").read_text()=="old app"


def test_unhealthy_deployment_restores_previous_code_and_virtualenv(installation,monkeypatch):
    source,target,manifest,args,calls=installation
    monkeypatch.setattr(installer,"healthy",lambda *a:False)
    with pytest.raises(RuntimeError,match="checks failed"):
        installer.apply_release(source,target,installer.plan(source,target,manifest),args)
    assert (target / "app.py").read_text()=="old app"
    assert not (target / "new.py").exists()
    assert not (target / ".venv").is_symlink()
    assert (target / ".venv/bin/python").read_text()=="old interpreter"
    assert calls[-1]==("systemctl","start","witforms")
