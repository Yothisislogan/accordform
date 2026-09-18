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


def test_database_copy_and_real_gunicorn_leave_snapshot_unchanged(tmp_path, diagnostic, capsys):
    snapshot = tmp_path / 'snapshot.sqlite3'
    with sqlite3.connect(snapshot) as connection:
        connection.execute('CREATE TABLE private_marker (value TEXT)')
        connection.execute('INSERT INTO private_marker VALUES (?)', ('never-print-this-customer-value',))
    before = snapshot.read_bytes()
    runtime = tmp_path / 'probe'
    runtime.mkdir(mode=0o700)
    diagnostic.copy_snapshot(snapshot, runtime / 'probe.sqlite3')
    assert diagnostic.check_gunicorn(ROOT, Path(sys.executable), runtime)
    assert snapshot.read_bytes() == before
    with sqlite3.connect(runtime / 'probe.sqlite3') as connection:
        assert connection.execute('SELECT value FROM private_marker').fetchone()[0] == 'never-print-this-customer-value'
        assert connection.execute('SELECT count(*) FROM wit_risk_intakes').fetchone()[0] == 0
    output = capsys.readouterr().out
    assert 'never-print-this-customer-value' not in output
    assert '"gunicorn_ok": true' in output


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
    assert 'private-exception-value' not in output
    assert '"gunicorn_ok": false' in output
