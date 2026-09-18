"""Hedge integration: OAuth device flow, ACORD→submission mapping, routes.

All HTTP is mocked — no network, and no live call is ever made to Hedge.
"""
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status=200, payload=None, content=b"", headers=None, text=""):
        self.status_code = status
        self._payload = payload
        self.content = content
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture()
def hedge(tmp_path, monkeypatch):
    """hedge_service bound to a temp DATA_DIR, with requests fully stubbed."""
    for mod in ("config", "hedge_service"):
        sys.modules.pop(mod, None)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HEDGE_ENV", "staging")
    import config
    import hedge_service as hs
    monkeypatch.setattr(config.Config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config.Config, "HEDGE_ENV", "staging")
    monkeypatch.setattr(config.Config, "HEDGE_LIVE", True)
    calls = []

    class FakeRequests:
        RequestException = Exception

        @staticmethod
        def get(url, **kw):
            calls.append(("GET", url, kw))
            return FakeRequests._route("GET", url, kw)

        @staticmethod
        def post(url, **kw):
            calls.append(("POST", url, kw))
            return FakeRequests._route("POST", url, kw)

        @staticmethod
        def request(method, url, **kw):
            calls.append((method, url, kw))
            return FakeRequests._route(method, url, kw)

        handlers = {}

        @staticmethod
        def _route(method, url, kw):
            for pred, resp in FakeRequests.handlers.items():
                if pred in url:
                    return resp(method, url, kw) if callable(resp) else resp
            return FakeResp(200, {})

    monkeypatch.setattr(hs, "requests", FakeRequests)
    hs._calls = calls
    hs._fake = FakeRequests
    return hs


META = {
    "authorization_endpoint": "https://staging-api.hedgespecialty.com/oauth/authorize",
    "token_endpoint": "https://staging-api.hedgespecialty.com/oauth/token",
    "device_authorization_endpoint": "https://staging-api.hedgespecialty.com/oauth/device",
    "registration_endpoint": "https://staging-api.hedgespecialty.com/oauth/register",
}


def _wire_login(hedge, poll_payloads):
    """Discovery + registration + device start, then a queue of poll results."""
    queue = list(poll_payloads)

    def token_ep(method, url, kw):
        return queue.pop(0)

    hedge._fake.handlers = {
        ".well-known/oauth-authorization-server": FakeResp(200, META),
        "/oauth/register": FakeResp(200, {"client_id": "client-abc"}),
        "/oauth/device": FakeResp(200, {
            "device_code": "dev-123", "user_code": "WXYZ-1234",
            "verification_uri": "https://staging-brokers.hedgespecialty.com/device",
            "verification_uri_complete": "https://staging-brokers.hedgespecialty.com/device?c=WXYZ-1234",
            "interval": 1, "expires_in": 900,
        }),
        "/oauth/token": token_ep,
    }


# --------------------------------------------------------------------------
# Environment + endpoints
# --------------------------------------------------------------------------
def test_environments_match_the_cli(hedge):
    import config
    assert hedge.env(config.Config)["api_base"] == "https://staging-api.hedgespecialty.com/api/v1"
    monkey = config.Config
    monkey.HEDGE_ENV = "prod"
    assert hedge.env(monkey)["api_base"] == "https://api.hedgespecialty.com/api/v1"
    monkey.HEDGE_ENV = "staging"


def test_bad_env_rejected(hedge):
    import config
    config.Config.HEDGE_ENV = "nope"
    with pytest.raises(hedge.HedgeError):
        hedge.env(config.Config)
    config.Config.HEDGE_ENV = "staging"


# --------------------------------------------------------------------------
# OAuth device flow
# --------------------------------------------------------------------------
def test_device_login_start_returns_user_code_not_device_code(hedge):
    _wire_login(hedge, [])
    out = hedge.start_device_login()
    assert out["user_code"] == "WXYZ-1234"
    # The device_code is a credential — it must stay server-side.
    assert "device_code" not in out
    assert "verification_uri_complete" in out


