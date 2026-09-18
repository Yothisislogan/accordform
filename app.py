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
from io import BytesIO

import hedge_mapping
import hedge_service
import loss_run
from gemini_service import GeminiError, generate_proposal
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
        except Exception as e:  # malformed schema must fail loud.
            app.logger.error("Catalog seed failed: %s", e)
            raise

    _register_security(app)
    _register_routes(app)
    from hedge.integrations import register_routes
    register_routes(app)

    # Background remote-state poller (Phase 2). Only worth a thread when live
    # writes are enabled; the loop itself also no-ops while signed out.
    if getattr(cfg, "HEDGE_LIVE", False):
        from hedge import api_client as _hedge_pipeline
        _hedge_pipeline.start_poller(app, config=cfg)
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

    @app.route("/proposal")
    def proposal_page():
        return send_from_directory(STATIC_DIR, "proposal.html")

    @app.route("/loss-run")
    def loss_run_page():
        return send_from_directory(STATIC_DIR, "loss-run.html")

    @app.route("/hedge")
    def hedge_page():
        return send_from_directory(STATIC_DIR, "hedge.html")

    @app.route("/appetite")
    def appetite_page():
        return send_from_directory(STATIC_DIR, "appetite.html")

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok", "service": "wit-forms"})

    @app.route("/api/config")
    @auth.api_login_required
    def api_config():
        # Non-secret client config. Email is local-download/user-owned email in
        # Phase 1. The Gemini API key is server-side only and never sent here.
        return jsonify({
            "owner_cc_email": cfg.OWNER_CC_EMAIL,
            "csrf_token": session.get("csrf"),
            "email_enabled": False,
            "email_mode": "local_download",
            "proposal_enabled": cfg.gemini_configured(),
        })

    # ---- Insurance proposal generator (Gemini) ----
    @app.route("/api/proposal/generate", methods=["POST"])
    @auth.api_login_required
    def api_proposal_generate():
        body = request.get_json(silent=True) or {}
        fields = body.get("fields") or {}
        try:
            result = generate_proposal(fields, config=cfg)
        except GeminiError as e:
            # 503 when simply unconfigured, 502 when the upstream call failed.
            code = 503 if not cfg.gemini_configured() else 502
            return jsonify({"error": str(e)}), code
        app.logger.info("proposal generated model=%s fields=%s",
                        result["model"], sorted(fields.keys()))
        return jsonify(result)

    # ---- Loss run request (non-ACORD; generated letter, not a filled template) ----
    @app.route("/api/loss-run/generate", methods=["POST"])
    @auth.api_login_required
    def api_loss_run():
        fields = (request.get_json(silent=True) or {}).get("fields") or {}
        errs = loss_run.validate(fields)
        if errs:
            return jsonify({"error": "validation failed", "fields": errs}), 422
        try:
            pdf_bytes = loss_run.build_pdf(fields)
        except loss_run.LossRunError as e:
            return jsonify({"error": str(e)}), 422

        # Audit like any other produced document. form_id 0 == non-ACORD output.
        out = _output_path(0, "loss_run")
        out.write_bytes(pdf_bytes)
        log_submission(
            db.get_db(), user_id=auth.current_user_id(), form_id=0,
            action="loss_run", answers=fields, output_path=str(out),
        )
        app.logger.info("loss run generated fields=%s", mask_pii(fields))
        return send_file(out, mimetype="application/pdf", as_attachment=True,
                         download_name=loss_run.suggested_filename(fields))

    # ---- Hedge Phase 1: public feeds (no Hedge account required) ----
    @app.route("/api/hedge/public/feed/<name>")
    @auth.api_login_required
    def hedge_public_feed(name):
        from hedge import public_client
        try:
            res = public_client.get_feed(name, cfg,
                                         force=request.args.get("force") == "1")
        except ValueError as e:
            return jsonify({"error": str(e)}), 404
        return jsonify(dict(res))

    @app.route("/api/hedge/public/appetite")
    @auth.api_login_required
    def hedge_public_appetite():
        """Published appetite, VERBATIM. Directional only — never account
        approval; 'review case by case' is never softened to yes."""
        from hedge import feed_adapter, public_client
        klass = request.args.get("class", "")
        state = request.args.get("state", "")
        feed = public_client.get_feed("appetite.json", cfg)
        entries = (feed_adapter.appetite_entries(feed["data"], klass=klass,
                                                 state=state)
                   if feed["data"] is not None else None)
        return jsonify({
            "disclaimer": ("Directional — published appetite only. Confirmed "
                           "only after Hedge review; never account approval."),
            "source": feed["source"], "warning": feed["warning"],
            "map_confirmed": entries is not None,
            "entries": entries,                       # verbatim rows, or null
            "raw": feed["data"] if entries is None else None,
            "classes": feed_adapter.appetite_classes(feed["data"])
                       if feed["data"] is not None else None,
        })

    @app.route("/api/hedge/public/checklist")
    @auth.api_login_required
    def hedge_public_checklist():
        """Submission-prep checklist cross-referenced against what WIT Forms
        already generated for this client (the concrete gap list)."""
        from hedge import artifacts, feed_adapter, public_client
        client_ref = request.args.get("client", "")
        feed = public_client.get_feed(
            "commercial-insurance-submission-checklist.json", cfg)
        items = (feed_adapter.checklist_items(feed["data"])
                 if feed["data"] is not None else None)
        have = artifacts.artifacts_for_client(db.get_db(), client_ref)
        out = {
            "source": feed["source"], "warning": feed["warning"],
            "map_confirmed": items is not None,
            "raw": feed["data"] if items is None else None,
            "artifacts": have,
        }
        if items is not None:
            out["gap"] = feed_adapter.gap_list(items, set(have["acords"]),
                                               have["loss_runs"])
        return jsonify(out)

    # ---- Hedge Phase 2: submission pipeline (behind HEDGE_LIVE) ----
    from hedge import api_client as hedge_pipeline

    def _pipe(fn, *a, **kw):
        try:
            return jsonify(fn(*a, **kw))
        except hedge_pipeline.PipelineError as e:
            return jsonify({"error": str(e)}), e.status
        except hedge_service.HedgeAuthRequired as e:
            return jsonify({"error": str(e), "signed_in": False}), 401
        except hedge_service.HedgeError as e:
            return jsonify({"error": f"Hedge returned: {e}"}), e.status or 502

    @app.route("/api/hedge/pipeline")
    @auth.api_login_required
    def hedge_pipe_list():
        return jsonify({"submissions": hedge_pipeline.list_local(db.get_db())})

    @app.route("/api/hedge/pipeline", methods=["POST"])
    @auth.api_login_required
    def hedge_pipe_create():
        body = request.get_json(silent=True) or {}
        payload = body.get("body")
        if payload is None:
            payload = hedge_mapping.build_submission_body(
                body.get("answers") or {}, overrides=body.get("overrides") or {})
        missing = hedge_mapping.missing_required(payload)
        if missing:
            return jsonify({"error": "Hedge needs these before submitting: "
                                     + ", ".join(missing), "missing": missing}), 422
        if (hedge_service.auth_mode(cfg) == "client_credentials"
                and not payload.get("producer_email")):
            payload["producer_email"] = session.get("email")
        return _pipe(hedge_pipeline.create_draft, db.get_db(),
                     client_ref=body.get("client_ref")
                     or (payload.get("applicant") or {}).get("insured_name", ""),
                     payload=payload,
                     class_desc=str(payload.get("narrative", ""))[:200],
                     state_code=(payload.get("applicant") or {})
                                .get("mailing_address", {}).get("state")
                                or payload.get("primary_state", ""),
                     lines=payload.get("lines_of_business") or [],
                     effective_date=payload.get("effective_date", ""),
                     actor=session.get("email", ""))

    @app.route("/api/hedge/pipeline/<int:lid>")
    @auth.api_login_required
    def hedge_pipe_get(lid):
        def build():
            row = hedge_pipeline.public_row(hedge_pipeline.get_local(db.get_db(), lid))
            row["docs"] = hedge_pipeline.docs_for(db.get_db(), lid)
            row["events"] = hedge_pipeline.events_for(db.get_db(), lid)
            return row
        return _pipe(build)

    @app.route("/api/hedge/pipeline/<int:lid>/send", methods=["POST"])
    @auth.api_login_required
    def hedge_pipe_send(lid):
        return _pipe(hedge_pipeline.send_create, db.get_db(), lid,
                     actor=session.get("email", ""), config=cfg)

    @app.route("/api/hedge/pipeline/<int:lid>/docs", methods=["POST"])
    @auth.api_login_required
    def hedge_pipe_docs(lid):
        body = request.get_json(silent=True) or {}
        if "file" in request.files:
            f = request.files["file"]
            return _pipe(hedge_pipeline.upload_doc, db.get_db(), lid,
                         source="upload", filename=f.filename or "document.pdf",
                         pdf_bytes=f.read(), actor=session.get("email", ""),
                         config=cfg)
        form_id = body.get("form_id")
        if form_id:
            ctx = _prepare_fill(int(form_id))
            if not isinstance(ctx, dict):
                return ctx
            out = _output_path(int(form_id), "hedge")
            try:
                produce_pdf(ctx["schema"], ctx["template"], out_path=out,
                            pdf_data=_fill_data(ctx), flatten=True,
                            pdftk_bin=cfg.PDFTK_BIN)
            except PdfFillError as e:
                return jsonify({"error": str(e)}), 500
            number = ctx["schema"]["_meta"]["acord_number"]
            return _pipe(hedge_pipeline.upload_doc, db.get_db(), lid,
                         source=f"acord_{number}",
                         filename=f"ACORD_{number}.pdf",
                         pdf_bytes=out.read_bytes(),
                         actor=session.get("email", ""), config=cfg)
        if body.get("loss_run"):
            errs = loss_run.validate(body["loss_run"])
            if errs:
                return jsonify({"error": "validation failed", "fields": errs}), 422
            return _pipe(hedge_pipeline.upload_doc, db.get_db(), lid,
                         source="loss_run",
                         filename=loss_run.suggested_filename(body["loss_run"]),
                         pdf_bytes=loss_run.build_pdf(body["loss_run"]),
                         actor=session.get("email", ""), config=cfg)
        return jsonify({"error": "provide a file, a form_id, or loss_run fields"}), 400

    @app.route("/api/hedge/pipeline/<int:lid>/requirements")
    @auth.api_login_required
    def hedge_pipe_requirements(lid):
        return _pipe(hedge_pipeline.fetch_requirements, db.get_db(), lid, cfg)

    @app.route("/api/hedge/pipeline/<int:lid>/review", methods=["POST"])
    @auth.api_login_required
    def hedge_pipe_review(lid):
        return _pipe(hedge_pipeline.mark_reviewed, db.get_db(), lid,
                     actor=session.get("email", ""))

    @app.route("/api/hedge/pipeline/<int:lid>/finalize", methods=["POST"])
    @auth.api_login_required
    def hedge_pipe_finalize(lid):
        body = request.get_json(silent=True) or {}
        # Explicit approval is the contract: {"approved": true} or nothing happens.
        return _pipe(hedge_pipeline.finalize, db.get_db(), lid,
                     approved=body.get("approved") is True,
                     actor=session.get("email", ""), config=cfg)

    @app.route("/api/hedge/pipeline/<int:lid>/poll", methods=["POST"])
    @auth.api_login_required
    def hedge_pipe_poll(lid):
        return _pipe(hedge_pipeline.poll_one, db.get_db(), lid,
                     actor=session.get("email", ""), config=cfg)

    # ---- Hedge broker platform (submit -> market -> quote) ----
    def _hedge(fn, *a, **kw):
        """Run a Hedge call, turning failures into readable JSON responses."""
        try:
            return jsonify(fn(*a, **kw))
        except hedge_service.HedgeAuthRequired as e:
            return jsonify({"error": str(e), "signed_in": False}), 401
        except hedge_service.HedgeError as e:
            return jsonify({"error": str(e)}), e.status or 502

    @app.route("/api/hedge/status")
    @auth.api_login_required
    def hedge_status():
        signed_in = hedge_service.is_signed_in(cfg)
        out = {"signed_in": signed_in, "env": cfg.HEDGE_ENV,
               "auth_mode": hedge_service.auth_mode(cfg),
               "portal": hedge_service.env(cfg)["portal"]}
        if signed_in:
            try:
                out["broker"] = hedge_service.whoami(cfg)
            except hedge_service.HedgeError as e:
                out["signed_in"] = False
                out["error"] = str(e)
        return jsonify(out)

    # Signing in binds the whole agency's Hedge session — admin only.
    @app.route("/api/hedge/login/start", methods=["POST"])
    @auth.admin_required
    def hedge_login_start():
        return _hedge(hedge_service.start_device_login, cfg)

    @app.route("/api/hedge/login/poll", methods=["POST"])
    @auth.admin_required
    def hedge_login_poll():
        return _hedge(hedge_service.poll_device_login, cfg)

    @app.route("/api/hedge/logout", methods=["POST"])
    @auth.admin_required
    def hedge_logout():
        hedge_service.clear_token(cfg)
        return jsonify({"ok": True, "signed_in": False})

    @app.route("/api/hedge/appetite")
    @auth.api_login_required
    def hedge_appetite():
        return _hedge(hedge_service.appetite, request.args.to_dict(), cfg)

    @app.route("/api/hedge/appetite/preflight", methods=["POST"])
    @auth.api_login_required
    def hedge_appetite_preflight():
        """Pre-flight from form answers: derive the class/state via the mapping
        and ask which markets have appetite BEFORE a submission is created."""
        body = request.get_json(silent=True) or {}
        params = hedge_mapping.appetite_params(
            body.get("answers") or {}, overrides=body.get("overrides") or {})
        if not params:
            return jsonify({"error": "Add a description of operations first — "
                                     "appetite is matched on the class of business."}), 422
        resp = _hedge(hedge_service.appetite, params, cfg)
        if isinstance(resp, tuple):
            return resp
        data = resp.get_json()
        return jsonify({"params": params,
                        "results": (data or {}).get("results", data or [])})

    @app.route("/api/hedge/submissions")
    @auth.api_login_required
    def hedge_submissions():
        return _hedge(hedge_service.list_submissions, request.args.to_dict(), cfg)

    @app.route("/api/hedge/submissions/<sid>")
    @auth.api_login_required
    def hedge_submission(sid):
        return _hedge(hedge_service.get_submission, sid, cfg)

    @app.route("/api/hedge/preview-body", methods=["POST"])
    @auth.api_login_required
    def hedge_preview_body():
        """Show the CSR exactly what would be sent, before anything is sent."""
        body = request.get_json(silent=True) or {}
        payload = hedge_mapping.build_submission_body(
            body.get("answers") or {}, overrides=body.get("overrides") or {})
        return jsonify({
            "body": payload,
            "missing": hedge_mapping.missing_required(payload),
            "address_status": hedge_mapping.address_status(payload),
        })

    @app.route("/api/hedge/submissions", methods=["POST"])
    @auth.api_login_required
    def hedge_create_submission():
        body = request.get_json(silent=True) or {}
        payload = body.get("body")
        if payload is None:
            # Build it from form answers via the data-driven mapping.
            payload = hedge_mapping.build_submission_body(
                body.get("answers") or {}, overrides=body.get("overrides") or {})
        missing = hedge_mapping.missing_required(payload)
        if missing:
            return jsonify({"error": "Hedge needs these before submitting: "
                                     + ", ".join(missing), "missing": missing}), 422
        # Brokerage API-client credentials require attributing the producing
        # broker. Default to the WIT user who is creating the submission; an
        # explicit producer_email in the payload/overrides wins.
        if (hedge_service.auth_mode(cfg) == "client_credentials"
                and not payload.get("producer_email")):
            payload["producer_email"] = session.get("email")
        resp = _hedge(hedge_service.create_submission, payload, cfg)
        if isinstance(resp, tuple):
            return resp
        app.logger.info("hedge submission created answers=%s",
                        mask_pii(body.get("answers") or {}))
        return resp

    @app.route("/api/hedge/submissions/<sid>/documents", methods=["POST"])
    @auth.api_login_required
    def hedge_upload(sid):
        """Attach a PDF. Either an uploaded file, or — the useful path — a form
        from this app: fill the ACORD/loss run here and push it straight up."""
        label = request.form.get("name") or (request.get_json(silent=True) or {}).get("name")

        if "file" in request.files:
            f = request.files["file"]
            return _hedge(hedge_service.upload_document, sid, f.read(),
                          f.filename or "document.pdf", label=label, config=cfg)

        body = request.get_json(silent=True) or {}
        form_id = body.get("form_id")
        if form_id:
            ctx = _prepare_fill(int(form_id))
            if not isinstance(ctx, dict):
                return ctx
            schema = ctx["schema"]
            out = _output_path(int(form_id), "hedge")
            try:
                produce_pdf(schema, ctx["template"], out_path=out,
                            pdf_data=_fill_data(ctx), flatten=True,
                            pdftk_bin=cfg.PDFTK_BIN)
            except PdfFillError as e:
                return jsonify({"error": str(e)}), 500
            number = schema["_meta"]["acord_number"]
            resp = _hedge(hedge_service.upload_document, sid, out.read_bytes(),
                          f"ACORD_{number}.pdf", label=label or f"ACORD {number}",
                          config=cfg)
            if isinstance(resp, tuple):
                return resp
            _record_usage(ctx, int(form_id))
            log_submission(db.get_db(), user_id=auth.current_user_id(),
                           form_id=int(form_id), action="hedge_upload",
                           answers=ctx["answers"], output_path=str(out))
            return resp

        if body.get("loss_run"):
            fields = body["loss_run"]
            errs = loss_run.validate(fields)
            if errs:
                return jsonify({"error": "validation failed", "fields": errs}), 422
            pdf_bytes = loss_run.build_pdf(fields)
            return _hedge(hedge_service.upload_document, sid, pdf_bytes,
                          loss_run.suggested_filename(fields),
                          label=label or "Loss run request", config=cfg)

        return jsonify({"error": "provide a file, a form_id, or loss_run fields"}), 400

    @app.route("/api/hedge/submissions/<sid>/requirements")
    @auth.api_login_required
    def hedge_requirements(sid):
        return _hedge(hedge_service.requirements, sid, cfg)

    @app.route("/api/hedge/submissions/<sid>/finalize", methods=["POST"])
    @auth.api_login_required
    def hedge_finalize(sid):
        return jsonify({"error": "Use the local pipeline review and approval to release to market."}), 409

    @app.route("/api/hedge/submissions/<sid>/quotes")
    @auth.api_login_required
    def hedge_quotes(sid):
        return _hedge(hedge_service.quote_sessions, sid, cfg)

    @app.route("/api/hedge/submissions/<sid>/quotes/<session_id>/answers", methods=["POST"])
    @auth.api_login_required
    def hedge_answer(sid, session_id):
        payload = (request.get_json(silent=True) or {}).get("answers") or {}
        return _hedge(hedge_service.answer_quote, sid, session_id, payload, cfg)

    @app.route("/api/hedge/submissions/<sid>/quotes/<session_id>/close", methods=["POST"])
    @auth.api_login_required
    def hedge_close_quote(sid, session_id):
        return _hedge(hedge_service.close_quote, sid, session_id, cfg)

    @app.route("/api/hedge/submissions/<sid>/documents", methods=["GET"])
    @auth.api_login_required
    def hedge_finalized_docs(sid):
        return _hedge(hedge_service.finalized_documents, sid, cfg)

    @app.route("/api/hedge/documents/<document_id>/pdf")
    @auth.api_login_required
    def hedge_download_doc(document_id):
        try:
            content, name = hedge_service.download_finalized(document_id, cfg)
        except hedge_service.HedgeAuthRequired as e:
            return jsonify({"error": str(e), "signed_in": False}), 401
        except hedge_service.HedgeError as e:
            return jsonify({"error": str(e)}), e.status or 502
        return send_file(BytesIO(content), mimetype="application/pdf",
                         as_attachment=True, download_name=name)

    @app.route("/api/hedge/policies")
    @auth.api_login_required
    def hedge_policies():
        return _hedge(hedge_service.list_policies, cfg)

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
        # Server-side email is intentionally disabled in Phase 1 (local-download
        # model): generate a flattened PDF, download it, and let the user attach
        # it from their own Gmail/Outlook account. This endpoint is a
        # download-compatible alias so stale UI/API clients keep working.
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
