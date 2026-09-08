"""Hedge (Taven Tech broker platform) client — submit-to-quote workflow.

Talks directly to the Hedge broker API from Python. The endpoint surface and
OAuth mechanics below were derived from the MIT-licensed `taventech/hedge-cli`
source (https://github.com/taventech/hedge-cli), not guessed:

    api base   https://api.hedgespecialty.com/api/v1        (staging-api… for staging)
    discovery  https://api.hedgespecialty.com/.well-known/oauth-authorization-server
    auth       OAuth 2.1 — RFC 7591 dynamic client registration + RFC 8628
               device authorization grant, refresh_token for renewal
    scopes     broker_mcp broker_submit

Endpoints used:
    GET    /broker/me
    GET    /broker/appetite
    GET    /broker/submissions                 POST /broker/submissions
    GET    /broker/submissions/{id}
    POST   /broker/submissions/{id}/documents  (multipart, field "file")
    GET    /broker/submissions/{id}/requirements
    POST   /broker/submissions/{id}/finalize
    GET    /broker/submissions/{id}/api-quotes/sessions
    POST   /broker/submissions/{id}/api-quotes/sessions/{sid}/answers
    POST   /broker/submissions/{id}/api-quotes/sessions/{sid}/close
    GET    /broker/submissions/{id}/finalized-documents
    GET    /broker/finalized-documents/{docId}/pdf
    GET    /broker/policies  /broker/policies/{id}  /broker/policies/{id}/document/{kind}

The refresh token is the sensitive artifact: it is stored in DATA_DIR at 0600
and never leaves the server.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from config import Config

CLIENT_NAME = "WIT Forms"
# Device flow has no redirect, but RFC 7591 registration wants the field.
REDIRECT_URIS = ["http://127.0.0.1/callback"]

ENVIRONMENTS = {
    "prod": {
        "issuer": "https://api.hedgespecialty.com",
        "api_base": "https://api.hedgespecialty.com/api/v1",
        "portal": "https://brokers.hedgespecialty.com",
    },
    "staging": {
        "issuer": "https://staging-api.hedgespecialty.com",
        "api_base": "https://staging-api.hedgespecialty.com/api/v1",
        "portal": "https://staging-brokers.hedgespecialty.com",
    },
}


class HedgeError(RuntimeError):
    """API/auth failure carrying the HTTP status for the route layer."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class HedgeAuthRequired(HedgeError):
    """Not signed in, or the session expired and can't be refreshed."""

    def __init__(self, message: str = "Not signed in to Hedge."):
        super().__init__(message, status=401)


# --------------------------------------------------------------------------
# Environment + token storage
# --------------------------------------------------------------------------
def env(config: type[Config] = Config) -> dict:
    name = (config.HEDGE_ENV or "staging").lower()
    if name not in ENVIRONMENTS:
        raise HedgeError(f"HEDGE_ENV must be 'prod' or 'staging' (got {name!r})")
    return ENVIRONMENTS[name]


def _state_path(config: type[Config], kind: str) -> Path:
    name = (config.HEDGE_ENV or "staging").lower()
    return Path(config.DATA_DIR) / f"hedge.{name}.{kind}.json"


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(path, 0o600)     # refresh token — owner only
    except OSError:
        pass


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def load_token(config: type[Config] = Config) -> dict | None:
    return _read_json(_state_path(config, "token"))


def save_token(tok: dict, config: type[Config] = Config) -> None:
    _write_private(_state_path(config, "token"), tok)


def clear_token(config: type[Config] = Config) -> None:
    for kind in ("token", "pending"):
        p = _state_path(config, kind)
        if p.exists():
            p.unlink()


# --------------------------------------------------------------------------
# OAuth 2.1 (RFC 8414 discovery, RFC 7591 registration, RFC 8628 device grant)
# --------------------------------------------------------------------------
_FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _post_form(url: str, fields: dict, timeout: int) -> tuple[int, dict]:
    try:
        r = requests.post(url, data=fields, headers=_FORM, timeout=timeout)
    except requests.RequestException as e:
        raise HedgeError(f"Could not reach Hedge: {e}") from e
    try:
        return r.status_code, (r.json() or {})
    except ValueError:
        return r.status_code, {}


