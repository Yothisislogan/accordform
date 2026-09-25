"""Signed inbound connections. Neither endpoint accepts browser authentication.

Hedge's Svix contract: https://docs.hedgespecialty.com/api/webhooks/receivesubmissionevents/
The WordPress bridge is a separate HMAC contract documented in docs/HEDGE-SETUP.md.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import date

from flask import jsonify, request
from db import get_db

STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split())
LINES = {"gl", "wc", "commercial_property", "commercial_auto", "professional_liability", "cyber", "umbrella", "other"}
CONSENT_VERSION = "wit-risk-v1"


def _text(body, key, maximum, required=False):
    value = body.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"Invalid {key}.")
    value = value.strip()
    if len(value) > maximum or (required and not value):
        raise ValueError(f"Please check {key}.")
    return value


def intake_payload(body):
    """Allowlist and validate; no consumer can set producer, insured ID or state."""
    if not isinstance(body, dict):
        raise ValueError("Expected an object.")
    request_id = str(uuid.UUID(_text(body, "request_id", 36, True)))
    if body.get("consent") is not True or body.get("consent_version") != CONSENT_VERSION:
        raise ValueError("Please confirm permission to review your request.")
    state = _text(body, "state", 2, True).upper()
    if state not in STATES:
        raise ValueError("Choose a US state or DC.")
    email = _text(body, "email", 254, True)
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("Please enter a valid email address.")
    phone = _text(body, "phone", 40, True)
    if not 10 <= len(re.sub(r"\D", "", phone)) <= 15:
        raise ValueError("Please enter a valid phone number.")
    zip_code = _text(body, "zip", 10, True)
    if not re.fullmatch(r"\d{5}(?:-\d{4})?", zip_code):
        raise ValueError("Please check your ZIP code.")
    lines = body.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= len(LINES) or any(not isinstance(v, str) or v not in LINES for v in lines):
        raise ValueError("Choose at least one coverage.")
    effective = _text(body, "effective_date", 10)
    if effective and (date.fromisoformat(effective).isoformat() != effective or date.fromisoformat(effective) < date.today()):
        raise ValueError("Choose today or a future effective date.")
    narrative = _text(body, "operations", 5000, True)
    if len(narrative) < 20:
        raise ValueError("Tell us a little more about the business operations.")
    for key, label in (("revenue", "Annual revenue"), ("employees", "Employees"),
                       ("current_insurance", "Current coverage / renewal"), ("losses", "Claims / prior losses")):
        value = _text(body, key, 1000)
        if value:
            narrative += f"\n{label}: {value}"
    if "other" in lines:
        narrative += "\nCoverage selection: agent guidance requested."
    applicant = {
        "insured_name": _text(body, "business_name", 256, True),
        "contact_first_name": _text(body, "first_name", 100, True),
        "contact_last_name": _text(body, "last_name", 100, True),
        "business_phone": phone, "business_email": email, "contact_email": email,
        "mailing_address": {"line1": _text(body, "address", 256, True),
                            "city": _text(body, "city", 100, True), "state": state, "zip": zip_code},
    }
    payload = {"applicant": applicant, "narrative": narrative,
               "lines_of_business": sorted(set(lines) - {"other"})}
    if effective:
        payload["effective_date"] = effective
    return request_id, payload


def _fresh_timestamp(value):
    return bool(re.fullmatch(r"\d{1,12}", value or "")) and abs(time.time() - int(value)) <= 300


def verify_bridge(raw, headers, secret):
    stamp = headers.get("X-WIT-Timestamp", "")
    signature = headers.get("X-WIT-Signature", "")
    if not secret or not _fresh_timestamp(stamp) or not re.fullmatch(r"[a-f0-9]{64}", signature):
        return False
    expected = hmac.new(secret.encode(), b"wit-risk-v1." + stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_hedge(raw, headers, secret):
    delivery = headers.get("svix-id", "")
    stamp = headers.get("svix-timestamp", "")
    if not delivery or len(delivery) > 256 or not _fresh_timestamp(stamp) or not secret.startswith("whsec_"):
        return False
    try:
        key = base64.b64decode(secret[6:], validate=True)
    except ValueError:
        return False
    if len(key) < 16:
        return False
    signed = delivery.encode() + b"." + stamp.encode() + b"." + raw
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    # Multiple v1 signatures are legal during secret rotation.
    return any(hmac.compare_digest(expected, part[3:]) for part in
               headers.get("svix-signature", "").split() if re.fullmatch(r"v1,[A-Za-z0-9+/=]+", part))


def register_routes(app):
    @app.post("/integrations/wit/intake")
    def website_intake():
        secret = app.config.get("WIT_INTAKE_SECRET", "")
        if len(secret) < 32:
            return jsonify(error="Intake is not configured."), 503
        request.max_content_length = 32 * 1024
        raw = request.get_data()
        if not verify_bridge(raw, request.headers, secret):
            return jsonify(error="Invalid signature."), 401
        try:
            body = json.loads(raw)
            request_id, payload = intake_payload(body)
        except (ValueError, TypeError, AttributeError):
            return jsonify(error="Please check the required business, contact and consent fields."), 422
        # Hash the original request; reusing a reference with changed content is a conflict.
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        conn = get_db()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT payload_hash FROM wit_risk_intakes WHERE request_id=?", (request_id,)).fetchone()
            if existing and existing[0] != digest:
                return jsonify(error="This reference already belongs to another request."), 409
            if not existing:
                confirmation = {"draft_payload": payload, "source": "weinsurethings.com",
                                "intake_reference": request_id, "consent_version": CONSENT_VERSION,
                                "consent": True}
                cur = conn.execute("""INSERT INTO hedge_submissions
                    (client_ref, idempotency_key, class_desc, state_code, lines, effective_date,
                     confirmation_json, payload_hash, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'website')""",
                    (payload["applicant"]["insured_name"], str(uuid.uuid4()), payload["narrative"][:200],
                     payload["applicant"]["mailing_address"]["state"], ",".join(payload["lines_of_business"]),
                     payload.get("effective_date", ""), json.dumps(confirmation), digest))
                conn.execute("INSERT INTO wit_risk_intakes (request_id, submission_id, payload_hash) VALUES (?, ?, ?)",
                             (request_id, cur.lastrowid, digest))
                conn.execute("INSERT INTO hedge_events (submission_id, kind, detail_json, actor) VALUES (?, 'website_intake', ?, 'website')",
                             (cur.lastrowid, json.dumps({"reference": request_id, "consent_version": CONSENT_VERSION})))
        # A receipt is not an access token: no public read endpoint exists.
        return jsonify(reference=request_id, status="received_for_review"), 200 if existing else 201

    @app.post("/integrations/hedge/events")
    def hedge_events():
        secret = app.config.get("HEDGE_WEBHOOK_SECRET", "")
        if not secret:
            return jsonify(error="Webhook is not configured."), 503
        request.max_content_length = 1024 * 1024
        raw = request.get_data()
        if not verify_hedge(raw, request.headers, secret):
            return jsonify(error="Invalid signature."), 401
        try:
            body = json.loads(raw)
            if body.get("type") == "ping":
                return jsonify(received=True)
            if body.get("type") != "submission.events" or not isinstance(body.get("events"), list):
                raise ValueError()
            sid = str(uuid.UUID(body["submission_id"]))
            for event in body["events"]:
                uuid.UUID(event["id"])
                if not isinstance(event.get("type"), str) or not 1 <= len(event["type"]) <= 100:
                    raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            return jsonify(error="Invalid event payload."), 400
        digest = hashlib.sha256(raw).hexdigest()
        conn = get_db()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT payload_hash FROM hedge_webhook_deliveries WHERE delivery_id=?",
                                    (request.headers["svix-id"],)).fetchone()
            if existing:
                if existing[0] != digest:
                    return jsonify(error="Delivery ID conflict."), 409
                return jsonify(received=True)
            row = conn.execute("SELECT id FROM hedge_submissions WHERE hedge_id=?", (sid,)).fetchone()
            for event in body["events"]:
                encoded = json.dumps(event)
                cur = conn.execute("INSERT OR IGNORE INTO hedge_webhook_events (hedge_id, event_id, event_type, event_json) VALUES (?, ?, ?, ?)",
                                   (sid, event["id"], event["type"], encoded))
                if cur.rowcount:
                    conn.execute("INSERT INTO hedge_events (submission_id, kind, detail_json, payload_hash, hedge_ref, actor) VALUES (?, ?, ?, ?, ?, 'hedge-webhook')",
                                 (row[0] if row else None, event["type"], encoded, digest, sid))
            conn.execute("INSERT INTO hedge_webhook_deliveries (delivery_id, payload_hash) VALUES (?, ?)",
                         (request.headers["svix-id"], digest))
        # Market events are not the overall submission status. Keep the poller
        # authoritative for that field. No outbound HTTP inside this callback.
        return jsonify(received=True)
