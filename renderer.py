"""WiTNext-only, stdin/stdout PDF renderer. No customer files or network I/O.

Templates are licensed, reviewed blank PDFs mounted read-only inside WiTNext.
Run `python renderer.py audit FORM` to prepare the mapping review. Never pass
customer answers on the command line; the private API uses stdin.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import simpleSplit

ROOT = Path(__file__).resolve().parent


class RenderError(ValueError):
    pass


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def schema_for(form_id):
    if not re.fullmatch(r"acord_\d+(?:_[a-z]+)?", form_id):
        raise RenderError("Unknown form")
    path = ROOT / "schemas" / (form_id + ".json")
    if not path.is_file():
        raise RenderError("Unknown form")
    raw = path.read_bytes()
    return json.loads(raw), digest(raw)


def fields(schema):
    for section in schema.get("sections", []):
        if section.get("include_toggle"):
            yield section, section["include_toggle"]
        for field in section.get("fields", []):
            yield section, field


def targets(schema):
    prefix = schema["_meta"].get("field_name_prefix", "")
    for _, field in fields(schema):
        if field.get("pdf_field"):
            yield prefix + field["pdf_field"]
        for option in field.get("options", []):
            if isinstance(option, dict) and option.get("pdf_field"):
                yield prefix + option["pdf_field"]
    for row in schema.get("insurers", {}).get("rows", []):
        for key in ("name_pdf_field", "naic_pdf_field"):
            if row.get(key):
                yield prefix + row[key]


def mapping(schema, available):
    resolved, unresolved, ambiguous = {}, [], []
    for target in sorted(set(targets(schema))):
        # Exact names win. A page-independent leaf is only safe when UNIQUE.
        matches = [target] if target in available else [
            n for n in available if n.split(".")[-1] == target.split(".")[-1]
        ]
        if len(matches) == 1:
            resolved[target] = matches[0]
        elif matches:
            ambiguous.append(target)
        else:
            unresolved.append(target)
    return resolved, unresolved, ambiguous


def audit(form_id, directory=None):
    schema, schema_hash = schema_for(form_id)
    directory = Path(directory or os.environ.get("FORMS_TEMPLATE_DIR", "/run/witnext-forms"))
    template = directory / (form_id + ".pdf")
    if not template.is_file():
        raise RenderError("Licensed blank template has not been installed")
    raw = template.read_bytes()
    reader = PdfReader(io.BytesIO(raw))
    available = reader.get_fields() or {}
    resolved, unresolved, ambiguous = mapping(schema, available)
    # Fail closed on a non-blank fillable source. Static/flattened content still
    # needs human blank-template review, recorded in the manifest below.
    nonblank = [n for n, f in available.items() if str(f.get("/V", "")) not in ("", "/Off", "Off")]
    result = {
        "formId": form_id, "edition": schema["_meta"]["edition"],
        "schemaSha256": schema_hash, "templateSha256": digest(raw),
        "mappingSha256": digest(json.dumps(resolved, sort_keys=True).encode()),
        "mapped": resolved, "unresolved": unresolved, "ambiguous": ambiguous,
        "unmapped": sorted(set(available) - set(resolved.values())),
        "duplicateTargets": sorted(n for n, count in Counter(targets(schema)).items() if count > 1),
        "nonblankFields": nonblank, "pdfFieldCount": len(available),
    }
    return result, raw


def approved_template(form_id):
    report, raw = audit(form_id)
    path = Path(os.environ.get("FORMS_TEMPLATE_DIR", "/run/witnext-forms")) / (form_id + ".review.json")
    if not path.is_file():
        raise RenderError("Template field review is required before PDF generation")
    review = json.loads(path.read_text())
    hashes = ("schemaSha256", "templateSha256", "mappingSha256")
    if any(review.get(k) != report[k] for k in hashes):
        raise RenderError("Template or mapping changed; a new field review is required")
    if (not report["pdfFieldCount"] or report["nonblankFields"] or report["unresolved"]
            or report["ambiguous"] or report["duplicateTargets"] or review.get("blankTemplateReviewed") is not True):
        raise RenderError("Template is not blank or field mapping is incomplete or ambiguous")
    # Every omission must have an explicit review reason. No percentage-based
    # approval can conceal a missing signature, vehicle, driver or location.
    omissions = review.get("omissions", {})
    if set(omissions) != set(report["unmapped"]) or any(
        not isinstance(v, str) or len(v.strip()) < 10 for v in omissions.values()
    ):
        raise RenderError("Every unmapped PDF field needs a documented review decision")
    return report, raw


def truthy(value):
    return str(value).lower() in ("true", "1", "yes", "y", "on")


def visible(section, field, answers):
    if section.get("optional_block") and field is not section.get("include_toggle"):
        if not truthy(answers.get(section.get("include_toggle", {}).get("key"))):
            return False
    return not field.get("show_if") or truthy(answers.get(field["show_if"]))


def validate(schema, answers, required=True):
    errors = []
    allowed = {f["key"] for _, f in fields(schema)} | {"_insurers"}
    # Virtual reveal controls can gate schema fields but have no PDF target.
    allowed |= {f["show_if"] for _, f in fields(schema) if f.get("show_if")}
    if set(answers) - allowed:
        raise RenderError("Answers include unknown schema keys")
    for section, field in fields(schema):
        if not visible(section, field, answers):
            continue
        key, kind = field["key"], field.get("type", "text")
        value = answers.get(key, "")
        if isinstance(value, (dict, list)):
            errors.append({"key": key, "error": "Use a single answer"})
            continue
        text = str(value).strip() if value is not None else ""
        error = None
        if not text:
            if required and field.get("required"):
                error = "Required"
        elif len(text) > field.get("max_length", 5000):
            error = "Answer is too long"
        elif kind == "date":
            try:
                if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", text):
                    raise ValueError()
                datetime.strptime(text, "%m/%d/%Y")
            except ValueError:
                error = "Use a valid date in MM/DD/YYYY format"
        elif kind == "yn_code" and text.upper() not in ("Y", "N", "YES", "NO"):
            error = "Choose Yes or No"
        elif kind == "radio_group" and text not in [str(o.get("value", o.get("label"))) for o in field.get("options", [])]:
            error = "Choose one of the listed options"
        elif kind == "checkbox" and value not in (True, False, "", "true", "false"):
            error = "Choose checked or unchecked"
        elif kind == "email" and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", text):
            error = "Use a valid email"
        elif kind in ("number", "currency") and not re.fullmatch(r"-?\d+(?:\.\d+)?", re.sub(r"[,$ ]", "", text)):
            error = "Use a finite number"
        if error:
            errors.append({"key": key, "error": error})
    return errors


def render(form_id, answers):
    schema, _ = schema_for(form_id)
    errors = validate(schema, answers)
    if errors:
        return {"errors": errors}
    report, raw = approved_template(form_id)
    reader = PdfReader(io.BytesIO(raw))
    available = reader.get_fields() or {}
    prefix = schema["_meta"].get("field_name_prefix", "")
    values = {}

    def put(target, value):
        name = report["mapped"][prefix + target]
        field = available[name]
        if field.get("/FT") == "/Sig":
            raise RenderError("A digital signature field cannot be filled as text; use the signing workflow")
        if field.get("/FT") == "/Btn" and value != "/Off":
            states = field.get("/_States_", [])
            on = [str(s) for s in states if str(s) != "/Off"]
            if len(on) != 1:
                raise RenderError("Checkbox export values require template review")
            value = on[0]
        if field.get("/MaxLen") and len(value) > int(field["/MaxLen"]):
            raise RenderError("An answer exceeds its PDF field capacity")
        values[name] = value

    for section, field in fields(schema):
        if not visible(section, field, answers):
            continue
        value = answers.get(field["key"], "")
        kind = field.get("type", "text")
        if kind == "radio_group":
            for option in field.get("options", []):
                chosen = str(value) == str(option.get("value", option.get("label")))
                put(option["pdf_field"], "1" if chosen else "/Off")
        elif field.get("pdf_field"):
            if kind == "checkbox":
                put(field["pdf_field"], "1" if truthy(value) else "/Off")
            elif value not in (None, ""):
                put(field["pdf_field"], str(value).upper()[0] if kind == "yn_code" else str(value))
    for row in schema.get("insurers", {}).get("rows", []):
        info = answers.get("_insurers", {}).get(row["letter"], {})
        for key, target in (("name", "name_pdf_field"), ("naic", "naic_pdf_field")):
            if info.get(key) and row.get(target):
                put(row[target], str(info[key]))
    writer = PdfWriter()
    writer.append(reader)
    writer.update_page_form_field_values(None, values, auto_regenerate=False, flatten=True)
    writer.remove_annotations("/Widget")
    writer._root_object.pop("/AcroForm", None)
    output = io.BytesIO()
    writer.write(output)
    pdf = output.getvalue()
    return {"pdf": base64.b64encode(pdf).decode(), "sha256": digest(pdf),
            "templateSha256": report["templateSha256"], "mappingSha256": report["mappingSha256"]}


def sign(payload):
    raw = base64.b64decode(payload["pdf"], validate=True)
    if digest(raw) != payload["documentSha256"]:
        raise RenderError("Signature document checksum mismatch")
    mark = payload["name"]
    if not isinstance(mark, str) or not 1 <= len(mark.strip()) <= 150:
        raise RenderError("Enter the signer's full name")
    receipt = io.BytesIO()
    c = canvas.Canvas(receipt, pagesize=(612, 792))
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if not font_path.is_file():
        raise RenderError("The signature font is not installed")
    pdfmetrics.registerFont(TTFont("WiTNextSans", str(font_path)))
    c.setFont("WiTNextSans", 16)
    c.drawString(48, 742, "WiTNext electronic signature record")
    c.setFont("WiTNextSans", 11)
    # Text is emitted as PDF strings, never HTML or executable markup.
    rows = ["Electronic signature: " + mark, "Role: " + payload["role"],
            "Consent: I reviewed this document and agree to sign it electronically.",
            "Signed at: " + payload["signedAt"], "Session: " + payload["sessionId"],
            "Document SHA-256 (pages before this record):", payload["documentSha256"],
            "Authentication: private, expiring, single-use signing link.",
            "This record is an electronic signature, not a certificate-based digital signature."]
    y = 705
    for row in rows:
        for line in simpleSplit(row, "WiTNextSans", 11, 516):
            c.drawString(48, y, line)
            y -= 16
        y -= 9
    c.save()
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(raw)))
    writer.append(PdfReader(io.BytesIO(receipt.getvalue())))
    out = io.BytesIO(); writer.write(out)
    return {"pdf": base64.b64encode(out.getvalue()).decode(), "sha256": digest(out.getvalue())}


def run(payload):
    operation = payload.get("operation")
    if operation == "sign":
        return sign(payload)
    if operation == "render":
        return render(payload["formId"], payload["answers"])
    if operation == "validate":
        schema, _ = schema_for(payload["formId"])
        return {"errors": validate(schema, payload["answers"], payload.get("required", True))}
    if operation == "audit":
        return audit(payload["formId"])[0]
    raise RenderError("Unsupported renderer operation")


if __name__ == "__main__":
    try:
        payload = {"operation": "audit", "formId": sys.argv[2]} if len(sys.argv) == 3 and sys.argv[1] == "audit" else json.load(sys.stdin)
        print(json.dumps(run(payload)))
    except RenderError as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(2)
    except Exception:
        # Never print answer values, PDF bytes, or library exceptions to logs.
        print(json.dumps({"error": "PDF processing failed; check the reviewed template configuration"}))
        sys.exit(2)
