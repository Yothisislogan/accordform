"""Loss run request letter — generated as a WIT-branded PDF.

A loss run request is the standard letter an agency sends a carrier asking for
an insured's claims history. It is NOT an ACORD form — there is no licensed
template to fill — so we compose the document ourselves with reportlab. (Hard
rule #1 is about not recreating ACORD forms; this is WIT's own letterhead.)

The letter body is a deterministic built-in template, not model-generated: the
content is pure boilerplate with the operator's values dropped in, and policy
numbers / date ranges must be reproduced exactly. Nothing here can be
hallucinated, and it works with no API key and no network.

Output is a flat (non-editable) PDF — reportlab draws text directly, so there
is no AcroForm layer to flatten.
"""
from __future__ import annotations

import re
from datetime import date
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

# --- WIT palette (mirrors the CSS tokens; deep blue for text, bright for accents) ---
WIT_BLUE = colors.HexColor("#00AEEF")
WIT_BLUE_DEEP = colors.HexColor("#007EAE")
WIT_INK = colors.HexColor("#06121D")
WIT_BG = colors.HexColor("#F4F8FB")
WIT_GRAY = colors.HexColor("#6B7280")
WIT_BORDER = colors.HexColor("#E2E8F0")

AGENCY_NAME = "We Insure Things"

# Fields the letter cannot be produced without.
REQUIRED_FIELDS = {
    "carrier_name": "Insurance company name",
    "policy_number": "Policy number",
    "insured_name": "Insured name",
    "period_from": "Loss run period (from)",
    "period_to": "Loss run period (to)",
}

_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


class LossRunError(ValueError):
    """Raised when the request can't be built from the supplied fields."""


def validate(fields: dict) -> list[dict]:
    """Return [{key,label,error}] — empty when the fields are usable."""
    fields = fields or {}
    errors = []
    for key, label in REQUIRED_FIELDS.items():
        if not str(fields.get(key, "")).strip():
            errors.append({"key": key, "label": label, "error": "required"})
    for key in ("period_from", "period_to"):
        val = str(fields.get(key, "")).strip()
        if val and not _DATE_RE.match(val):
            errors.append({"key": key, "label": REQUIRED_FIELDS[key],
                           "error": "must be MM/DD/YYYY"})
    return errors


def _clean(fields: dict, key: str) -> str:
    return str((fields or {}).get(key, "") or "").strip()


def _insured_address(fields: dict) -> str:
    """Assemble the address block from its parts, skipping anything blank."""
    line1 = _clean(fields, "insured_address")
    city = _clean(fields, "insured_city")
    st = _clean(fields, "insured_state").upper()
    zipc = _clean(fields, "insured_zip")
    city_line = ", ".join(p for p in [city, " ".join(x for x in [st, zipc] if x)] if p)
    return "<br/>".join(p for p in [line1, city_line] if p)


def _styles():
    ss = getSampleStyleSheet()
    return {
        "body": ParagraphStyle("body", parent=ss["Normal"], fontName="Helvetica",
                               fontSize=10.5, leading=15, textColor=WIT_INK,
                               alignment=TA_JUSTIFY, spaceAfter=10),
        "h": ParagraphStyle("h", parent=ss["Normal"], fontName="Helvetica-Bold",
                            fontSize=12, leading=16, textColor=WIT_BLUE_DEEP,
                            spaceBefore=6, spaceAfter=6),
        "small": ParagraphStyle("small", parent=ss["Normal"], fontName="Helvetica",
                                fontSize=8.5, leading=12, textColor=WIT_GRAY),
        "cell": ParagraphStyle("cell", parent=ss["Normal"], fontName="Helvetica",
                               fontSize=10, leading=14, textColor=WIT_INK),
        "cell_b": ParagraphStyle("cell_b", parent=ss["Normal"], fontName="Helvetica-Bold",
                                 fontSize=10, leading=14, textColor=WIT_INK),
    }


