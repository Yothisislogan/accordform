"""Gemini API connector for the WIT insurance proposal generator.

Thin, dependency-light client for the Gemini REST API. Verified against the
public v1beta discovery document:

    POST https://generativelanguage.googleapis.com/v1beta/models/<model>:generateContent
    header: x-goog-api-key: <key>
    body:   { systemInstruction: Content, contents: [Content], generationConfig: {...} }
    reply:  { candidates: [ { content: { parts: [ {text} ] }, finishReason } ], ... }

Two hard rules are baked into the system instruction and enforced again on the
way out (belt and braces, because a model can always drift):

  1. Output is ALWAYS a complete HTML5 document — no markdown, no code fences,
     no commentary before or after.
  2. It is ALWAYS styled in the We Insure Things palette.

The API key is read from the environment and never leaves the server.
"""
from __future__ import annotations

import re

import requests

from config import Config

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# --- WIT brand palette (single source of truth for what we tell the model) ---
WIT_PALETTE = {
    "blue": "#00AEEF",       # bright brand blue — ACCENTS ONLY (fails contrast as text)
    "blue_deep": "#007EAE",  # deep blue — headings, links, button fills (AA on white)
    "ink": "#06121D",        # dark surfaces / strong text
    "white": "#FFFFFF",
    "bg": "#F4F8FB",         # soft page background
    "gray": "#6B7280",       # muted text
    "green": "#33D17A",      # success / included
    "red": "#FF6868",        # alert / excluded
}

SYSTEM_INSTRUCTION = f"""\
You are the proposal writer for We Insure Things (WIT), an independent \
insurance agency. You produce client-facing insurance proposals.

OUTPUT CONTRACT — follow exactly, every time:
- Respond with ONE complete HTML5 document and NOTHING else.
- The very first characters of your response MUST be `<!DOCTYPE html>` and the \
last MUST be `</html>`.
- Never wrap the output in markdown code fences (no ```html). Never add \
commentary, preamble, or explanation before or after the document.
- The document must be fully self-contained: all CSS inside a single `<style>` \
tag in the `<head>`. No external stylesheets, fonts, scripts, images, or \
network requests of any kind. Use system fonts.
- Semantic HTML5 (`<header>`, `<main>`, `<section>`, `<table>`, `<footer>`), \
`<meta charset="utf-8">` and a responsive viewport meta tag.
- It must print cleanly on US Letter: include an `@media print` block and \
avoid breaking tables across pages where practical.

WIT BRAND STYLING — use these exact colors and nothing else:
- Bright WIT blue {WIT_PALETTE['blue']}: ACCENTS ONLY (rules, table header \
bars, left borders, highlights). Never use it for body text on white — it \
fails accessibility contrast.
- Deep WIT blue {WIT_PALETTE['blue_deep']}: headings, links, and button/banner \
fills with white text.
- Ink {WIT_PALETTE['ink']}: primary text and dark header bands.
- White {WIT_PALETTE['white']}: content surfaces.
- Soft background {WIT_PALETTE['bg']}: page background and zebra table rows.
- Muted gray {WIT_PALETTE['gray']}: secondary/label text and fine print.
- Green {WIT_PALETTE['green']}: included/covered indicators.
- Red {WIT_PALETTE['red']}: excluded/declined indicators and warnings.
Design: clean, generous whitespace, a branded header band, clear section \
headings, and coverage tables with striped rows. Professional, not flashy.

PROPOSAL CONTENT:
- Structure: header (agency + client + proposal date), a short introduction, \
coverage summary table(s) with limits/deductibles/premium, notable \
exclusions or conditions, next steps, and a closing footer.
- Use ONLY the facts supplied by the user. Do NOT invent carriers, premiums, \
limits, policy numbers, or effective dates. Where a needed value was not \
supplied, write `TBD` so a human can fill it in — never guess a number.
- Always include a short disclaimer in the footer stating the proposal is a \
summary for discussion only, that coverage is subject to carrier approval and \
the actual policy terms, and that the policy language governs.
"""


class GeminiError(RuntimeError):
    """Raised when the Gemini call fails or returns unusable output."""