def test_poll_pending_then_complete_persists_token(hedge, tmp_path):
    _wire_login(hedge, [
        FakeResp(400, {"error": "authorization_pending"}),
        FakeResp(400, {"error": "slow_down"}),
        FakeResp(200, {"access_token": "at-1", "refresh_token": "rt-1",
                       "expires_in": 3600, "scope": "broker_mcp broker_submit"}),
    ])
    hedge.start_device_login()
    assert hedge.poll_device_login()["status"] == "pending"
    assert hedge.poll_device_login()["status"] == "slow_down"
    assert hedge.poll_device_login()["status"] == "complete"

    tok = hedge.load_token()
    assert tok["access_token"] == "at-1" and tok["refresh_token"] == "rt-1"
    assert hedge.is_signed_in()
    # Pending state is cleaned up once the token lands.
    assert not (tmp_path / "hedge.staging.pending.json").exists()


def test_token_file_is_0600(hedge, tmp_path):
    _wire_login(hedge, [FakeResp(200, {"access_token": "a", "refresh_token": "r",
                                       "expires_in": 3600})])
    hedge.start_device_login()
    hedge.poll_device_login()
    mode = stat.S_IMODE(os.stat(tmp_path / "hedge.staging.token.json").st_mode)
    assert mode == 0o600, f"refresh token file is {oct(mode)}, expected 0600"


def test_access_denied_surfaces_readably(hedge):
    _wire_login(hedge, [FakeResp(400, {"error": "access_denied"})])
    hedge.start_device_login()
    with pytest.raises(hedge.HedgeError, match="denied"):
        hedge.poll_device_login()


def test_client_id_is_cached_not_reregistered(hedge):
    _wire_login(hedge, [])
    hedge.start_device_login()
    hedge.start_device_login()
    registrations = [c for c in hedge._calls if "/oauth/register" in c[1]]
    assert len(registrations) == 1


def test_expired_token_refreshes_and_persists(hedge):
    hedge.save_token({"access_token": "old", "refresh_token": "rt-1",
                      "expires_at": int(time.time()) - 10,
                      "token_endpoint": "https://staging-api.hedgespecialty.com/oauth/token",
                      "client_id": "client-abc"})
    hedge._fake.handlers = {"/oauth/token": FakeResp(200, {
        "access_token": "new-token", "expires_in": 3600})}
    assert hedge.bearer() == "new-token"
    stored = hedge.load_token()
    assert stored["access_token"] == "new-token"
    # Server omitted refresh_token on refresh — the old one must be kept.
    assert stored["refresh_token"] == "rt-1"


def test_dead_refresh_clears_session(hedge):
    hedge.save_token({"access_token": "old", "refresh_token": "bad",
                      "expires_at": int(time.time()) - 10,
                      "token_endpoint": "https://staging-api.hedgespecialty.com/oauth/token",
                      "client_id": "c"})
    hedge._fake.handlers = {"/oauth/token": FakeResp(400, {"error": "invalid_grant"})}
    with pytest.raises(hedge.HedgeAuthRequired):
        hedge.bearer()
    assert hedge.load_token() is None


def test_not_signed_in_raises_auth_required(hedge):
    with pytest.raises(hedge.HedgeAuthRequired):
        hedge.whoami()


# --------------------------------------------------------------------------
# API surface + error rendering
# --------------------------------------------------------------------------
def _signed_in(hedge):
    hedge.save_token({"access_token": "at", "refresh_token": "rt",
                      "expires_at": int(time.time()) + 3600,
                      "token_endpoint": "t", "client_id": "c"})


