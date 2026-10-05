# WiT Forms

WiT Forms stays at **forms.weinsurethings.com**. Start a package, edit answers,
review and preview PDFs, save/reopen, collect signatures and use Hedge from the
Forms interface. **WiTNext owns customer storage and authentication.**

The Forms host serves a small branded portal. It embeds the dedicated
`/forms-app/` surface supplied by the paired WiTNext release; it does not send
users into the CRM customer workspace. The embedded application talks directly
to WiTNext's authenticated APIs. No answers, PDFs, customer identifiers or access
tokens pass through the Forms host or its readiness messages.

## Configure the connection

Deploy the paired WiTNext Forms release first. Its web server permits the exact
Forms origin to embed **only** `/forms-app/`; the CRM stays unembeddable by Forms.
Set `WITNEXT_ORIGIN` in `/etc/wit-forms/witforms.env` to the actual HTTPS WiTNext
origin, without a path. `.env.example` documents the only portal setting.

Use `witforms.service` and `nginx.conf.example` after checking deployment paths.
The service is read-only, suppresses request diagnostics and core dumps, and has
no database, session or customer output directory. Local writes return 410
without reading their bodies. `/healthz` reports `forms-portal`, `configured`
and `customer_storage: false` without exposing data.

First-time sign-in may need **Connect to WiTNext**, which opens authentication
in another tab. Return to Forms and select **Reload forms**. This is not a
redirect or a move of the Forms workflow. A missing connection is shown as a
setup error, never silently routed to another environment.

See [deployment and data transition](docs/witnext-cutover.md) for the frame,
Access and historical-data checks required before deployment.

## Templates and field mapping

The private renderer runs inside WiTNext, using genuinely blank licensed ACORD
PDFs and explicit template/schema/mapping approval hashes. Existing schemas
alone do not prove field completeness or certify an ACORD edition. Rendering
stays blocked until each template and omitted field has been reviewed. Never
use a completed customer application as a blank template.

## Verification

```sh
python -m pytest -q
node --test tests/portal.test.mjs
```

The paired WiTNext tests cover save/reopen, revision conflicts, account access,
signatures, Hedge and the standalone Forms interface. Portal tests check origin
validation, strict message sources, no sensitive message payloads, no file or
session creation, and rejection of legacy APIs and assets.

Historical application code in `tests/legacy_app.py` is a regression fixture,
never a deployment entry point. [Historical documentation](docs/legacy-app.md)
is retained for interpreting old records. This source change does not migrate
or delete existing customer data, logs or backups.
