"""Google OAuth 2.0 auth, domain-restricted to @weinsurethings.com.

Same pattern as the existing WIT Sales Tracker. On callback we verify the
Google ID token, reject any email whose domain is not on the allowlist
(hard rule #5), auto-provision the `users` row on first valid login, and store
a server-side session. Admin role is granted to ADMIN_EMAILS (Logan) and can
also be set manually in the DB.

Decorators:
    @login_required      -> for HTML routes; redirects to /auth/login
    @api_login_required  -> for /api/* routes; returns 401 JSON
    @admin_required      -> 403 unless session role == 'admin'
"""
from __future__ import annotations

import functools

from authlib.integrations.flask_client import OAuth
from flask import (
    Blueprint, current_app, jsonify, redirect, request, session, url_for,
)

from db import get_db

oauth = OAuth()
bp = Blueprint("auth", __name__, url_prefix="/auth")


def init_app(app) -> None:
    oauth.init_app(app)
    if app.config.get("GOOGLE_CLIENT_ID"):
        oauth.register(
            name="google",
            server_metadata_url=app.config["GOOGLE_DISCOVERY_URL"],
            client_id=app.config["GOOGLE_CLIENT_ID"],
            client_secret=app.config["GOOGLE_CLIENT_SECRET"],
            client_kwargs={"scope": "openid email profile"},
        )
    app.register_blueprint(bp)


# --------------------------------------------------------------------------
# Allowlist
# --------------------------------------------------------------------------
def email_allowed(email: str) -> bool:
    if not email:
        return False
    email = email.strip().lower()
    cfg = current_app.config
    if email in cfg.get("ALLOWED_EMAILS", []):
        return True
    domain = email.rsplit("@", 1)[-1]
    return domain in cfg.get("ALLOWED_DOMAINS", [])


def _provision_user(email: str, name: str) -> dict:
    """Insert the user on first login; return the row as a dict."""
    email = email.strip().lower()
    role = "admin" if email in current_app.config.get("ADMIN_EMAILS", []) else "user"
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO users (email, name, role) VALUES (?, ?, ?)",
            (email, name, role),
        )
        db.commit()
        row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    elif role == "admin" and row["role"] != "admin":
        # Promote a known admin if they were provisioned earlier as a user.
        db.execute("UPDATE users SET role = 'admin' WHERE id = ?", (row["id"],))
        db.commit()
        row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@bp.route("/login")
def login():
    if not current_app.config.get("GOOGLE_CLIENT_ID"):
        return (
            "Google OAuth is not configured. Set GOOGLE_CLIENT_ID / "
            "GOOGLE_CLIENT_SECRET (TODO from Logan).",
            503,
        )
    redirect_uri = current_app.config.get("OAUTH_REDIRECT_URI") or url_for(
        "auth.callback", _external=True
    )
    return oauth.google.authorize_redirect(redirect_uri)


@bp.route("/callback")
def callback():
    token = oauth.google.authorize_access_token()
    userinfo = token.get("userinfo") or oauth.google.userinfo()
    email = (userinfo.get("email") or "").lower()
    verified = userinfo.get("email_verified", True)

    if not verified or not email_allowed(email):
        session.clear()
        return (
            "Access denied. WIT Forms is restricted to @weinsurethings.com "
            "accounts.",
            403,
        )

    user = _provision_user(email, userinfo.get("name") or email)
    session.clear()
    session["user_id"] = user["id"]
    session["email"] = user["email"]
    session["name"] = user["name"]
    session["role"] = user["role"]
    return redirect("/")


@bp.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    if request.method == "GET":
        return redirect("/")
    return jsonify({"ok": True})


@bp.route("/me")
def me():
    if "user_id" not in session:
        return jsonify({"authenticated": False}), 401
    return jsonify({
        "authenticated": True,
        "email": session.get("email"),
        "name": session.get("name"),
        "role": session.get("role"),
    })


# --------------------------------------------------------------------------
# Decorators
# --------------------------------------------------------------------------
def current_user_id() -> int | None:
    return session.get("user_id")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def api_login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "authentication required"}), 401
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "authentication required"}), 401
        if session.get("role") != "admin":
            return jsonify({"error": "admin only"}), 403
        return view(*args, **kwargs)
    return wrapped
