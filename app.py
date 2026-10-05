"""WiT Forms portal: customer traffic goes directly to authenticated WiTNext."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit
from flask import Flask, jsonify, render_template, request, send_from_directory, abort


def create_app(config=None):
    app = Flask(__name__, static_folder=None, template_folder="portal_templates")
    origin = os.environ.get("WITNEXT_ORIGIN", "").rstrip("/")
    if config is not None:
        origin = getattr(config, "WITNEXT_ORIGIN", origin).rstrip("/")
    parsed = urlsplit(origin)
    if origin and (parsed.scheme != "https" or not parsed.netloc or parsed.username
                   or parsed.password or parsed.path or parsed.query or parsed.fragment
                   or not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?", origin)
                   or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
        raise ValueError("WITNEXT_ORIGIN must be an HTTPS origin without credentials or a path")
    if origin:
        # MessageEvent.origin uses a lowercase hostname and omits the HTTPS
        # default port. Match that browser serialization for exact comparison.
        port = f":{parsed.port}" if parsed.port not in (None, 443) else ""
        origin = f"https://{parsed.hostname}{port}"

    @app.before_request
    def reject_writes():
        if request.method not in ("GET", "HEAD"):
            # Never read, parse, log, forward, or retain the incoming body.
            return jsonify(error="Open WiT Forms to edit. Saving connects directly to WiTNext."), 410
        return None

    @app.after_request
    def private_response(response):
        response.headers.update({
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                "connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
                "frame-src " + (origin or "'none'")
            ),
        })
        return response

    @app.get("/health")
    @app.get("/healthz")
    def health():
        return jsonify(status="ok", mode="forms-portal", configured=bool(origin), customer_storage=False)

    @app.get("/portal/<path:asset>")
    def portal_asset(asset):
        if asset not in {"portal.css", "portal.js", "assets/wit-forms-logo-1b-light.png", "assets/favicon.svg"}:
            abort(404)
        return send_from_directory(os.path.join(app.root_path, "static"), asset)

    @app.get("/static/<path:unused>")
    def legacy_asset(unused):
        # Never revive the old application, PDF outputs or customer-bearing paths.
        abort(404)

    @app.get("/api/<path:unused>")
    def retired_api(unused):
        return jsonify(error="Local data endpoints are disabled. Open WiT Forms to continue."), 410

    @app.get("/", defaults={"unused": ""})
    @app.get("/<path:unused>")
    def portal(unused):
        # No incoming path, query, cookie or header is reflected into the frame.
        return render_template("forms_portal.html", witnext_origin=origin)

    return app


app = create_app()
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8097)
