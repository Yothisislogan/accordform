"""Gemini proposal generator: output contract, auth gating, key containment."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BRAND_HEXES = ["#00AEEF", "#007EAE", "#06121D", "#F4F8FB", "#6B7280", "#33D17A", "#FF6868"]


# --- The build instructions always demand HTML5 + WIT brand colors ---
def test_system_instruction_mandates_html5_and_brand():
    from gemini_service import SYSTEM_INSTRUCTION as si

    assert "<!DOCTYPE html>" in si
    assert "</html>" in si
    assert "markdown" in si.lower() and "```" in si   # fences explicitly forbidden
    for hexcode in BRAND_HEXES:
        assert hexcode in si, f"brand color {hexcode} missing from instructions"
    # Contrast rule: bright blue must be called out as accent-only.
    assert "ACCENTS ONLY" in si
    # Don't invent insurance facts.
    assert "TBD" in si


# --- Output is scrubbed to HTML only, whatever the model wraps it in ---
@pytest.mark.parametrize("raw", [
    "```html\n<!DOCTYPE html><html><body>hi</body></html>\n```",
    "Sure! Here's your proposal:\n\n<!DOCTYPE html><html><body>hi</body></html>",
    "<!DOCTYPE html><html><body>hi</body></html>\n\nLet me know if you'd like changes!",
    "```\n<!DOCTYPE html><html><body>hi</body></html>```",
])
def test_clean_html_strips_fences_and_prose(raw):
    from gemini_service import clean_html

    out = clean_html(raw)
    assert out.startswith("<!DOCTYPE html>")
    assert out.endswith("</html>")
    assert "```" not in out
    assert "Sure!" not in out and "Let me know" not in out


def test_clean_html_rejects_empty():
    from gemini_service import GeminiError, clean_html

    with pytest.raises(GeminiError):
        clean_html("")


def test_build_prompt_skips_blanks_and_requires_input():
    from gemini_service import GeminiError, build_prompt

    prompt = build_prompt({"client_name": "Acme LLC", "premium": "", "notes": None})
    assert "Client Name: Acme LLC" in prompt
    assert "Premium" not in prompt          # blank fields are dropped
    assert "TBD" in prompt                  # tells the model not to invent
    with pytest.raises(GeminiError):
        build_prompt({"client_name": "  "})


# --- Fails loudly (not silently) when the key is absent ---
def test_generate_without_key_raises(monkeypatch):
    import config
    from gemini_service import GeminiError, generate_proposal

    monkeypatch.setattr(config.Config, "GEMINI_API_KEY", "")
    with pytest.raises(GeminiError, match="not configured"):
        generate_proposal({"client_name": "Acme"}, config=config.Config)


# --- Route is auth-gated like every other /api route ---
def test_proposal_route_requires_auth(app):
    c = app.test_client()
    # No CSRF token: rejected by the CSRF guard, which runs before the view.
    r = c.post("/api/proposal/generate", json={"fields": {"client_name": "x"}})
    assert r.status_code == 403
    # Valid CSRF but no session user: rejected by the auth decorator.
    with c.session_transaction() as s:
        s["csrf"] = "t"
    r = c.post("/api/proposal/generate", json={"fields": {"client_name": "x"}},
               headers={"X-CSRF-Token": "t"})
    assert r.status_code == 401


def test_proposal_unconfigured_returns_503(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
        s["email"] = "logan@weinsurethings.com"
        s["csrf"] = "t"
    r = c.post("/api/proposal/generate", json={"fields": {"client_name": "Acme"}},
               headers={"X-CSRF-Token": "t"})
    assert r.status_code == 503
    assert "GEMINI_API_KEY" in r.get_json()["error"]


# --- The API key never reaches the browser ---
def test_api_key_never_sent_to_client(app, monkeypatch):
    import config
    monkeypatch.setattr(config.Config, "GEMINI_API_KEY", "SECRET-KEY-VALUE")
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
        s["email"] = "logan@weinsurethings.com"
        s["csrf"] = "t"
    payload = c.get("/api/config").get_data(as_text=True)
    assert "SECRET-KEY-VALUE" not in payload      # only a boolean is exposed
    assert "proposal_enabled" in payload
    # The page never holds a key and never calls Gemini directly — it posts to
    # our own endpoint. (It may name the env var in a "not configured" hint.)
    page = (ROOT / "static" / "proposal.html").read_text()
    assert "generativelanguage.googleapis.com" not in page
    assert "x-goog-api-key" not in page.lower()
    assert "/api/proposal/generate" in page