def discover(config: type[Config] = Config) -> dict:
    """RFC 8414 authorization-server metadata."""
    url = env(config)["issuer"] + "/.well-known/oauth-authorization-server"
    try:
        r = requests.get(url, timeout=config.HEDGE_TIMEOUT)
    except requests.RequestException as e:
        raise HedgeError(f"Could not reach Hedge auth server: {e}") from e
    if r.status_code != 200:
        raise HedgeError(f"Could not load Hedge auth metadata ({r.status_code})", r.status_code)
    return r.json()


def register_client(meta: dict, config: type[Config] = Config) -> str:
    """RFC 7591 dynamic registration → public client_id (cached on disk)."""
    cached = _read_json(_state_path(config, "client")) or {}
    if cached.get("client_id"):
        return cached["client_id"]
    endpoint = meta.get("registration_endpoint")
    if not endpoint:
        raise HedgeError("Hedge auth server does not support dynamic client registration")
    try:
        r = requests.post(endpoint, json={"client_name": CLIENT_NAME,
                                          "redirect_uris": REDIRECT_URIS},
                          timeout=config.HEDGE_TIMEOUT)
    except requests.RequestException as e:
        raise HedgeError(f"Client registration failed: {e}") from e
    if r.status_code >= 400:
        raise HedgeError(f"Client registration failed ({r.status_code})", r.status_code)
    client_id = (r.json() or {}).get("client_id")
    if not client_id:
        raise HedgeError("Registration returned no client_id")
    _write_private(_state_path(config, "client"), {"client_id": client_id})
    return client_id


def start_device_login(config: type[Config] = Config) -> dict:
    """Begin the device grant. Returns the code + URL to show the operator.

    The device_code is kept server-side (a file, so it survives across gunicorn
    workers); the browser only ever sees the user-facing code and URL.
    """
    if auth_mode(config) == "api_key":
        raise HedgeError("An API key is configured — no sign-in is needed.")
    meta = discover(config)
    client_id = register_client(meta, config)
    endpoint = meta.get("device_authorization_endpoint")
    if not endpoint:
        raise HedgeError("Hedge auth server does not support device login")

    status, body = _post_form(
        endpoint, {"client_id": client_id, "scope": config.HEDGE_SCOPES},
        config.HEDGE_TIMEOUT)
    if status >= 400 or not body.get("device_code"):
        raise HedgeError(f"Device authorization failed ({status})", status)

    _write_private(_state_path(config, "pending"), {
        "device_code": body["device_code"],
        "client_id": client_id,
        "token_endpoint": meta["token_endpoint"],
        "interval": body.get("interval", 5),
        "expires_at": int(time.time()) + int(body.get("expires_in", 900)),
    })
    return {
        "user_code": body.get("user_code"),
        "verification_uri": body.get("verification_uri"),
        "verification_uri_complete": body.get("verification_uri_complete"),
        "interval": body.get("interval", 5),
        "expires_in": body.get("expires_in", 900),
    }


def poll_device_login(config: type[Config] = Config) -> dict:
    """Poll once. Returns {status: pending|slow_down|complete}.

    The caller (the browser) drives the polling loop so no request blocks a
    worker for the full 15-minute window.
    """
    pending = _read_json(_state_path(config, "pending"))
    if not pending:
        raise HedgeError("No sign-in is in progress. Start again.")
    if time.time() > pending.get("expires_at", 0):
        clear_token(config)
        raise HedgeError("Sign-in timed out. Start again.")

    status, body = _post_form(pending["token_endpoint"], {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": pending["device_code"],
        "client_id": pending["client_id"],
    }, config.HEDGE_TIMEOUT)

    if status < 400 and body.get("access_token"):
        _persist(body, pending["client_id"], pending["token_endpoint"], config)
        _state_path(config, "pending").unlink(missing_ok=True)
        return {"status": "complete"}

    err = body.get("error")
    if err == "authorization_pending":
        return {"status": "pending"}
    if err == "slow_down":
        pending["interval"] = pending.get("interval", 5) + 5
        _write_private(_state_path(config, "pending"), pending)
        return {"status": "slow_down", "interval": pending["interval"]}
    _state_path(config, "pending").unlink(missing_ok=True)
    raise HedgeError("Access was denied." if err == "access_denied"
                     else f"Sign-in failed: {err or status}", status)


