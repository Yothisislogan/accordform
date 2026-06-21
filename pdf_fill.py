"""PDF fill + flatten pipeline for WIT Forms.

This reproduces the *verified* recipe from the spec — it does not redesign it:

  1. One-time template prep (see tools/prep_template.py):
       pdftk template.pdf output template_clean.pdf drop_xfa
     -> strips the XFA layer (which otherwise shadows AcroForm in Adobe) and
        removes the owner-password copy/change restriction.

  2. Per fill (this module):
       pypdf reads the clean template, maps schema answers -> AcroForm field
       names (prefix + relative name), and writes values with
       update_page_form_field_values(..., auto_regenerate=False).

  3. Flatten before any email/print/download (hard rule #3):
       pdftk filled.pdf output final.pdf flatten
     -> locks the form so the emailed/printed copy is not editable and renders
        identically everywhere.

All value mapping is driven entirely by the schema (hard rule #7) — there are
NO form-specific branches here. Adding a form needs only a new schema JSON.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from pypdf import PdfReader, PdfWriter

# Verified checkbox export values for ACORD AcroForms.
CHECKBOX_ON = "1"
CHECKBOX_OFF = "Off"


class PdfFillError(RuntimeError):
    pass


@dataclass
class FillResult:
    """Mapping plus per-field-key usage stats (drives field_usage analytics)."""
    pdf_data: dict[str, str]
    filled_keys: list[str] = dc_field(default_factory=list)
    skipped_keys: list[str] = dc_field(default_factory=list)


# --------------------------------------------------------------------------
# Value normalisation helpers
# --------------------------------------------------------------------------
def _truthy(val) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    if isinstance(val, (int, float)):
        return val != 0
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on", "checked")


def _nonempty(val) -> bool:
    return val is not None and str(val).strip() != ""


def _yn(val) -> str | None:
    """Normalise a yes/no answer to literal 'Y' or 'N' text (not a checkbox)."""
    if val is None or str(val).strip() == "":
        return None
    s = str(val).strip().lower()
    if s in ("y", "yes", "true", "1"):
        return "Y"
    if s in ("n", "no", "false", "0"):
        return "N"
    # Already a literal — pass through uppercased first char if Y/N-ish.
    return "Y" if s.startswith("y") else "N"


# --------------------------------------------------------------------------
# Schema -> PDF field mapping (the schema-driven core)
# --------------------------------------------------------------------------
def build_field_values(schema: dict, answers: dict) -> FillResult:
    """Translate user `answers` (keyed by schema field `key`) into a flat
    {full_pdf_field_name: value} dict, honouring every schema rule:

      * field_name_prefix is prepended to every relative pdf_field
      * excluded optional blocks contribute NO pdf fields (acceptance test #2)
      * radio groups emit exactly one '1', the rest 'Off' (acceptance test #3)
      * checkboxes -> '1' / omitted; yn_code -> literal 'Y'/'N'
      * insurer_ref writes the chosen letter; the A-F table writes name/NAIC
    """
    meta = schema["_meta"]
    prefix = meta.get("field_name_prefix", "")
    res = FillResult(pdf_data={})

    def put(rel_field: str, value: str) -> None:
        res.pdf_data[prefix + rel_field] = value

    # --- Insurers A-F reference table ---
    insurers_answers = answers.get("_insurers") or {}
    insurers_block = schema.get("insurers") or {}
    for row in insurers_block.get("rows", []):
        letter = row.get("letter")
        info = insurers_answers.get(letter) or insurers_answers.get(str(letter)) or {}
        if isinstance(info, str):  # tolerate {"A": "Acme"} shorthand
            info = {"name": info}
        if _nonempty(info.get("name")):
            put(row["name_pdf_field"], str(info["name"]).strip())
            if row.get("naic_pdf_field") and _nonempty(info.get("naic")):
                put(row["naic_pdf_field"], str(info["naic"]).strip())

    # --- Sections ---
    for section in schema.get("sections", []):
        fields = section.get("fields", [])

        if section.get("optional_block"):
            toggle = section.get("include_toggle", {})
            included = _truthy(answers.get(toggle.get("key")))
            if not included:
                # Excluded block: write nothing, count every field as skipped.
                res.skipped_keys.extend(f["key"] for f in fields)
                continue
            # Block included: if the toggle itself maps to a PDF checkbox, set it.
            if toggle.get("pdf_field") and toggle.get("type") == "checkbox":
                put(toggle["pdf_field"], CHECKBOX_ON)

        for f in fields:
            _write_field(f, answers, put, res)

    return res


def _write_field(f: dict, answers: dict, put, res: FillResult) -> None:
    key = f["key"]
    ftype = f.get("type", "text")

    if ftype == "radio_group":
        selected = answers.get(key)
        if not _nonempty(selected):
            res.skipped_keys.append(key)
            return
        wrote_one = False
        for opt in f.get("options", []):
            on = str(opt.get("label")) == str(selected) or str(opt.get("value")) == str(selected)
            if on and wrote_one:
                on = False  # safety: never a second '1'
            put(opt["pdf_field"], CHECKBOX_ON if on else CHECKBOX_OFF)
            wrote_one = wrote_one or on
        res.filled_keys.append(key) if wrote_one else res.skipped_keys.append(key)
        return

    if ftype == "checkbox":
        if _truthy(answers.get(key)):
            put(f["pdf_field"], CHECKBOX_ON)
            res.filled_keys.append(key)
        else:
            res.skipped_keys.append(key)
        return

    if ftype == "yn_code":
        code = _yn(answers.get(key))
        if code is not None:
            put(f["pdf_field"], code)
            res.filled_keys.append(key)
        else:
            res.skipped_keys.append(key)
        return

    if ftype == "insurer_ref":
        val = answers.get(key)
        if _nonempty(val):
            put(f["pdf_field"], str(val).strip().upper())
            res.filled_keys.append(key)
        else:
            res.skipped_keys.append(key)
        return

    # Plain value types: text/textarea/number/currency/date/phone/email/state/select
    val = answers.get(key)
    if _nonempty(val):
        put(f["pdf_field"], str(val).strip())
        res.filled_keys.append(key)
    else:
        res.skipped_keys.append(key)


# --------------------------------------------------------------------------
# Low-level PDF operations
# --------------------------------------------------------------------------
def fill_pdf(clean_template_path: str | Path, pdf_data: dict[str, str],
             out_path: str | Path) -> Path:
    """Fill the AcroForm layer of a *clean* (drop_xfa'd) template via pypdf."""
    clean_template_path = Path(clean_template_path)
    out_path = Path(out_path)
    if not clean_template_path.exists():
        raise PdfFillError(f"clean template not found: {clean_template_path}")

    reader = PdfReader(str(clean_template_path))
    writer = PdfWriter()
    writer.append(reader)

    # Fields absent in this edition are skipped (don't crash) — editions drift.
    available = set((reader.get_fields() or {}).keys())
    data = {k: v for k, v in pdf_data.items() if not available or k in available}

    for page in writer.pages:
        writer.update_page_form_field_values(page, data, auto_regenerate=False)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        writer.write(fh)
    return out_path


def _pdftk(args: list[str], pdftk_bin: str = "pdftk") -> None:
    if shutil.which(pdftk_bin) is None:
        raise PdfFillError(
            f"'{pdftk_bin}' not found. Install it: sudo apt-get install pdftk"
        )
    proc = subprocess.run([pdftk_bin, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise PdfFillError(f"pdftk failed: {proc.stderr.strip() or proc.stdout.strip()}")


def flatten_pdf(in_path: str | Path, out_path: str | Path,
                pdftk_bin: str = "pdftk") -> Path:
    """Flatten so the result is non-editable (hard rule #3)."""
    in_path, out_path = Path(in_path), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _pdftk([str(in_path), "output", str(out_path), "flatten"], pdftk_bin)
    return out_path


def produce_pdf(schema: dict, clean_template_path: str | Path, answers: dict,
                out_path: str | Path, *, flatten: bool = True,
                pdftk_bin: str = "pdftk") -> tuple[Path, FillResult]:
    """End-to-end: map answers -> fill -> (flatten). Returns (path, FillResult).

    `flatten=True` (default) for any email/print/download. A non-flattened
    intermediate is used only transiently for the flatten step.
    """
    result = build_field_values(schema, answers)
    out_path = Path(out_path)

    if not flatten:
        fill_pdf(clean_template_path, result.pdf_data, out_path)
        return out_path, result

    with tempfile.TemporaryDirectory() as tmp:
        filled = Path(tmp) / "filled.pdf"
        fill_pdf(clean_template_path, result.pdf_data, filled)
        flatten_pdf(filled, out_path, pdftk_bin=pdftk_bin)
    return out_path, result
