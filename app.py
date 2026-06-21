"""WIT Forms — Flask application factory and routes.

Wires together auth (M1), the PDF pipeline (M2), catalog/search/render (M3),
preview + local output actions (M4), profiles (M5), and field-usage tracking
(M7). Phase-2 endpoints (drafts, NowCerts, admin re-tag) are stubbed with clean
hooks — they return 501, they do not pretend to work.

Run (dev):   python app.py
Run (prod):  gunicorn -w 3 -b 127.0.0.1:8097 "app:create_app()"
"""
from __future__ import annotations

import secrets
import time
import uuid
from pathlib import Path

from flask import (
    Flask, jsonify, request, send_file, send_from_directory, session,
)

import auth
import db
from config import load_config
from forms_catalog import (
    get_form, get_form_schema, search_forms, seed_catalog,
)
from pdf_fill import PdfFillError, produce_pdf
from profiles import apply_profiles, get_profile, list_profiles, save_profile
from submissions import (
    field_usage_stats, log_submission, mask_pii, record_field_usage,
)
from validation import validate_answers

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(config=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    cfg = config or load_config()
    app.config.from_object(cfg)
    app.config["_CFG"] = cfg

    db.init_app(app)
    auth.init_app(app)

    # Ensure schema + catalog exist on boot.
    with app.app_context():
        db.init_db(Path(app.config["DB_PATH"]))
        try:
            seed_catalog(db.get_db())
        except Exception as e:  # malformed schema must fail loud.
            app.logger.error("Catalog seed failed: %s", e)
            raise

    _register_security(app)
    _register_routes(app)
    return app


# --------------------------------------------------------------------------
# Security: CSRF + cache headers
# --------------------------------------------------------------------------
def _register_security(app: Flask) -> None:
    @app.before_request
    def _csrf_protect():
        # Issue a per-session CSRF token lazily.
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        # Enforce on state-changing API calls only; auth callback is OAuth-state
        # protected by Authlib itself.
        if request.path.startswith("/api/") and request.method in (
            "POST", "PUT", "PATCH", "DELETE"
        ):
            sent = request.headers.get("X-CSRF-Token", "")
            if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
                return jsonify({"error": "invalid or missing CSRF token"}), 403


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def _register_routes(app: Flask) -> None:
    cfg = app.config["_CFG"]

    # ---- Static SPA + health ----
    @app.route("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.route("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC_DIR, filename)

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok", "service": "wit-forms"})

    @app.route("/api/config")
    @auth.api_login_required
    def api_config():
        # Non-secret client config. Email is local-download/user-owned email in Phase 1.
        return jsonify({
            "owner_cc_email": cfg.OWNER_CC_EMAIL,
            "csrf_token": session.get("csrf"),
            "email_enabled": False,
            "email_mode": "local_download",
        })

    # ---- Catalog + search (M3) ----
    @app.route("/api/forms")
    @auth.api_login_required
    def api_forms():
        q = request.args.get("q", "")
        return jsonify({"forms": search_forms(db.get_db(), q)})

    @app.route("/api/forms/<int:form_id>")
    @auth.api_login_required
    def api_form(form_id):
        schema = get_form_schema(db.get_db(), form_id)
        if not schema:
            return jsonify({"error": "form not found"}), 404
        return jsonify(schema)

    # ---- Profiles (M5) ----
    @app.route("/api/profiles")
    @auth.api_login_required
    def api_profiles():
        ptype = request.args.get("type")
        return jsonify({"profiles": list_profiles(db.get_db(), ptype)})

    @app.route("/api/profiles/<int:profile_id>")
    @auth.api_login_required
    def api_profile(profile_id):
        prof = get_profile(db.get_db(), profile_id)
        if not prof:
            return jsonify({"error": "profile not found"}), 404
        return jsonify(prof)

    @app.route("/api/profiles", methods=["POST"])
    @auth.api_login_required
    def api_save_profile():
        body = request.get_json(silent=True) or {}
        try:
            prof = save_profile(
                db.get_db(),
                ptype=body.get("type", ""),
                name=body.get("name", ""),
                data=body.get("data", {}),
                owner_user_id=auth.current_user_id(),
                profile_id=body.get("id"),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(prof)

    # ---- Preview + local output actions (M4) ----
    @app.route("/api/forms/<int:form_id>/preview", methods=["POST"])
    @auth.api_login_required
    def api_preview(form_id):
        ctx = _prepare_fill(form_id)
        if isinstance(ctx, tuple):  # (error_json, status)
            return ctx
        schema, template, answers = ctx
        try:
            out = _output_path(form_id, "preview")
            produce_pdf(schema, template, answers, out, flatten=True,
                        pdftk_bin=cfg.PDFTK_BIN)
        except PdfFillError as e:
            return jsonify({"error": str(e)}), 500
        # Preview is not an audit action and is not logged or usage-counted.
        return send_file(out, mimetype="application/pdf",
                         download_name=f"acord_{schema['_meta']['acord_number']}_preview.pdf")

    @app.route("/api/forms/<int:form_id>/download", methods=["POST"])
    @auth.api_login_required
    def api_download(form_id):
        return _action(form_id, "download")

    @app.route("/api/forms/<int:form_id>/print", methods=["POST"])
    @auth.api_login_required
    def api_print(form_id):
        return _action(form_id, "print")

    @app.route("/api/forms/<int:form_id>/email", methods=["POST"])
    @auth.api_login_required
    def api_email(form_id):
        # Server-side email is intentionally disabled. The Phase 1 flow is:
        # generate a flattened PDF, download it, and let the user attach/send it
        # from their own Gmail/Outlook/mail account. Keeping this endpoint as a
        # download-compatible alias prevents stale UI/API clients from failing.
        return _action(form_id, "download")

    # ---- Field usage (M7) / admin (Phase-2 hook) ----
    @app.route("/api/admin/field-usage/<int:form_id>")
    @auth.admin_required
    def api_field_usage(form_id):
        return jsonify({"usage": field_usage_stats(db.get_db(), form_id)})

    @app.route("/api/admin/forms/<int:form_id>/retag", methods=["POST"])
    @auth.admin_required
    def api_retag(form_id):
        # Phase 2: persist per-field priority/required overrides. Hook only.
        return jsonify({"error": "admin re-tagging is a Phase-2 feature"}), 501

    # ---- Phase-2 stubs ----
    @app.route("/api/drafts", methods=["POST"])
    @auth.api_login_required
    def api_draft_save():
        return jsonify({"error": "drafts are a Phase-2 feature"}), 501

    @app.route("/api/nowcerts/lookup")
    @auth.api_login_required
    def api_nowcerts():
        return jsonify({"error": "NowCerts lookup is a Phase-2 feature"}), 501

    # ---- Shared helpers (closures over cfg) ----
    def _prepare_fill(form_id):
        schema = get_form_schema(db.get_db(), form_id)
        if not schema:
            return jsonify({"error": "form not found"}), 404
        body = request.get_json(silent=True) or {}
        answers = body.get("answers", {}) or {}

        # Merge selected profiles (agency/client) before validating/filling.
        profile_ids = body.get("profile_ids") or []
        profs = [p for p in (get_profile(db.get_db(), pid) for pid in profile_ids) if p]
        if profs:
            answers = apply_profiles(schema, answers, profs)

        errors = validate_answers(schema, answers)
        if errors:
            return jsonify({"error": "validation failed", "fields": errors}), 422

        form = get_form(db.get_db(), form_id)
        template = Path(form["template_path"])
        if not template.exists():
            # Hard rule #1: never synthesize. Tell the operator to prep the
            # licensed template Logan supplied.
            return jsonify({
                "error": (
                    f"Clean template missing: {template.name}. Run "
                    f"tools/prep_template.py on the licensed ACORD "
                    f"{schema['_meta']['acord_number']} PDF first."
                )
            }), 503
        return schema, str(template), answers

    def _action(form_id, action):
        ctx = _prepare_fill(form_id)
        if isinstance(ctx, tuple):
            return ctx
        schema, template, answers = ctx
        try:
            out = _output_path(form_id, action)
            _, result = produce_pdf(schema, template, answers, out,
                                    flatten=True, pdftk_bin=cfg.PDFTK_BIN)
        except PdfFillError as e:
            return jsonify({"error": str(e)}), 500

        record_field_usage(db.get_db(), form_id, result.filled_keys, result.skipped_keys)
        log_submission(
            db.get_db(), user_id=auth.current_user_id(), form_id=form_id,
            action=action, answers=answers, output_path=str(out),
        )
        app.logger.info("%s form=%s answers=%s", action, form_id, mask_pii(answers))
        return send_file(
            out, mimetype="application/pdf", as_attachment=(action == "download"),
            download_name=f"ACORD_{schema['_meta']['acord_number']}.pdf",
        )

    def _output_path(form_id, action) -> Path:
        out_dir = Path(cfg.OUTPUT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        _purge_old_outputs(out_dir, cfg.PDF_RETENTION_DAYS)
        name = f"form{form_id}_{action}_{uuid.uuid4().hex}.pdf"
        return out_dir / name


def _purge_old_outputs(out_dir: Path, retention_days: int) -> None:
    """Best-effort retention: delete generated PDFs older than the window.

    The submissions metadata row is kept regardless (spec §12)."""
    if retention_days <= 0:
        return
    cutoff = time.time() - retention_days * 86400
    for p in out_dir.glob("*.pdf"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


# Module-level app for gunicorn ("app:app") and `python app.py`.
app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=app.config["PORT"], debug=app.config["DEBUG"])