def test_upload_uses_multipart_file_field(hedge):
    _signed_in(hedge)
    hedge._fake.handlers = {"/documents": FakeResp(200, {"document_id": "d1"})}
    hedge.upload_document("sub-1", b"%PDF-1.4", "ACORD_125.pdf", label="ACORD 125")
    method, url, kw = hedge._calls[-1]
    assert method == "POST"
    assert url.endswith("/api/v1/broker/submissions/sub-1/documents")
    assert "file" in kw["files"]
    fname, payload, ctype = kw["files"]["file"]
    assert fname == "ACORD_125.pdf" and ctype == "application/pdf"
    assert kw["data"] == {"name": "ACORD 125"}
    assert kw["headers"]["Authorization"] == "Bearer at"


@pytest.mark.parametrize("fn,expected", [
    ("whoami", "/broker/me"),
    ("list_policies", "/broker/policies"),
])
def test_endpoint_paths(hedge, fn, expected):
    _signed_in(hedge)
    getattr(hedge, fn)()
    assert hedge._calls[-1][1].endswith(expected)


def test_workflow_endpoint_paths(hedge):
    _signed_in(hedge)
    hedge.requirements("s1")
    assert hedge._calls[-1][1].endswith("/broker/submissions/s1/requirements")
    hedge.finalize("s1")
    assert hedge._calls[-1][1].endswith("/broker/submissions/s1/finalize")
    hedge.quote_sessions("s1")
    assert hedge._calls[-1][1].endswith("/broker/submissions/s1/api-quotes/sessions")
    hedge.close_quote("s1", "q9")
    assert hedge._calls[-1][1].endswith("/broker/submissions/s1/api-quotes/sessions/q9/close")


def test_fastapi_validation_detail_is_rendered(hedge):
    _signed_in(hedge)
    hedge._fake.handlers = {"/broker/submissions": FakeResp(422, {"detail": [
        {"loc": ["body", "applicant", "mailing_address"], "msg": "field required"}]})}
    with pytest.raises(hedge.HedgeError) as e:
        hedge.create_submission({})
    assert "mailing_address" in str(e.value) and "field required" in str(e.value)
    assert e.value.status == 422


def test_401_becomes_auth_required(hedge):
    _signed_in(hedge)
    hedge._fake.handlers = {"/broker/me": FakeResp(401, {"detail": "expired"})}
    with pytest.raises(hedge.HedgeAuthRequired):
        hedge.whoami()


# --------------------------------------------------------------------------
# ACORD -> Hedge submission mapping
# --------------------------------------------------------------------------
ACORD_125 = {
    "namedinsured_fullname_a0": "Acme Contracting LLC",
    "namedinsured_mailingaddress_lineone_a0": "400 Granby St",
    "namedinsured_mailingaddress_cityname_a0": "Norfolk",
    "namedinsured_mailingaddress_stateorprovincecode_a0": "va",
    "namedinsured_mailingaddress_postalcode_a0": "23510",
    "namedinsured_taxidentifier_a0": "54-1234567",
    "namedinsured_naicscode_a0": "NAICS 236118",
    "commercialpolicy_operationsdescription_b0": "Residential remodeling",
    "policy_effectivedate_a0": "01/15/2026",
}


def test_mapping_builds_expected_body():
    from hedge_mapping import build_submission_body

    body = build_submission_body(ACORD_125)
    app = body["applicant"]
    assert app["insured_name"] == "Acme Contracting LLC"
    assert app["mailing_address"] == {"line1": "400 Granby St", "city": "Norfolk",
                                      "state": "VA", "zip": "23510"}
    assert app["fein_or_ssn"] == "54-1234567"
    assert app["naics"] == "236118"                 # digits transform
    assert body["narrative"] == "Residential remodeling"
    assert body["effective_date"] == "2026-01-15"   # MM/DD/YYYY -> ISO
    assert "primary_state" not in body              # state lives on the address


def test_mapping_works_for_acord_25_key_names():
    from hedge_mapping import build_submission_body

    body = build_submission_body({
        "insured_name": "Acme LLC", "insured_addr1": "2 Oak Ave",
        "insured_city": "Norfolk", "insured_state": "VA", "insured_zip": "23503",
    })
    assert body["applicant"]["insured_name"] == "Acme LLC"
    assert body["applicant"]["mailing_address"]["line1"] == "2 Oak Ave"


