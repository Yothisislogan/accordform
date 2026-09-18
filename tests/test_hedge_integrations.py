"""Real routes + SQLite, mocked external Hedge only. No live requests."""
import base64
import hashlib
import hmac
import json
import time
import uuid

import pytest

SECRET = "test-only-bridge-secret-32-characters-long"
WEBHOOK_KEY = b"test-only-webhook-key-32-characters"
SID = "00000000-0000-4000-8000-000000000001"
EVENT = "00000000-0000-4000-8000-000000000002"


def risk():
    return dict(request_id=str(uuid.uuid4()), business_name="Example Test LLC",
                first_name="Test", last_name="Shopper", email="shopper@example.com",
                phone="2025550199", address="123 Test Street", city="Test City",
                state="VA", zip="22201", operations="Residential interior painting with no exterior work.",
                lines=["gl"], consent=True, consent_version="wit-risk-v1")


def intake(client, data, *, raw=None, timestamp=None, secret=SECRET):
    raw = raw if raw is not None else json.dumps(data).encode()
    stamp = str(timestamp or int(time.time()))
    signature = hmac.new(secret.encode(), b"wit-risk-v1." + stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return client.post("/integrations/wit/intake", data=raw, content_type="application/json",
                       headers={"X-WIT-Timestamp": stamp, "X-WIT-Signature": signature})


def webhook(client, body, *, delivery="msg_1", timestamp=None, key=WEBHOOK_KEY, raw=None):
    raw = raw if raw is not None else json.dumps(body).encode()
    stamp = str(timestamp or int(time.time()))
    sig = base64.b64encode(hmac.new(key, delivery.encode() + b"." + stamp.encode() + b"." + raw, hashlib.sha256).digest()).decode()
    return client.post("/integrations/hedge/events", data=raw, content_type="application/json",
                       headers={"svix-id": delivery, "svix-timestamp": stamp, "svix-signature": "v1," + sig})


@pytest.fixture()
def connected(app):
    app.config.update(WIT_INTAKE_SECRET=SECRET, HEDGE_WEBHOOK_SECRET="whsec_" + base64.b64encode(WEBHOOK_KEY).decode())
    return app


def test_intake_receipt_is_durable_private_and_idempotent(connected, monkeypatch):
    import hedge_service
    from db import get_db
    monkeypatch.setattr(hedge_service, "request", lambda *a, **kw: pytest.fail("Intake must never call Hedge"))
    client = connected.test_client()
    body = risk()
    body.update(producer_email="attacker@example.com", insured_id=SID, local_state="finalized")
    first = intake(client, body)
    assert first.status_code == 201
    assert first.json == {"reference": body["request_id"], "status": "received_for_review"}
    assert intake(client, body).status_code == 200
    with connected.app_context():
        conn = get_db()
        rows = conn.execute("SELECT * FROM hedge_submissions").fetchall()
        assert len(rows) == 1 and rows[0]["local_state"] == "draft" and rows[0]["hedge_id"] is None
        confirmation = json.loads(rows[0]["confirmation_json"])
        assert confirmation["source"] == "weinsurethings.com"
        assert confirmation["consent"] is True
        assert "producer_email" not in confirmation["draft_payload"]
        assert "insured_id" not in confirmation["draft_payload"]
    assert client.get("/api/hedge/pipeline").status_code == 401
    changed = {**body, "business_name": "Different Test LLC"}
    assert intake(client, changed).status_code == 409


def test_intake_signature_staleness_and_configuration(app, connected):
    client = connected.test_client()
    body = risk()
    assert intake(client, body, secret="bad").status_code == 401
    assert intake(client, body, timestamp=int(time.time()) - 301).status_code == 401
    assert client.post("/integrations/wit/intake", json=body).status_code == 401
    assert intake(client, body, raw=b"x" * 32769).status_code == 413
    app.config["WIT_INTAKE_SECRET"] = ""
    assert intake(client, body).status_code == 503


@pytest.mark.parametrize("changes", [
    {"consent": False}, {"consent": "true"}, {"consent_version": "wrong"},
    {"business_name": ""}, {"state": "XX"}, {"email": "bad"}, {"phone": "555"},
    {"lines": ["personal_auto"]}, {"operations": "short"}, {"effective_date": "2026-02-30"},
    {"effective_date": "2020-01-01"}, {"address": []}, {"zip": "nope"},
])
def test_intake_rejects_invalid_inputs(connected, changes):
    assert intake(connected.test_client(), {**risk(), **changes}).status_code == 422


def test_webhook_verifies_raw_bytes_and_timestamp(connected):
    client = connected.test_client()
    assert webhook(client, {"type": "ping"}).status_code == 200
    assert webhook(client, {"type": "ping"}, key=b"bad").status_code == 401
    assert webhook(client, {"type": "ping"}, timestamp=int(time.time()) + 301).status_code == 401
    assert webhook(client, {}, raw=b"not-json").status_code == 400
    assert webhook(client, {}, raw=b"x" * (1024 * 1024 + 1)).status_code == 413
    assert webhook(client, {"type":"submission.events", "submission_id":SID, "events":[{}]}).status_code == 400


def test_webhook_dedupes_delivery_and_event_type_pairs(connected):
    from db import get_db
    client = connected.test_client()
    assert intake(client, risk()).status_code == 201
    with connected.app_context():
        conn = get_db()
        conn.execute("UPDATE hedge_submissions SET hedge_id=?, remote_state='submitted'", (SID,))
        conn.commit()
    event = {"id": EVENT, "type":"status_changed", "occurred_at":"2026-09-18T10:00:00Z",
             "market":{"name":"Test Market"}, "status":"reviewing", "status_label":"In review"}
    body = {"type":"submission.events", "submission_id":SID, "events":[event]}
    assert webhook(client, body).status_code == 200
    assert webhook(client, body).status_code == 200
    assert webhook(client, body, delivery="msg_2").status_code == 200
    body["events"][0]["type"] = "quote_received"
    assert webhook(client, body, delivery="msg_3").status_code == 200
    assert webhook(client, body, delivery="msg_1").status_code == 409
    with connected.app_context():
        conn = get_db()
        rows = conn.execute("SELECT * FROM hedge_events WHERE actor='hedge-webhook'").fetchall()
        assert len(rows) == 2
        assert {r["kind"] for r in rows} == {"status_changed", "quote_received"}
        assert all(r["submission_id"] == 1 for r in rows)
        assert conn.execute("SELECT remote_state FROM hedge_submissions").fetchone()[0] == "submitted"


def test_events_arriving_before_create_response_are_preserved(connected, monkeypatch):
    from db import get_db
    from hedge import api_client
    client = connected.test_client()
    intake(client, risk())
    body = {"type":"submission.events", "submission_id":SID,
            "events":[{"id":EVENT, "type":"quote_received"}]}
    assert webhook(client, body).status_code == 200
    cfg = connected.config["_CFG"]
    monkeypatch.setattr(cfg, "HEDGE_LIVE", True)
    monkeypatch.setattr(cfg, "HEDGE_CLIENT_ID", "bac-test")
    monkeypatch.setattr(cfg, "HEDGE_CLIENT_SECRET", "bas-test")
    sent = []
    monkeypatch.setattr(api_client.hedge_service, "request", lambda *a, **kw: sent.append(kw) or {"submission_id":SID})
    with connected.app_context():
        conn = get_db()
        api_client.send_create(conn, 1, actor="producer@weinsurethings.com", config=cfg)
        assert conn.execute("SELECT submission_id FROM hedge_events WHERE actor='hedge-webhook'").fetchone()[0] == 1
        assert sent[0]["json_body"]["producer_email"] == "producer@weinsurethings.com"
        assert json.loads(conn.execute("SELECT confirmation_json FROM hedge_submissions").fetchone()[0])["source"] == "weinsurethings.com"


def test_old_uncertain_create_is_not_retried(connected, monkeypatch):
    from db import get_db
    from hedge import api_client
    client = connected.test_client()
    intake(client, risk())
    cfg = connected.config["_CFG"]
    monkeypatch.setattr(cfg, "HEDGE_LIVE", True)
    monkeypatch.setattr(api_client.hedge_service, "request", lambda *a, **kw: pytest.fail("No unsafe retry"))
    with connected.app_context():
        conn = get_db()
        conn.execute("INSERT INTO hedge_create_attempts VALUES (1, ?)", (int(time.time()) - 24 * 3600,))
        conn.commit()
        with pytest.raises(api_client.PipelineError, match="Reconcile"):
            api_client.send_create(conn, 1, actor="producer@weinsurethings.com", config=cfg)


def test_legacy_finalize_cannot_bypass_review(connected):
    c = connected.test_client()
    with c.session_transaction() as session:
        session.update(email="producer@weinsurethings.com", user_id=1, csrf="test")
    response = c.post(f"/api/hedge/submissions/{SID}/finalize", headers={"X-CSRF-Token":"test"}, json={"approved":True})
    assert response.status_code == 409
