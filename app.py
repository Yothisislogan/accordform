"""WIT Forms — Flask application factory and routes.

Wires together auth (M1), the PDF pipeline (M2), catalog/search/render (M3),
preview + 3 actions (M4), profiles (M5), and field-usage tracking (M7).
Phase-2 endpoints (drafts, NowCerts, admin re-tag) are stubbed with clean
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
from email_service import EmailError, send_form_email
from forms_catalog import (
    get_form, get_form_schema, search_forms, seed_catalog,
)
from pdf_fill import (
    PdfFillError, build_field_values, flat_map_to_pdf_data, produce_pdf,
)
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
        except Exception as e:  # malformed schema must fail loud — but not on a
            app.logger.error("Catalog seed failed: %s", e)  # bare static asset.
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
        # Non-secret client config: locked owner CC + csrf token.
        return jsonify({
            "owner_cc_email": cfg.OWNER_CC_EMAIL,
            "csrf_token": session.get("csrf"),
            "email_enabled": cfg.email_configured(),
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

    # ---- Preview + 3 actions (M4) ----
    @app.route("/api/forms/<int:form_id>/preview", methods=["POST"])
    @auth.api_login_required
    def api_preview(form_id):
        ctx = _prepare_fill(form_id)
        if not isinstance(ctx, dict):  # error (response, status)
            return ctx
        schema = ctx["schema"]
        try:
            out = _output_path(form_id, "preview")
            produce_pdf(schema, ctx["template"], out_path=out,
                        pdf_data=_fill_data(ctx), flatten=True,
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
        body = request.get_json(silent=True) or {}
        recipients = [r for r in (body.get("recipients") or []) if r and r.strip()]
        if not recipients:
            return jsonify({"error": "at least one recipient is required"}), 400

        ctx = _prepare_fill(form_id)
        if not isinstance(ctx, dict):
            return ctx
        schema = ctx["schema"]

        try:
            out = _output_path(form_id, "email")
            produce_pdf(schema, ctx["template"], out_path=out,
                        pdf_data=_fill_data(ctx), flatten=True,
                        pdftk_bin=cfg.PDFTK_BIN)
        except PdfFillError as e:
            return jsonify({"error": str(e)}), 500

        title = schema["_meta"]["title"]
        subject = body.get("subject") or f"Your {title} from We Insure Things"
        message = body.get("message") or (
            f"Attached is your {title}.\n\n— We Insure Things"
        )
        try:
            # OWNER_CC is enforced inside send_form_email regardless of input.
            sent = send_form_email(
                to=recipients, subject=subject, body=message,
                pdf_path=out,
                pdf_filename=f"ACORD_{schema['_meta']['acord_number']}.pdf",
                config=cfg,
            )
        except EmailError as e:
            return jsonify({"error": str(e)}), 502

        _record_usage(ctx, form_id)
        log_submission(
            db.get_db(), user_id=auth.current_user_id(), form_id=form_id,
            action="email", answers=ctx["answers"],
            recipient_emails=",".join(sent["to"]),
            cc_emails=",".join(sent["cc"]), output_path=str(out),
        )
        app.logger.info("email sent form=%s to=%d cc=%d answers=%s",
                        form_id, len(sent["to"]), len(sent["cc"]),
                        mask_pii(ctx["answers"]))
        return jsonify({"ok": True, "to": sent["to"], "cc": sent["cc"]})

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
        """Validate + locate template. Returns a CONTEXT DICT on success, or a
        Flask (response, status) tuple on error.

        Accepts the flat-map contract (TEST-WIRE-UP §0): the front end resolves
        all schema logic and sends `fields` = { relative_pdf_field: value }
        (authoritative for filling). Keyed `answers` remain optional and drive
        server-side validation, field-usage analytics, and the audit snapshot.
        """
        schema = get_form_schema(db.get_db(), form_id)
        if not schema:
            return jsonify({"error": "form not found"}), 404
        body = request.get_json(silent=True) or {}
        has_answers = "answers" in body          # distinguishes {} from absent
        answers = body.get("answers") or {}
        flat = body.get("fields")  # flat {relative_pdf_field: value} map

        # Merge selected profiles into the keyed answers (validation/usage view).
        profile_ids = body.get("profile_ids") or []
        profs = [p for p in (get_profile(db.get_db(), pid) for pid in profile_ids) if p]
        if profs and answers:
            answers = apply_profiles(schema, answers, profs)

        # Server-side validation runs whenever the client sends keyed answers
        # (defense in depth — the SPA always does). A flat-map-ONLY POST skips it.
        if has_answers:
            errors = validate_answers(schema, answers)
            if errors:
                return jsonify({"error": "validation failed", "fields": errors}), 422
        elif flat is None:
            return jsonify({"error": "no answers or fields provided"}), 400

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
        return {"schema": schema, "template": str(template),
                "answers": answers, "flat": flat}

    def _fill_data(ctx):
        """Authoritative fill data: the flat map when present (front end resolved
        the logic), else resolve the keyed answers server-side (back-compat). In
        both cases fill_pdf's page-token resolver maps names to the template."""
        if ctx["flat"] is not None:
            return flat_map_to_pdf_data(ctx["schema"]["_meta"], ctx["flat"])
        return build_field_values(ctx["schema"], ctx["answers"]).pdf_data

    def _record_usage(ctx, form_id):
        """field_usage is keyed by field_key, so derive it from the keyed
        answers (the flat map has no keys). No-op for flat-map-only POSTs."""
        if not ctx["answers"]:
            return
        res = build_field_values(ctx["schema"], ctx["answers"])
        record_field_usage(db.get_db(), form_id, res.filled_keys, res.skipped_keys)

    def _action(form_id, action):
        ctx = _prepare_fill(form_id)
        if not isinstance(ctx, dict):
            return ctx
        schema = ctx["schema"]
        try:
            out = _output_path(form_id, action)
            produce_pdf(schema, ctx["template"], out_path=out,
                        pdf_data=_fill_data(ctx), flatten=True,
                        pdftk_bin=cfg.PDFTK_BIN)
        except PdfFillError as e:
            return jsonify({"error": str(e)}), 500

        _record_usage(ctx, form_id)
        log_submission(
            db.get_db(), user_id=auth.current_user_id(), form_id=form_id,
            action=action, answers=ctx["answers"], output_path=str(out),
        )
        app.logger.info("%s form=%s answers=%s", action, form_id, mask_pii(ctx["answers"]))
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