def test_incomplete_address_is_dropped_not_sent():
    from hedge_mapping import address_status, build_submission_body

    partial = dict(ACORD_125)
    del partial["namedinsured_mailingaddress_postalcode_a0"]   # no zip
    body = build_submission_body(partial)
    assert "mailing_address" not in body["applicant"]   # would be a 422
    assert body["primary_state"] == "VA"                # state still usable
    assert address_status(body) == "dropped"


def test_overrides_win_and_can_complete_the_address():
    from hedge_mapping import address_status, build_submission_body

    partial = dict(ACORD_125)
    del partial["namedinsured_mailingaddress_postalcode_a0"]
    body = build_submission_body(
        partial, overrides={"applicant.mailing_address.zip": "23510",
                            "applicant.insured_name": "Corrected Name LLC"})
    assert body["applicant"]["insured_name"] == "Corrected Name LLC"
    assert address_status(body) == "complete"
    assert "primary_state" not in body


def test_missing_required_reports_what_hedge_needs():
    from hedge_mapping import build_submission_body, missing_required

    assert missing_required(build_submission_body(ACORD_125)) == []
    bare = build_submission_body({"insured_name": "Acme"})
    assert "narrative" in missing_required(bare)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def _client(app, admin=True):
    import db
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
        s["email"] = "logan@weinsurethings.com"
        s["role"] = "admin" if admin else "user"
        s["csrf"] = "t"
    with app.app_context():
        d = db.get_db()
        d.execute("INSERT OR IGNORE INTO users (id,email) VALUES (1,'logan@weinsurethings.com')")
        d.commit()
    return c, {"X-CSRF-Token": "t"}


def test_routes_require_auth(app):
    c = app.test_client()
    assert c.get("/api/hedge/status").status_code == 401


def test_login_is_admin_only(app):
    c, h = _client(app, admin=False)
    assert c.post("/api/hedge/login/start", headers=h).status_code == 403


def test_status_reports_signed_out_without_token(app):
    c, h = _client(app)
    body = c.get("/api/hedge/status").get_json()
    assert body["signed_in"] is False
    assert body["env"] in ("staging", "prod")


def test_preview_body_route_shows_payload_before_sending(app):
    c, h = _client(app)
    r = c.post("/api/hedge/preview-body", json={"answers": ACORD_125}, headers=h)
    assert r.status_code == 200
    data = r.get_json()
    assert data["body"]["applicant"]["insured_name"] == "Acme Contracting LLC"
    assert data["missing"] == []
    assert data["address_status"] == "complete"


def test_create_submission_blocks_on_missing_required(app):
    c, h = _client(app)
    r = c.post("/api/hedge/submissions", json={"answers": {"insured_name": "Acme"}},
               headers=h)
    assert r.status_code == 422
    assert "narrative" in r.get_json()["missing"]


def test_upload_requires_a_source(app):
    c, h = _client(app)
    r = c.post("/api/hedge/submissions/s1/documents", json={}, headers=h)
    assert r.status_code == 400
    assert "form_id" in r.get_json()["error"]


def test_no_token_ever_reaches_the_client(app):
    """The access/refresh tokens must never appear in an API response."""
    c, h = _client(app)
    payload = c.get("/api/hedge/status").get_data(as_text=True)
    for leak in ("access_token", "refresh_token", "device_code"):
        assert leak not in payload


# --------------------------------------------------------------------------
# Appetite pre-flight
# --------------------------------------------------------------------------
def test_appetite_params_derive_from_answers():
    from hedge_mapping import appetite_params

    p = appetite_params(ACORD_125)
    assert p["q"] == "Residential remodeling"
    assert p["state"] == "VA"          # from the mailing address
    assert "lob" not in p

    # State also comes from primary_state when the address is incomplete.
    partial = dict(ACORD_125)
    del partial["namedinsured_mailingaddress_postalcode_a0"]
    assert appetite_params(partial)["state"] == "VA"

    # No narrative -> no usable query.
    assert appetite_params({"insured_name": "Acme"}) == {}

    # Overrides can supply the narrative; long ones are capped at 200 chars.
    p = appetite_params({}, overrides={"narrative": "x" * 500,
                                       "applicant.insured_name": "A"})
    assert len(p["q"]) == 200


