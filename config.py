"""Central configuration for WIT Forms.

All secrets and environment-specific values are read from the environment
(systemd `EnvironmentFile` in production, a local `.env` in dev). Nothing
secret is hardcoded here. Values that Logan still owes us (OWNER_CC_EMAIL,
OAuth creds, email transport) fall back to safe placeholders with a TODO so
the app boots for development without blocking — but email/auth will refuse
to operate until the real values are supplied (see the `is_configured`
helpers below).
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


class Config:
    # --- Core Flask ---
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")  # TODO(logan): set in prod
    PORT = int(os.environ.get("PORT", "8097"))  # 8095/8096 taken by WIT Connect
    DEBUG = _bool("FLASK_DEBUG", False)

    # Secure session cookies (served behind nginx TLS in prod).
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", not DEBUG)

    # --- Paths ---
    DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
    DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "witforms.db"))
    SCHEMAS_DIR = Path(os.environ.get("SCHEMAS_DIR", BASE_DIR / "schemas"))
    TEMPLATES_DIR = Path(os.environ.get("TEMPLATES_DIR", BASE_DIR / "templates" / "acord"))
    OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", DATA_DIR / "output"))

    # Generated-PDF retention (days). submissions metadata row is kept regardless.
    PDF_RETENTION_DAYS = int(os.environ.get("PDF_RETENTION_DAYS", "30"))  # TODO(logan): confirm

    # --- Auth (Google OAuth 2.0, domain-restricted) ---
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")  # TODO(logan): supply
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")  # TODO(logan): supply
    GOOGLE_DISCOVERY_URL = (
        "https://accounts.google.com/.well-known/openid-configuration"
    )
    OAUTH_REDIRECT_URI = os.environ.get(
        "OAUTH_REDIRECT_URI", "https://forms.weinsurethings.com/auth/callback"
    )
    # Domain allowlist. Default is the WIT domain; extra exceptions can be added
    # via ALLOWED_EMAILS (full addresses) for edge cases.
    ALLOWED_DOMAINS = _csv("ALLOWED_DOMAINS", "weinsurethings.com")
    ALLOWED_EMAILS = [e.lower() for e in _csv("ALLOWED_EMAILS", "")]
    ADMIN_EMAILS = [e.lower() for e in _csv("ADMIN_EMAILS", "logan@weinsurethings.com")]

    # --- Email ---
    # Owner CC is ENFORCED server-side on every outbound email (hard rule #4).
    OWNER_CC_EMAIL = os.environ.get("OWNER_CC_EMAIL", "owner@weinsurethings.com")  # TODO(logan)
    EMAIL_TRANSPORT = os.environ.get("EMAIL_TRANSPORT", "smtp").lower()  # 'smtp' | 'sendgrid'
    EMAIL_FROM = os.environ.get("EMAIL_FROM", "forms@weinsurethings.com")
    EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "We Insure Things")

    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_USE_TLS = _bool("SMTP_USE_TLS", True)

    SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")  # TODO(logan) if using SendGrid

    # --- Tooling ---
    PDFTK_BIN = os.environ.get("PDFTK_BIN", "pdftk")

    # --- Helpers ---------------------------------------------------------
    @classmethod
    def auth_configured(cls) -> bool:
        return bool(cls.GOOGLE_CLIENT_ID and cls.GOOGLE_CLIENT_SECRET)

    @classmethod
    def email_configured(cls) -> bool:
        if cls.EMAIL_TRANSPORT == "sendgrid":
            return bool(cls.SENDGRID_API_KEY)
        return bool(cls.SMTP_HOST)


def load_config() -> type[Config]:
    """Return the active config class (extension point for envs)."""
    return Config
