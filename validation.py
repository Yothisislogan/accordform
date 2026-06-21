"""Server-side answer validation (mirrors the client-side rules).

Validation is schema-driven: types and `required` come from the form schema,
so there are no per-form branches. Required checks respect optional-block
inclusion and show_if visibility — a required field inside an excluded coverage
block is not enforced (acceptance test #2 territory).
"""
from __future__ import annotations

import re

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR", "VI", "GU", "AS", "MP",
}

_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"\d")


def _nonempty(v) -> bool:
    return v is not None and str(v).strip() != ""


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on") if v is not None else False


def _type_error(ftype: str, value) -> str | None:
    s = str(value).strip()
    if ftype == "date":
        if not _DATE_RE.match(s):
            return "must be MM/DD/YYYY"
    elif ftype == "email":
        if not _EMAIL_RE.match(s):
            return "invalid email address"
    elif ftype == "state":
        if s.upper() not in US_STATES:
            return "invalid US state code"
    elif ftype == "phone":
        digits = re.sub(r"\D", "", s)
        if len(digits) < 10:
            return "phone must have at least 10 digits"
    elif ftype in ("number", "currency"):
        cleaned = re.sub(r"[\s,$]", "", s)
        try:
            float(cleaned)
        except ValueError:
            return "must be a number"
    return None


def _section_included(section: dict, answers: dict) -> bool:
    if not section.get("optional_block"):
        return True
    toggle = section.get("include_toggle", {})
    return _truthy(answers.get(toggle.get("key")))


def _visible(field: dict, section: dict, answers: dict) -> bool:
    if not _section_included(section, answers):
        return False
    cond = field.get("show_if")
    if not cond:
        return True
    # Visible if the gating answer is truthy. Virtual reveal flags (not present
    # in answers) resolve falsey -> field treated as hidden, which is correct.
    return _truthy(answers.get(cond))


def validate_answers(schema: dict, answers: dict) -> list[dict]:
    """Return a list of {key, label, error}. Empty list == valid."""
    answers = answers or {}
    errors: list[dict] = []
    included_blocks = 0
    has_optional_blocks = False

    for section in schema.get("sections", []):
        if section.get("optional_block"):
            has_optional_blocks = True
            if _section_included(section, answers):
                included_blocks += 1

        for field in section.get("fields", []):
            key = field["key"]
            ftype = field.get("type", "text")
            value = answers.get(key)
            visible = _visible(field, section, answers)

            if field.get("required") and visible and not _nonempty(value):
                errors.append({"key": key, "label": field.get("label", key),
                               "error": "required"})
                continue

            if _nonempty(value) and ftype in (
                "date", "email", "state", "phone", "number", "currency"
            ):
                msg = _type_error(ftype, value)
                if msg:
                    errors.append({"key": key, "label": field.get("label", key),
                                   "error": msg})

    # "At least one coverage block required" (schema logic rule).
    if has_optional_blocks and included_blocks == 0:
        errors.append({"key": "_blocks", "label": "Coverage",
                       "error": "include at least one coverage block"})

    return errors