def build_prompt(fields: dict) -> str:
    """Turn the proposal form fields into the user turn for the model.

    Purely a formatter — every label is generic, so adding a field to the form
    needs no change here.
    """
    lines = []
    for key, value in (fields or {}).items():
        if value is None or str(value).strip() == "":
            continue
        label = str(key).replace("_", " ").strip().title()
        lines.append(f"- {label}: {str(value).strip()}")
    if not lines:
        raise GeminiError("Add at least one detail before generating a proposal.")
    return (
        "Write the insurance proposal as a complete HTML5 document using these "
        "details.\nAnything not listed here is unknown — mark it TBD rather than "
        "inventing it.\n\n" + "\n".join(lines)
    )


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*|\s*```\s*$")


def clean_html(text: str) -> str:
    """Strip anything that isn't the HTML5 document itself.

    Removes markdown code fences and any stray prose the model may put before
    `<!DOCTYPE html>` / after `</html>`. This is the second half of the
    "HTML only" guarantee — the system instruction is the first half.
    """
    if not text:
        raise GeminiError("Gemini returned an empty response.")
    out = _FENCE_RE.sub("", text.strip()).strip()

    # Trim leading prose: start at the doctype, or failing that at <html>/<!--.
    lower = out.lower()
    start = lower.find("<!doctype html")
    if start == -1:
        start = lower.find("<html")
    if start > 0:
        out = out[start:]

    # Trim trailing prose after the closing tag.
    end = out.lower().rfind("</html>")
    if end != -1:
        out = out[: end + len("</html>")]

    out = out.strip()
    if "<" not in out:
        raise GeminiError("Gemini did not return HTML.")
    return out


def generate_proposal(fields: dict, *, config: type[Config] = Config) -> dict:
    """Generate a branded HTML5 proposal. Returns {html, model}.

    Raises GeminiError with a readable message on any failure — the caller
    surfaces it to the UI rather than showing a blank panel.
    """
    api_key = (config.GEMINI_API_KEY or "").strip()
    if not api_key:
        raise GeminiError(
            "Gemini is not configured. Set GEMINI_API_KEY in the environment file."
        )

    model = config.GEMINI_MODEL
    url = f"{API_BASE}/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": build_prompt(fields)}]}],
        "generationConfig": {
            "temperature": 0.4,          # factual, low embellishment
            "maxOutputTokens": 8192,
        },
    }

    try:
        resp = requests.post(
            url,
            json=body,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            timeout=config.GEMINI_TIMEOUT,
        )
    except requests.RequestException as e:
        raise GeminiError(f"Could not reach Gemini: {e}") from e

    if resp.status_code != 200:
        raise GeminiError(_api_error(resp, model))

    try:
        data = resp.json()
    except ValueError as e:
        raise GeminiError("Gemini returned a malformed response.") from e

    candidates = data.get("candidates") or []
    if not candidates:
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        raise GeminiError(
            f"Gemini returned no content (blocked: {blocked})." if blocked
            else "Gemini returned no content."
        )

    first = candidates[0]
    parts = ((first.get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts)
    if first.get("finishReason") == "MAX_TOKENS" and "</html>" not in text.lower():
        raise GeminiError(
            "The proposal was cut off before it finished. Try shortening the input."
        )

    return {"html": clean_html(text), "model": data.get("modelVersion") or model}


def _api_error(resp, model: str) -> str:
    """Turn a Gemini error payload into something a CSR can act on."""
    detail = ""
    try:
        detail = ((resp.json().get("error") or {}).get("message") or "").strip()
    except ValueError:
        detail = (resp.text or "").strip()[:200]
    if resp.status_code in (401, 403):
        return f"Gemini rejected the API key ({resp.status_code}). {detail}"
    if resp.status_code == 404:
        return (
            f"Model '{model}' was not found. Set GEMINI_MODEL to a model your "
            f"key can use. {detail}"
        )
    if resp.status_code == 429:
        return "Gemini rate limit reached. Wait a moment and try again."
    return f"Gemini error {resp.status_code}: {detail or 'unknown error'}"
