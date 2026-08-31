"""Map WIT Forms answers onto a Hedge submission body.

Data-driven: `mappings/hedge_submission.json` lists candidate source keys per
target (first non-empty wins), so one map serves several ACORD forms whose
auto-generated key names differ. Adding a form means editing the JSON, never
adding an if-branch here — the same rule the PDF side follows.

Two Hedge quirks are enforced here rather than discovered as a 422:
  * mailing_address must be complete (line1, city, state, zip) or omitted;
  * primary_state is only sent when there is NO complete mailing address,
    otherwise the two can disagree and the API rejects the submission.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

MAP_PATH = Path(__file__).resolve().parent / "mappings" / "hedge_submission.json"

ADDRESS_PARTS = ("line1", "city", "state", "zip")
_DATE_US = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


class MappingError(ValueError):
    pass


@lru_cache(maxsize=1)
def load_map(path: str | None = None) -> dict:
    p = Path(path) if path else MAP_PATH
    if not p.exists():
        raise MappingError(f"Hedge mapping not found: {p}")
    data = json.loads(p.read_text())
    if not isinstance(data.get("fields"), dict):
        raise MappingError(f"{p}: missing 'fields' object")
    return data


# --------------------------------------------------------------------------
# Transforms
# --------------------------------------------------------------------------
def _t_state(v: str) -> str:
    return str(v).strip().upper()[:2]


def _t_digits(v: str) -> str:
    return re.sub(r"\D", "", str(v))


def _t_date_iso(v: str) -> str:
    """MM/DD/YYYY (what the ACORD forms collect) -> YYYY-MM-DD (what Hedge wants)."""
    s = str(v).strip()
    m = _DATE_US.match(s)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
    return s  # already ISO, or something the caller must fix


TRANSFORMS = {"state": _t_state, "digits": _t_digits, "date_iso": _t_date_iso}


def _set_path(target: dict, dotted: str, value) -> None:
    node = target
    parts = dotted.split(".")
    for key in parts[:-1]:
        node = node.setdefault(key, {})
    node[parts[-1]] = value


def _first_value(answers: dict, candidates: list[str]):
    for key in candidates:
        val = answers.get(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return None


# --------------------------------------------------------------------------
# Public
# --------------------------------------------------------------------------
def build_submission_body(answers: dict, *, overrides: dict | None = None,
                          mapping: dict | None = None) -> dict:
    """Turn form answers into a Hedge submission body.

    `overrides` are merged last so a CSR can correct anything on the review
    screen before it is sent — the mapping is a head start, not a straitjacket.
    """
    spec = (mapping or load_map())["fields"]
    body: dict = {}

    for target, rule in spec.items():
        val = _first_value(answers or {}, rule.get("from", []))
        if val is None:
            continue
        tname = rule.get("transform")
        if tname:
            fn = TRANSFORMS.get(tname)
            if not fn:
                raise MappingError(f"unknown transform '{tname}' for {target}")
            val = fn(val)
        if val == "":
            continue
        _set_path(body, target, val)

    # Overrides are merged BEFORE normalising: normalising first would drop an
    # incomplete address, and an override supplying the one missing part (a ZIP,
    # say) would then have nothing left to complete.
    for key, val in (overrides or {}).items():
        if val is None or str(val).strip() == "":
            continue
        _set_path(body, key, str(val).strip() if isinstance(val, str) else val)

    _normalise_address(body)
    return body


def _normalise_address(body: dict) -> None:
    """Enforce Hedge's all-or-nothing mailing address, and the state rule."""
    applicant = body.get("applicant") or {}
    addr = applicant.get("mailing_address") or {}
    present = {k: v for k, v in addr.items() if str(v or "").strip()}
    complete = all(present.get(p) for p in ADDRESS_PARTS)

    if complete:
        applicant["mailing_address"] = present
        body.pop("primary_state", None)      # state lives on the address
    else:
        # Incomplete address would 422 — drop it, but keep the state usable.
        state = present.get("state")
        applicant.pop("mailing_address", None)
        if state:
            body["primary_state"] = state
    if applicant:
        body["applicant"] = applicant
    elif "applicant" in body:
        body.pop("applicant")


def missing_required(body: dict) -> list[str]:
    """What Hedge needs before `submit` will succeed (insured + narrative)."""
    missing = []
    if not ((body.get("applicant") or {}).get("insured_name")):
        missing.append("applicant.insured_name")
    if not str(body.get("narrative") or "").strip():
        missing.append("narrative")
    return missing


def address_status(body: dict) -> str:
    """'complete' | 'dropped' | 'absent' — so the UI can explain itself."""
    addr = (body.get("applicant") or {}).get("mailing_address")
    if addr and all(addr.get(p) for p in ADDRESS_PARTS):
        return "complete"
    return "dropped" if body.get("primary_state") else "absent"