def test_appetite_endpoint_passes_query_params(hedge):
    _signed_in(hedge)
    hedge._fake.handlers = {"/broker/appetite": FakeResp(200, {"results": []})}
    hedge.appetite({"q": "roofing contractor", "state": "VA"})
    method, url, kw = hedge._calls[-1]
    assert url.endswith("/broker/appetite")
    assert kw["params"] == {"q": "roofing contractor", "state": "VA"}


def test_preflight_route_requires_narrative(app):
    c, h = _client(app)
    r = c.post("/api/hedge/appetite/preflight",
               json={"answers": {"insured_name": "Acme"}}, headers=h)
    assert r.status_code == 422
    assert "description of operations" in r.get_json()["error"]


def test_preflight_route_signed_out_is_401_not_500(app):
    c, h = _client(app)
    r = c.post("/api/hedge/appetite/preflight",
               json={"answers": ACORD_125}, headers=h)
    assert r.status_code == 401
    assert r.get_json()["signed_in"] is False


def test_acord_125_schema_carries_the_appetite_flag():
    """The pre-flight button is schema-driven (hard rule #7), not an if-branch."""
    import json
    meta = json.loads((ROOT / "schemas" / "acord_125.json").read_text())["_meta"]
    assert meta.get("hedge_appetite") is True


# --------------------------------------------------------------------------
# Machine credentials use OAuth, never X-Api-Key.
# --------------------------------------------------------------------------
def _machine_mode(hedge, monkeypatch, scope="broker_mcp broker_submit"):
    import config
    monkeypatch.setattr(config.Config, "HEDGE_CLIENT_ID", "bac-test")
    monkeypatch.setattr(config.Config, "HEDGE_CLIENT_SECRET", "bas-test-secret")
    hedge._fake.handlers = {
        ".well-known/oauth-authorization-server": FakeResp(200, META),
        "/oauth/token": FakeResp(200, {"access_token": "machine-token", "expires_in": 3600, "scope": scope}),
        "/broker": FakeResp(200, {}, content=b"{}"),
    }


def test_machine_bearer_is_cached_across_all_transports(hedge, monkeypatch):
    _machine_mode(hedge, monkeypatch)
    assert hedge.auth_mode() == "client_credentials"
    hedge.whoami()
    hedge.upload_document("s1", b"%PDF-1.4", "a.pdf")
    hedge.download("/broker/finalized-documents/d1/pdf")
    token_calls = [kw for _, url, kw in hedge._calls if url.endswith("/oauth/token")]
    assert len(token_calls) == 1
    assert token_calls[0]["data"]["grant_type"] == "client_credentials"
    assert token_calls[0]["data"]["client_secret"] == "bas-test-secret"
    for _, url, kw in hedge._calls:
        if "/broker" in url:
            assert kw["headers"]["Authorization"] == "Bearer machine-token"
            assert "bas-test-secret" not in repr(kw)
            assert "X-Api-Key" not in kw["headers"]
    assert hedge._calls[0][1].endswith("/api/v1/oauth/.well-known/oauth-authorization-server")


def test_machine_token_renews_before_expiry(hedge, monkeypatch):
    _machine_mode(hedge, monkeypatch)
    hedge.whoami()
    for value in hedge._machine_cache.values():
        value["expires_at"] = 0
    hedge.whoami()
    assert sum(url.endswith("/oauth/token") for _, url, _ in hedge._calls) == 2


