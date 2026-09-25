"""Use synthetic records to verify offline database/Gunicorn diagnosis."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest


ROOT = Path(__file__).parents[1]


@pytest.fixture()
def diagnostic(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'tools'))
    spec = importlib.util.spec_from_file_location('deployment_probe', ROOT / 'tools/check_hedge_deployment.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('customize_gemini', [False, True])
def test_database_copy_and_real_gunicorn_leave_snapshot_unchanged(tmp_path, diagnostic, capsys, customize_gemini):
    snapshot = tmp_path / 'snapshot.sqlite3'
    with sqlite3.connect(snapshot) as connection:
        connection.execute('CREATE TABLE private_marker (value TEXT)')
        connection.execute('INSERT INTO private_marker VALUES (?)', ('never-print-this-customer-value',))
    before = snapshot.read_bytes()
    runtime = tmp_path / 'probe'
    runtime.mkdir(mode=0o700)
    release_before = (ROOT / 'gemini_service.py').read_bytes()
    installed = tmp_path / 'installed'
    installed.mkdir()
    customization = release_before.replace(b'You are the proposal writer', b'Private proposal customization')
    (installed / 'gemini_service.py').write_bytes(customization)
    local_gemini = diagnostic.literal_only_gemini(ROOT, installed) if customize_gemini else None
    if customize_gemini:
        assert local_gemini == customization
    diagnostic.copy_snapshot(snapshot, runtime / 'probe.sqlite3')
    assert diagnostic.check_gunicorn(ROOT, Path(sys.executable), runtime, local_gemini=local_gemini)
    assert snapshot.read_bytes() == before
    assert (ROOT / 'gemini_service.py').read_bytes() == release_before
    assert (installed / 'gemini_service.py').read_bytes() == customization
    if customize_gemini:
        assert (runtime / 'local_gemini.py').read_bytes() == customization
        assert (runtime / 'local_gemini.py').stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(runtime / 'probe.sqlite3') as connection:
        assert connection.execute('SELECT value FROM private_marker').fetchone()[0] == 'never-print-this-customer-value'
        assert connection.execute('SELECT count(*) FROM wit_risk_intakes').fetchone()[0] == 0
    output = capsys.readouterr().out
    assert 'never-print-this-customer-value' not in output
    assert 'Private proposal customization' not in output
    assert '"gunicorn_ok": true' in output
    assert ('verified local Gemini copy imported' in output) == customize_gemini


def test_supporting_file_drift_is_reported_without_contents(tmp_path, diagnostic):
    source, installed = tmp_path / 'release', tmp_path / 'installed'
    source.mkdir(); installed.mkdir()
    (source / 'app.py').write_text('new app')
    (installed / 'app.py').write_text('older supported app')
    (source / 'auth.py').write_text('public source')
    (installed / 'auth.py').write_text('private local customization')
    (source / 'helper.py').write_text('new required helper')
    conflicts = diagnostic.supporting_files(source, installed, {'files': [{'path': 'app.py'}]})
    assert conflicts == ['auth.py', 'helper.py']


def test_failed_worker_does_not_print_sensitive_exception_message(tmp_path, diagnostic, capsys):
    source, runtime = tmp_path / 'release', tmp_path / 'runtime'
    source.mkdir(); runtime.mkdir(mode=0o700)
    (source / 'config.py').write_text("raise RuntimeError('private-exception-value')\n")
    assert not diagnostic.check_gunicorn(source, Path(sys.executable), runtime)
    output = capsys.readouterr().out
    assert 'WIT_PROBE_ERROR: RuntimeError' in output
    assert output.count('WIT_PROBE_ERROR:') == 1
    assert 'private-exception-value' not in output
    assert '"gunicorn_ok": false' in output


@pytest.mark.parametrize('change', [
    lambda original: original + b'\nimport socket\n',
    lambda original: original + b'\nraise RuntimeError("private")\n',
    lambda original: original.replace(b'import requests', b'import urllib'),
    lambda original: b'not valid Python!',
])
def test_local_gemini_rejects_code_changes_without_running_them(tmp_path, diagnostic, change):
    original = (ROOT / 'gemini_service.py').read_bytes()
    (tmp_path / 'gemini_service.py').write_bytes(change(original))
    assert diagnostic.literal_only_gemini(ROOT, tmp_path) is None


def test_local_gemini_rejects_missing_and_symlinked_files(tmp_path, diagnostic):
    assert diagnostic.literal_only_gemini(ROOT, tmp_path) is None
    (tmp_path / 'gemini_service.py').symlink_to(ROOT / 'gemini_service.py')
    assert diagnostic.literal_only_gemini(ROOT, tmp_path) is None


def test_literal_change_can_break_startup_without_leaking_its_value(tmp_path, diagnostic, capsys):
    original = (ROOT / 'gemini_service.py').read_bytes()
    changed = original.replace(
        b're.compile(r"^\\s*```[a-zA-Z0-9]*\\s*|\\s*```\\s*$")',
        b're.compile("(?private-literal-value)")')
    assert changed != original
    (tmp_path / 'gemini_service.py').write_bytes(changed)
    verified = diagnostic.literal_only_gemini(ROOT, tmp_path)
    assert verified == changed
    runtime = tmp_path / 'probe'
    runtime.mkdir(mode=0o700)
    assert not diagnostic.check_gunicorn(ROOT, Path(sys.executable), runtime, local_gemini=verified)
    output = capsys.readouterr().out
    assert 'WIT_PROBE_ERROR:' in output
    assert output.count('WIT_PROBE_ERROR:') == 1
    assert '"gunicorn_ok": false' in output
    assert 'private-literal-value' not in output
    assert (ROOT / 'gemini_service.py').read_bytes() == original
