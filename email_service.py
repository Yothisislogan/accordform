"""Email delivery for filled ACORD forms.

HARD RULE #4: the owner CC (OWNER_CC_EMAIL) is enforced HERE, server-side, on
every outbound email — it is added regardless of what the client sends, and the
client cannot remove it. The attached PDF must already be flattened by the
caller (hard rule #3); this module does not fill or flatten.

Transport is config-driven (EMAIL_TRANSPORT = 'smtp' | 'sendgrid'). Never put
PII in the subject line (spec §11).
"""
from __future__ import annotations

import base64
import smtplib
from email.message import EmailMessage
from pathlib import Path

from config import Config


class EmailError(RuntimeError):
    pass


def _recipients(to: list[str]) -> list[str]:
    seen, out = set(), []
    for addr in to or []:
        a = (addr or "").strip()
        if a and a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


def send_form_email(*, to: list[str], subject: str, body: str,
                    pdf_path: str | Path, pdf_filename: str,
                    config: type[Config] = Config) -> dict:
    """Send the flattened PDF. Returns {to, cc} actually used (CC enforced).

    The owner CC is appended server-side and de-duplicated against `to`.
    """
    to_list = _recipients(to)
    if not to_list:
        raise EmailError("at least one recipient is required")

    owner_cc = (config.OWNER_CC_EMAIL or "").strip()
    # Enforce owner CC unconditionally; drop it from CC only if already a direct
    # recipient (avoid duplicate delivery), but it is ALWAYS copied either way.
    cc_list = [owner_cc] if owner_cc and owner_cc.lower() not in {a.lower() for a in to_list} else []

    pdf_bytes = Path(pdf_path).read_bytes()

    if config.EMAIL_TRANSPORT == "sendgrid":
        _send_sendgrid(config, to_list, cc_list, subject, body, pdf_bytes, pdf_filename)
    else:
        _send_smtp(config, to_list, cc_list, subject, body, pdf_bytes, pdf_filename)

    return {"to": to_list, "cc": [owner_cc] if owner_cc else []}


def _send_smtp(config, to_list, cc_list, subject, body, pdf_bytes, pdf_filename):
    if not config.SMTP_HOST:
        raise EmailError("SMTP not configured (SMTP_HOST missing). TODO from Logan.")
    msg = EmailMessage()
    msg["From"] = f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM}>"
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf",
                       filename=pdf_filename)

    all_rcpts = to_list + cc_list  # smtplib sends to the full envelope explicitly
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
            if config.SMTP_USE_TLS:
                s.starttls()
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg, to_addrs=all_rcpts)
    except (smtplib.SMTPException, OSError) as e:
        raise EmailError(f"SMTP send failed: {e}") from e


def _send_sendgrid(config, to_list, cc_list, subject, body, pdf_bytes, pdf_filename):
    if not config.SENDGRID_API_KEY:
        raise EmailError("SendGrid not configured (SENDGRID_API_KEY missing).")
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import (
            Attachment, Cc, Content, Disposition, FileContent, FileName,
            FileType, Mail, To,
        )
    except ImportError as e:
        raise EmailError("sendgrid package not installed") from e

    message = Mail(
        from_email=(config.EMAIL_FROM, config.EMAIL_FROM_NAME),
        subject=subject,
        plain_text_content=Content("text/plain", body),
    )
    message.to = [To(a) for a in to_list]
    if cc_list:
        message.cc = [Cc(a) for a in cc_list]
    message.attachment = Attachment(
        FileContent(base64.b64encode(pdf_bytes).decode()),
        FileName(pdf_filename),
        FileType("application/pdf"),
        Disposition("attachment"),
    )
    try:
        SendGridAPIClient(config.SENDGRID_API_KEY).send(message)
    except Exception as e:  # sendgrid raises various exception types
        raise EmailError(f"SendGrid send failed: {e}") from e