def test_machine_token_scope_cap_blocks_writes(hedge, monkeypatch):
    _machine_mode(hedge, monkeypatch, scope="broker_mcp")
    with pytest.raises(hedge.HedgeError, match="broker_submit"):
        hedge.create_submission({})
    assert not any("/broker/submissions" in url for _, url, _ in hedge._calls)


def test_obsolete_key_is_never_sent(hedge, monkeypatch):
    import config
    monkeypatch.setattr(config.Config, "HEDGE_API_KEY", "legacy-secret")
    with pytest.raises(hedge.HedgeAuthRequired, match="Static HEDGE_API_KEY"):
        hedge.whoami()
    assert not hedge._calls


def test_device_login_short_circuits_in_machine_mode(hedge, monkeypatch):
    _machine_mode(hedge, monkeypatch)
    with pytest.raises(hedge.HedgeError, match="no sign-in is needed"):
        hedge.start_device_login()
    assert hedge._calls == []


def test_transport_live_gate_blocks_legacy_writes(hedge, monkeypatch):
    import config
    monkeypatch.setattr(config.Config, "HEDGE_LIVE", False)
    with pytest.raises(hedge.HedgeError, match="live mode is off"):
        hedge.create_submission({})
    with pytest.raises(hedge.HedgeError, match="live mode is off"):
        hedge.upload_document("s1", b"%PDF-1.4", "a.pdf")
    assert not hedge._calls


def test_discovery_does_not_send_secret_to_other_origin(hedge, monkeypatch):
    _machine_mode(hedge, monkeypatch)
    hedge._fake.handlers[".well-known/oauth-authorization-server"] = FakeResp(200, {"token_endpoint": "https://attacker.example/token"})
    with pytest.raises(hedge.HedgeError, match="untrusted endpoint"):
        hedge.whoami()
    assert len(hedge._calls) == 1


def test_create_submission_route_attributes_producer_in_key_mode(app, monkeypatch):
    import config
    import hedge_service
    monkeypatch.setattr(config.Config, "HEDGE_CLIENT_ID", "bac-test")
    monkeypatch.setattr(config.Config, "HEDGE_CLIENT_SECRET", "bas-test-secret")
    sent = {}

    def fake_create(body, cfg):
        sent.update(body)
        return {"submission_id": "sub-1"}
    monkeypatch.setattr(hedge_service, "create_submission", fake_create)

    c, h = _client(app)
    r = c.post("/api/hedge/submissions", json={"answers": ACORD_125}, headers=h)
    assert r.status_code == 200
    # The signed-in WIT user is attributed as the producing broker.
    assert sent["producer_email"] == "logan@weinsurethings.com"

    # An explicit producer_email in the overrides wins.
    sent.clear()
    r = c.post("/api/hedge/submissions",
               json={"answers": ACORD_125,
                     "overrides": {"producer_email": "other@weinsurethings.com"}},
               headers=h)
    assert sent["producer_email"] == "other@weinsurethings.com"


def test_no_producer_injection_in_oauth_mode(app, monkeypatch):
    import hedge_service
    sent = {}

    def fake_create(body, cfg):
        sent.update(body)
        return {"submission_id": "sub-1"}
    monkeypatch.setattr(hedge_service, "create_submission", fake_create)

    c, h = _client(app)
    c.post("/api/hedge/submissions", json={"answers": ACORD_125}, headers=h)
    assert "producer_email" not in sent


def test_status_reports_auth_mode(app, monkeypatch):
    import config
    import hedge_service
    monkeypatch.setattr(config.Config, "HEDGE_CLIENT_ID", "bac-test")
    monkeypatch.setattr(config.Config, "HEDGE_CLIENT_SECRET", "bas-test-secret")
    monkeypatch.setattr(hedge_service, "whoami", lambda cfg: {"name": "WIT"})
    c, h = _client(app)
    body = c.get("/api/hedge/status").get_json()
    assert body["auth_mode"] == "client_credentials"
    assert body["signed_in"] is True
    # The key itself never appears in any response.
    assert "bas-test-secret" not in json.dumps(body)