def _draw_letterhead(canvas, doc):
    """Branded header band + footer rule, painted on every page."""
    canvas.saveState()
    w, h = LETTER
    # Ink header band with the bright-blue brand accent underneath.
    canvas.setFillColor(WIT_INK)
    canvas.rect(0, h - 0.9 * inch, w, 0.9 * inch, stroke=0, fill=1)
    canvas.setFillColor(WIT_BLUE)
    canvas.rect(0, h - 0.96 * inch, w, 0.06 * inch, stroke=0, fill=1)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(0.75 * inch, h - 0.55 * inch, AGENCY_NAME)
    canvas.setFont("Helvetica", 9.5)
    canvas.drawRightString(w - 0.75 * inch, h - 0.55 * inch, "Loss Run Request")
    # Footer.
    canvas.setStrokeColor(WIT_BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(0.75 * inch, 0.72 * inch, w - 0.75 * inch, 0.72 * inch)
    canvas.setFillColor(WIT_GRAY)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(0.75 * inch, 0.55 * inch,
                      f"{AGENCY_NAME} — Loss Run Request")
    canvas.drawRightString(w - 0.75 * inch, 0.55 * inch, f"Page {doc.page}")
    canvas.restoreState()


def build_pdf(fields: dict) -> bytes:
    """Compose the loss run request letter. Returns PDF bytes.

    Raises LossRunError if required fields are missing/invalid — callers should
    surface that to the UI rather than emitting a half-built letter.
    """
    errors = validate(fields)
    if errors:
        raise LossRunError("; ".join(f"{e['label']}: {e['error']}" for e in errors))

    st = _styles()
    carrier = _clean(fields, "carrier_name")
    policy = _clean(fields, "policy_number")
    insured = _clean(fields, "insured_name")
    pfrom, pto = _clean(fields, "period_from"), _clean(fields, "period_to")
    requested_by = _clean(fields, "requested_by")
    notes = _clean(fields, "notes")
    addr = _insured_address(fields)
    today = _clean(fields, "request_date") or date.today().strftime("%m/%d/%Y")

    buf = BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=1.2 * inch, bottomMargin=0.9 * inch,
        title=f"Loss Run Request — {insured}",
        author=AGENCY_NAME, subject=f"Loss run request for policy {policy}",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates([PageTemplate(id="letter", frames=[frame],
                                       onPage=_draw_letterhead)])

    story = []
    story.append(Paragraph(today, st["small"]))
    story.append(Spacer(1, 14))
    story.append(Paragraph(carrier, st["h"]))
    story.append(Paragraph("Attn: Claims / Underwriting — Loss Run Department", st["small"]))
    story.append(Spacer(1, 16))

    story.append(Paragraph(
        f"<b>RE: Request for loss runs — {insured} — Policy #{policy}</b>", st["body"]))

    story.append(Paragraph(
        f"To whom it may concern,<br/><br/>"
        f"Please provide currently valued loss runs for the insured and policy "
        f"identified below, covering the period <b>{pfrom}</b> through "
        f"<b>{pto}</b>. Please include all open and closed claims, with paid and "
        f"reserved amounts, claim status, date of loss, and a brief description "
        f"of each claim. If there have been no claims during this period, please "
        f"confirm that in writing as a no-loss letter.", st["body"]))

    # Details table — the operator's values, verbatim.
    rows = [
        ["Insurance company", carrier],
        ["Named insured", insured],
        ["Policy number", policy],
        ["Loss run period", f"{pfrom} – {pto}"],
    ]
    if addr:
        rows.insert(2, ["Insured address", addr])
    data = [[Paragraph(k, st["cell_b"]), Paragraph(v, st["cell"])] for k, v in rows]
    table = Table(data, colWidths=[1.75 * inch, doc.width - 1.75 * inch])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, -1), WIT_BG),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, WIT_BORDER),
        ("BOX", (0, 0), (-1, -1), 0.5, WIT_BORDER),
        ("LINEBEFORE", (0, 0), (0, -1), 2, WIT_BLUE),   # bright-blue accent bar
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
    ]))
    story.append(Spacer(1, 4))
    story.append(table)
    story.append(Spacer(1, 16))

    if notes:
        story.append(Paragraph("<b>Additional information</b>", st["body"]))
        story.append(Paragraph(notes.replace("\n", "<br/>"), st["body"]))

    story.append(Paragraph(
        f"{AGENCY_NAME} is the agent of record for this insured and is "
        f"authorized to request this information on their behalf. Please send "
        f"the loss runs to the agency contact below. If any additional "
        f"authorization is required to release this information, please advise "
        f"and we will provide it promptly.", st["body"]))

    story.append(Paragraph("Thank you for your assistance.", st["body"]))
    story.append(Spacer(1, 22))
    story.append(Paragraph("Sincerely,", st["body"]))
    story.append(Spacer(1, 8))
    if requested_by:
        story.append(Paragraph(f"<b>{requested_by}</b>", st["cell_b"]))
    story.append(Paragraph(AGENCY_NAME, st["small"]))
    for key in ("agency_phone", "agency_email"):
        val = _clean(fields, key)
        if val:
            story.append(Paragraph(val, st["small"]))

    doc.build(story)
    return buf.getvalue()


def suggested_filename(fields: dict) -> str:
    """Safe, descriptive download name."""
    insured = re.sub(r"[^A-Za-z0-9]+", "_", _clean(fields, "insured_name")).strip("_")
    return f"Loss_Run_Request_{insured or 'Insured'}.pdf"
