"""Forms-host handoff: no customer processing, sessions, database or outputs."""
from __future__ import annotations

import html
import os
from urllib.parse import urlsplit
from flask import Flask, jsonify, request


def create_app(config=None):
    app = Flask(__name__, static_folder=None)
    origin = os.environ.get("WITNEXT_ORIGIN", "").rstrip("/")
    if config is not None:
        origin = getattr(config, "WITNEXT_ORIGIN", origin).rstrip("/")
    parsed = urlsplit(origin)
    if origin and (parsed.scheme != "https" or not parsed.netloc or parsed.username
                   or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("WITNEXT_ORIGIN must be an HTTPS origin without credentials or a path")

    @app.before_request
    def reject_writes():
        if request.method not in ("GET", "HEAD"):
            # Never read, parse, log, forward, or retain the incoming body.
            return jsonify(error="Customer forms have moved to WiTNext. Reopen the form there."), 410
        return None

    @app.after_request
    def private_response(response):
        response.headers.update({
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        })
        return response

    @app.get("/health")
    def health():
        return jsonify(status="ok", mode="witnext-handoff", customer_storage=False)

    @app.get("/api/<path:unused>")
    def retired_api(unused):
        return jsonify(error="This API is retired. Use the WiTNext forms workspace."), 410

    @app.get("/", defaults={"unused": ""})
    @app.get("/<path:unused>")
    def handoff(unused):
        link = (f'<p><a href="{html.escape(origin, quote=True)}/forms">Open forms in WiTNext</a></p>'
                if origin else '<p>Open your WiTNext customer workspace and choose Forms.</p>')
        return ('<!doctype html><html lang="en"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>Forms have moved to WiTNext</title><main>'
                '<h1>Forms have moved to WiTNext</h1>'
                '<p>Start, save, reopen, and sign forms from the customer workspace.</p>'
                + link + '</main></html>')

    return app


app = create_app()
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8097)