def _persist(tokens: dict, client_id: str, token_endpoint: str,
             config: type[Config]) -> dict:
    stored = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": int(time.time()) + int(tokens.get("expires_in", 3600)),
        "scope": tokens.get("scope"),
        "token_endpoint": token_endpoint,
        "client_id": client_id,
    }
    save_token(stored, config)
    return stored


def bearer(config: type[Config] = Config) -> str:
    """A valid access token, refreshing (and persisting) within 60s of expiry."""
    tok = load_token(config)
    if not tok:
        raise HedgeAuthRequired("Not signed in to Hedge. Sign in first.")
    if tok.get("expires_at", 0) - 60 > int(time.time()):
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise HedgeAuthRequired("Hedge session expired. Sign in again.")

    status, body = _post_form(tok["token_endpoint"], {
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": tok["client_id"],
    }, config.HEDGE_TIMEOUT)
    if status >= 400 or not body.get("access_token"):
        clear_token(config)
        raise HedgeAuthRequired("Hedge session expired. Sign in again.")
    # Some servers omit refresh_token on refresh — keep the existing one.
    body.setdefault("refresh_token", tok["refresh_token"])
    return _persist(body, tok["client_id"], tok["token_endpoint"], config)["access_token"]


def auth_mode(config: type[Config] = Config) -> str:
    """'api_key' when a static brokerage credential is configured, else 'oauth'.

    The key wins when both exist: it is the deliberate, server-configured
    credential, while a stray token file may be stale.
    """
    return "api_key" if (getattr(config, "HEDGE_API_KEY", "") or "").strip() else "oauth"


def _auth_headers(config: type[Config] = Config) -> dict:
    """The credential header for a call — X-Api-Key or a (refreshed) Bearer."""
    if auth_mode(config) == "api_key":
        return {config.HEDGE_API_KEY_HEADER: config.HEDGE_API_KEY.strip()}
    return {"Authorization": f"Bearer {bearer(config)}"}


def is_signed_in(config: type[Config] = Config) -> bool:
    if auth_mode(config) == "api_key":
        return True
    tok = load_token(config)
    return bool(tok and (tok.get("refresh_token") or
                         tok.get("expires_at", 0) > int(time.time())))


# --------------------------------------------------------------------------
# HTTP core
# --------------------------------------------------------------------------
def _render_detail(detail, fallback: str) -> str:
    """FastAPI `detail` may be a string, a list of {loc,msg}, or an object."""
    if detail is None:
        return fallback
    if isinstance(detail, str):
        return detail.strip() or fallback
    if isinstance(detail, list):
        out = []
        for entry in detail:
            if isinstance(entry, dict) and "msg" in entry:
                loc = ".".join(str(x) for x in entry.get("loc", []))
                out.append(f"{loc}: {entry['msg']}" if loc else str(entry["msg"]))
            else:
                out.append(json.dumps(entry))
        return "\n".join(out) or fallback
    if isinstance(detail, dict):
        return json.dumps(detail)
    return str(detail)


def _raise_for_status(resp, fallback: str) -> None:
    if resp.status_code < 400:
        return
    parsed = None
    try:
        parsed = resp.json()
    except ValueError:
        parsed = (resp.text or "")[:300]
    detail = None
    if isinstance(parsed, dict):
        detail = parsed.get("detail", parsed.get("error"))
    elif isinstance(parsed, str):
        detail = parsed
    msg = _render_detail(detail, fallback)
    if resp.status_code == 401:
        raise HedgeAuthRequired(msg or "Hedge rejected the session.")
    raise HedgeError(msg, resp.status_code)


