"""PDF fill + flatten pipeline for WIT Forms.

This reproduces the verified recipe from the spec:

  1. One-time template prep (see tools/prep_template.py):
       pdftk template.pdf output template_clean.pdf drop_xfa

  2. Per fill:
       pypdf reads the clean template, maps schema answers to AcroForm fields,
       and writes values with update_page_form_field_values(..., auto_regenerate=False).

  3. Flatten before any email/print/download:
       pdftk filled.pdf output final.pdf flatten

All value mapping is driven by the schema. Adding a form should not require
form-specific branches in this module.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from pypdf import PdfReader, PdfWriter

CHECKBOX_ON = "1"
CHECKBOX_OFF = "Off"


class PdfFillError(RuntimeError):
    pass


@dataclass
class FillResult:
    """Mapping plus per-field-key usage stats."""
    pdf_data: dict[str, str]
    filled_keys: list[str] = dc_field(default_factory=list)
    skipped_keys: list[str] = dc_field(default_factory=list)


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
    """Normalise a yes/no answer to literal Y or N text."""
    if val is None or str(val).strip() == "":
        return None
    s = str(val).strip().lower()
    if s in ("y", "yes", "true", "1"):
        return "Y"
    if s in ("n", "no", "false", "0"):
        return "N"
    return "Y" if s.startswith("y") else "N"


def build_field_values(schema: dict, answers: dict) -> FillResult:
    """Translate schema-keyed answers into {pdf_field_name: value}."""
    meta = schema["_meta"]
    prefix = meta.get("field_name_prefix", "")
    res = FillResult(pdf_data={})

    def put(rel_field: str, value: str) -> None:
        res.pdf_data[prefix + rel_field] = value

    insurers_answers = answers.get("_insurers") or {}
    insurers_block = schema.get("insurers") or {}
    for row in insurers_block.get("rows", []):
        letter = row.get("letter")
        info = insurers_answers.get(letter) or insurers_answers.get(str(letter)) or {}
        if isinstance(info, str):
            info = {"name": info}
        if _nonempty(info.get("name")):
            put(row["name_pdf_field"], str(info["name"]).strip())
            if row.get("naic_pdf_field") and _nonempty(info.get("naic")):
                put(row["naic_pdf_field"], str(info["naic"]).strip())

    for section in schema.get("sections", []):
        fields = section.get("fields", [])

        if section.get("optional_block"):
            toggle = section.get("include_toggle", {})
            included = _truthy(answers.get(toggle.get("key")))
            if not included:
                res.skipped_keys.extend(f["key"] for f in fields)
                continue
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
                on = False
            put(opt["pdf_field"], str(opt.get("on_value", CHECKBOX_ON)) if on else CHECKBOX_OFF)
            wrote_one = wrote_one or on
        res.filled_keys.append(key) if wrote_one else res.skipped_keys.append(key)
        return

    if ftype == "checkbox":
        if _truthy(answers.get(key)):
            put(f["pdf_field"], str(f.get("on_value", CHECKBOX_ON)))
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

    val = answers.get(key)
    if _nonempty(val):
        put(f["pdf_field"], str(val).strip())
        res.filled_keys.append(key)
    else:
        res.skipped_keys.append(key)


def _logical_suffix(field_name: str) -> str:
    """Return the logical ACORD field tail after the outer F[0]. prefix.

    Some ACORD PDFs include page containers in the real AcroForm names, e.g.
    F[0].P1[0].Producer_FullName_A[0]. Some schemas intentionally omit that
    page token and store F[0].Producer_FullName_A[0]. This helper lets the fill
    step resolve those forms without hardcoding per-form page names.
    """
    if field_name.startswith("F[0]."):
        return field_name[len("F[0]."):]
    return field_name


def _resolve_pdf_data(pdf_data: dict[str, str], available: set[str]) -> dict[str, str]:
    """Map schema field names to actual PDF field names.

    Exact matches win. If a schema omits an ACORD page token like P1[0], find a
    unique available field whose name ends with the same logical suffix. This is
    generic support for ACORD editions that nest fields by page.
    """
    if not available:
        return pdf_data

    resolved: dict[str, str] = {}
    for key, value in pdf_data.items():
        if key in available:
            resolved[key] = value
            continue

        suffix = _logical_suffix(key)
        candidates = [name for name in available if name.endswith("." + suffix)]
        if len(candidates) == 1:
            resolved[candidates[0]] = value

    return resolved


def fill_pdf(clean_template_path: str | Path, pdf_data: dict[str, str],
             out_path: str | Path) -> Path:
    """Fill the AcroForm layer of a clean template via pypdf."""
    clean_template_path = Path(clean_template_path)
    out_path = Path(out_path)
    if not clean_template_path.exists():
        raise PdfFillError(f"clean template not found: {clean_template_path}")

    reader = PdfReader(str(clean_template_path))
    writer = PdfWriter()
    writer.append(reader)

    available = set((reader.get_fields() or {}).keys())
    data = _resolve_pdf_data(pdf_data, available)

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
    """Flatten so the result is non-editable."""
    in_path, out_path = Path(in_path), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _pdftk([str(in_path), "output", str(out_path), "flatten"], pdftk_bin)
    return out_path


def produce_pdf(schema: dict, clean_template_path: str | Path, answers: dict,
                out_path: str | Path, *, flatten: bool = True,
                pdftk_bin: str = "pdftk") -> tuple[Path, FillResult]:
    """End-to-end: map answers -> fill -> optionally flatten."""
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