def request(method: str, path: str, *, params: dict | None = None,
            json_body: dict | None = None, config: type[Config] = Config):
    """Authenticated JSON call against the Hedge broker API."""
    url = env(config)["api_base"] + path
    # Resolve credentials OUTSIDE the try: an expired session is an auth
    # problem, and must surface as HedgeAuthRequired, not "could not reach".
    headers = {**_auth_headers(config), "Accept": "application/json"}
    try:
        resp = requests.request(
            method, url, params=params, json=json_body,
            headers=headers, timeout=config.HEDGE_TIMEOUT)
    except requests.RequestException as e:
        raise HedgeError(f"Could not reach Hedge: {e}") from e
    _raise_for_status(resp, f"Hedge error {resp.status_code}")
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


def upload_document(submission_id: str, pdf_bytes: bytes, filename: str,
                    *, label: str | None = None, config: type[Config] = Config):
    """Attach a PDF (ACORD, loss run, anything) to a submission.

    Multipart field name is "file" — matching the CLI's implementation.
    """
    url = f"{env(config)['api_base']}/broker/submissions/{submission_id}/documents"
    files = {"file": (filename, pdf_bytes, "application/pdf")}
    data = {"name": label} if label else None
    headers = _auth_headers(config)  # auth errors before network errors
    try:
        resp = requests.post(url, files=files, data=data, headers=headers,
                             timeout=config.HEDGE_TIMEOUT)
    except requests.RequestException as e:
        raise HedgeError(f"Could not upload to Hedge: {e}") from e
    _raise_for_status(resp, f"Upload failed ({resp.status_code})")
    try:
        return resp.json()
    except ValueError:
        return {}


def download(path: str, config: type[Config] = Config) -> tuple[bytes, str]:
    """GET a binary document. Returns (bytes, filename)."""
    url = env(config)["api_base"] + path
    headers = _auth_headers(config)  # auth errors before network errors
    try:
        resp = requests.get(url, headers=headers,
                            timeout=config.HEDGE_TIMEOUT)
    except requests.RequestException as e:
        raise HedgeError(f"Could not reach Hedge: {e}") from e
    _raise_for_status(resp, f"Download failed ({resp.status_code})")
    name = ""
    disp = resp.headers.get("Content-Disposition", "")
    if "filename=" in disp:
        name = disp.split("filename=", 1)[1].strip('"; ')
    return resp.content, name or "document.pdf"


# --------------------------------------------------------------------------
# Workflow operations (thin, named wrappers — the route layer stays dumb)
# --------------------------------------------------------------------------
def whoami(config=Config):
    return request("GET", "/broker/me", config=config)


def appetite(params: dict | None = None, config=Config):
    return request("GET", "/broker/appetite", params=params or {}, config=config)


def list_submissions(params: dict | None = None, config=Config):
    return request("GET", "/broker/submissions", params=params or {}, config=config)


def create_submission(body: dict, config=Config):
    return request("POST", "/broker/submissions", json_body=body, config=config)


def get_submission(sid: str, config=Config):
    return request("GET", f"/broker/submissions/{sid}", config=config)


def requirements(sid: str, config=Config):
    return request("GET", f"/broker/submissions/{sid}/requirements", config=config)


def finalize(sid: str, config=Config):
    return request("POST", f"/broker/submissions/{sid}/finalize", config=config)


def quote_sessions(sid: str, config=Config):
    return request("GET", f"/broker/submissions/{sid}/api-quotes/sessions", config=config)


def answer_quote(sid: str, session_id: str, answers: dict, config=Config):
    return request("POST",
                   f"/broker/submissions/{sid}/api-quotes/sessions/{session_id}/answers",
                   json_body=answers, config=config)


def close_quote(sid: str, session_id: str, config=Config):
    return request("POST",
                   f"/broker/submissions/{sid}/api-quotes/sessions/{session_id}/close",
                   config=config)


def finalized_documents(sid: str, config=Config):
    return request("GET", f"/broker/submissions/{sid}/finalized-documents", config=config)


def download_finalized(document_id: str, config=Config):
    return download(f"/broker/finalized-documents/{document_id}/pdf", config=config)


def list_policies(config=Config):
    return request("GET", "/broker/policies", config=config)
