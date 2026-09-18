# WIT Forms

Internal web app that fills **licensed ACORD insurance PDFs** for We Insure
Things (≤5 users). Search a form → answer a short set of questions → preview the
filled PDF → **email / print / download** it. Everything is **schema-driven**:
adding a form means dropping in a licensed template and writing one JSON field
map — no application-code changes.

Built to match WIT's stack: **Flask + SQLite + Google SSO**, deployed on Oracle
Cloud Ubuntu behind **nginx** via **systemd** at `forms.weinsurethings.com`
(port `8097`).

> The app does **not** generate ACORD forms. It only fills the official licensed
> ACORD PDF templates Logan supplies. Templates are never committed to git.

---

## How it works (the schema-driven model)

Each form has one JSON schema in `schemas/` (e.g. `schemas/acord_25.json`). The
schema does three jobs at once:

1. Tells the **frontend** what to render (label, type, grouping, validation,
   priority core/common/rare).
2. Tells the **backend** which PDF AcroForm field(s) each answer writes to.
3. Drives the "which fields are truly necessary" analytics via `field_usage`.

The frontend never hardcodes a form layout — it renders whatever the schema
says. See `schemas/acord_25.json` for the complete, verified pattern every other
form must follow, and the **Schema contract** section below.

### The integration contract (front end resolves logic, backend stays dumb)

Per TEST-WIRE-UP §0: the SPA resolves every logic construct (`radio_group`,
`insurer_ref`, `yn_code`, `optional_block`, `show_if`, the 125
`sections_attached` hub) and POSTs the backend a flat `{ relative_pdf_field:
value }` map (authoritative for filling). The backend builds the full AcroForm
name and fills:

```
full = field_name_prefix + (page token if _meta.page_token_pattern) + relative_pdf_field
```

`fill_pdf._resolve_pdf_data` then maps that onto the template's real field names,
so editions that nest fields by page (e.g. `P2[0].…`) resolve generically.
`preview|download|email` accept `{ "fields": {…} }` plus, optionally,
`{ "answers": {…} }` (keyed) — when present the server still runs validation,
field-usage analytics, and the audit snapshot. The SPA sends both; a flat-map-
only POST also works (the contract's acceptance test). See
`pdf_fill.build_full_field_name` / `flat_map_to_pdf_data`.

**Final actions:** Preview, Download, Print, and **Use my email** (local
download — attach from your own client; server-side email is intentionally
disabled in Phase 1). Every download/print writes an audit row.

### The verified PDF pipeline (do not redesign)

Real ACORD PDFs are **XFA + owner-password encrypted**. Filling only the
AcroForm layer makes values invisible in Adobe. The fix, run once per template:

```bash
python tools/prep_template.py templates/acord/ACORD_25_2016-03.pdf
# -> templates/acord/ACORD_25_2016-03_clean.pdf  (Form: AcroForm, Encrypted: no)
```

Then per fill (`pdf_fill.py`): pypdf fills the clean template’s AcroForm fields
(`update_page_form_field_values(..., auto_regenerate=False)`), and **pdftk
flattens** the result before any email/print/download so the output is
non-editable and renders identically everywhere.

Verified facts (ACORD 25): checkbox on=`"1"` off=`"Off"`; ADDL INSD / SUBR WVD
are **text** `"Y"`/`"N"`; field names are `F[0].P1[0].<relative>`.

---

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
sudo apt-get install pdftk            # system dependency (not pip-installable)

cp .env.example .env                  # fill in what you have; placeholders are OK
python db.py                          # initialise the SQLite schema
python app.py                         # http://127.0.0.1:8097
```

Without Google OAuth creds the login button returns 503 — set
`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` to sign in. All `/api/*` routes are
gated behind a `@weinsurethings.com` session.

### Adding the licensed templates

Templates are **gitignored** (licensed IP + PII). Drop the licensed fillable PDF
into `templates/acord/` and prep it:

```bash
python tools/prep_template.py templates/acord/ACORD_25_2016-03.pdf
python tools/dump_fields.py templates/acord/ACORD_25_2016-03_clean.pdf  # verify field names
```

The catalog expects the clean copy at `templates/acord/ACORD_<number>_clean.pdf`
(see `forms_catalog._meta_to_catalog_row`); name it accordingly or adjust
`template_path` in the `forms` row.

---

## Loss run request — `/loss-run`

One button that produces a **WIT-letterhead PDF** asking a carrier for an
insured's claims history. Not an ACORD form — there's no licensed template to
fill, so `loss_run.py` composes the letter with reportlab and the user
downloads the finished PDF.

Fields: insurance company name*, policy number*, insured name*, insured address
(street/city/state/ZIP), loss run period from*/to* (defaulted to the last five
years), requested by, agency phone/email, and free-text notes. `*` = required,
enforced on both sides; dates must be MM/DD/YYYY.

The letter body is a **deterministic built-in template**, not model-generated —
it's boilerplate with the operator's values dropped in, and policy numbers and
date ranges must be reproduced exactly. It works with no API key and no network.
It asks for open and closed claims with paid/reserved amounts and requests a
no-loss letter if there were none, and states WIT is agent of record.

Output has no AcroForm layer (text is drawn, not filled), so it is already
flat — nothing for a recipient to edit. Every generation writes a `submissions`
audit row with `action='loss_run'` and `form_id=0` (the sentinel for non-ACORD
output).

---

## Hedge broker platform — `/hedge`

Push WIT Forms output straight into a **Hedge** submission and follow it through
to quotes, without leaving the app. Hedge is Taven Tech's broker platform.

**Why direct HTTP:** the endpoint surface and OAuth mechanics are ported from
the MIT-licensed [`taventech/hedge-cli`](https://github.com/taventech/hedge-cli)
source — so no Node runtime is needed on the box and it fits the existing Flask
app. `hedge_service.py` documents every endpoint it uses.

**Auth — two modes:**

* **Machine credentials**: set `HEDGE_CLIENT_ID` and `HEDGE_CLIENT_SECRET`.
  The server discovers Hedge’s token endpoint, exchanges the pair using
  `client_credentials`, caches short-lived bearer tokens in memory and renews
  them before expiry. Static `X-Api-Key` headers are not supported by the
  current contract. Machine submissions require an active Hedge portal user’s
  `producer_email`; website drafts acquire the signed-in agent’s attribution
  on their first send. The agency needs Hedge-enabled `broker_submit` access.
* **OAuth 2.1 device sign-in** (when no key is set): RFC 8414 discovery,
  RFC 7591 dynamic client registration, RFC 8628 device grant (the browser only
  ever sees the user code and URL; the `device_code` stays server-side in a
  file so it survives across gunicorn workers). The refresh token lives in
  `DATA_DIR` at `0600` and is never sent to the client. Signing in binds the
  whole agency's session, so it is **admin-only**.

**The workflow** (`/hedge`): create a submission → attach documents → finalize
to market it → read quotes back. The valuable step is document attachment: pick
a filled ACORD and it is **filled, flattened and uploaded in one call** — no
download/re-upload round trip. Loss run requests can be attached the same way.

**Mapping stays data-driven.** `mappings/hedge_submission.json` lists *candidate*
source keys per Hedge target (first non-empty wins), so one map serves ACORD 125,
25 and others whose auto-generated key names differ — adding a form means editing
JSON, never an if-branch. Two Hedge quirks are enforced up front rather than
discovered as a 422: `mailing_address` is all-or-nothing (line1+city+state+zip),
and `primary_state` is only sent when there is no complete address. `POST
/api/hedge/preview-body` shows the CSR exactly what would be sent, and overrides
from the review screen are merged **before** normalising so filling in a missing
ZIP actually completes the address.

```bash
HEDGE_ENV=staging   # start here; switch to prod when you're ready
```

> Not yet exercised with live brokerage credentials. Automated tests use mocked Hedge HTTP. Sign in on
> **staging** first and walk one submission end to end before using prod.


### Website intake and signed webhooks

The WordPress theme in `Yothisislogan/catdog` adds `/submit-a-risk/` for commercial
and specialty shoppers. Its server sends an HMAC-signed request to
`POST /integrations/wit/intake`. Valid, consented requests are atomically saved
as local drafts and appear in `/hedge`, labeled **Website**. Repeated requests
with the same reference return the original receipt; changed content conflicts.
No consumer endpoint calls Hedge or exposes the staff queue.

Hedge sends Svix-signed events to `POST /integrations/hedge/events`. Signatures
cover raw bytes and expire after five minutes. Delivery IDs and the combination
of submission ID, event ID and event type are durably deduplicated in SQLite.
Market events appear in the activity panel; they do not overwrite the overall
submission status. The existing poller / Refresh status supplies that status.
Unknown submissions' events are retained and linked if a create response arrives
later. No network calls happen during webhook acknowledgement.

See [HEDGE-SETUP.md](docs/HEDGE-SETUP.md) for credentials, WordPress configuration,
webhook registration, rollout, recovery, and the exact bridge contract.

### Phase 1 — public appetite & checklist (`/appetite`, no Hedge account)

Hedge publishes machine-readable feeds (`appetite.json`,
`submission-requirements.json`, `coverages.json`, `class-coverage.json`,
`commercial-insurance-submission-checklist.json`) plus a `changes.json`
staleness marker. `/appetite` serves two panels from them:

* **Published appetite** — class (autocomplete) + state → the published entry
  **verbatim**. The panel is labeled *"Directional — confirmed only after
  Hedge review"* and nothing ever rewrites a verdict: "review case by case"
  renders as exactly that, and no matching entry renders as "not published",
  never as "no".
* **Submission-prep checklist** — the published checklist cross-referenced
  against what WIT Forms already generated for a client (from the audit log),
  shown as a concrete have/missing gap list.

**Caching:** feeds are cached in SQLite for `HEDGE_FEED_TTL` (default 24h).
On expiry, `changes.json` is probed first and a feed is only refetched when its
marker moved. If Hedge is unreachable the cached copy is served **stale with a
visible warning**, then the committed fixture, then a "not captured yet"
message — a Hedge outage never breaks WIT Forms.

**Feed schemas are not guessed.** The build environment cannot reach
`hedgespecialty.com`, so field names live in `hedge/feed_map.json` and ship
**unconfirmed** — the UI falls back to rendering the raw feed verbatim with a
text filter until the ops pass below confirms them.

### Phase 2 — the submission pipeline (built, dark behind config)

`/hedge` runs a local-first pipeline (`hedge/api_client.py`):

```
draft (local only) → send → uploading → needs_requirements
      → awaiting_approval → finalized      (local states, hedge/states.py)
```

* **Nothing is sent until "Send to Hedge"** — and every live write (create,
  upload, finalize) is refused with a clear message unless `HEDGE_LIVE=1`.
* **Idempotent by construction:** a UUID `Idempotency-Key` is stored on the
  local row *before* the first send and reused verbatim on any retry; the
  column is UNIQUE, and attempts older than 23 hours are blocked for manual reconciliation before Hedge’s 24-hour replay window expires.
* **Finalize is never automatic.** The review screen shows the exact payload,
  the attached documents (with hashes) and outstanding requirements; the
  finalize button only arms after an explicit approval checkbox, the API
  requires `{"approved": true}` from the `awaiting_approval` state, and who
  approved + when is recorded.
* **Status discipline:** Hedge's `state`/`status_label` are stored and shown
  **verbatim** in separate columns — "submitted" is never displayed as quoted
  or bound. A background poller (only when `HEDGE_LIVE=1`) records every
  observed remote change in `hedge_events`, the append-only audit log that
  also carries a sha256 payload hash + Hedge's confirmation id for every
  external write.
* **Credentials at rest:** with `HEDGE_CRED_KEY` set (a Fernet key), OAuth
  tokens live encrypted in the `hedge_credentials` table — a raw read of the
  DB file shows only ciphertext. Unset (dev), tokens fall back to a `0600`
  JSON file in `DATA_DIR`. Generate a key:

  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```

* **URL hygiene:** insured/producer/agency data never goes into query strings —
  the transport rejects PII-looking query params outright; only documented
  filter params (`q`, `state`, `lob`) are used.

**Env vars:** `HEDGE_LIVE` (phase gate, default off), `HEDGE_CRED_KEY`,
`HEDGE_FEED_TTL`, `HEDGE_POLL_INTERVAL`, plus the auth block above — see
`.env.example`.

### Ops checklist — the day credentials arrive

1. On a machine with internet access, capture the public feeds + API spec:
   `python tools/fetch_hedge_feeds.py` (writes `hedge/fixtures/`; commit them).
2. Cross-check `hedge_service.py`'s endpoint docstring against the captured
   `openapi.json`; fix any drift **before** going live.
3. Open the captured feeds, fill in the real field names in
   `hedge/feed_map.json`, and set its `_meta.confirmed` to `true` — the
   appetite autocomplete + gap list light up; until then the UI shows raw
   feeds, which is correct.
4. Set `HEDGE_CRED_KEY` (command above) in the env file, restart, then either
   set `HEDGE_CLIENT_ID` / `HEDGE_CLIENT_SECRET` or device-sign-in at `/hedge` (admin).
5. Stay on `HEDGE_ENV=staging`, set `HEDGE_LIVE=1`, walk one submission end to
   end: draft → send → upload an ACORD → requirements → review → finalize →
   watch the status. Check `hedge_events` afterwards — every step should be
   there with hashes.
6. Only then switch `HEDGE_ENV=prod`.

---

## Proposal generator (Gemini) — `/proposal`

A separate tool from the ACORD filler, sharing the same auth/CSRF/deploy: fill
in client + coverage details, and Gemini returns a **client-ready HTML5
proposal in WIT brand colors**, shown as code with a **one-click Copy**
button (plus a sandboxed Preview toggle).

Two guarantees are enforced in `gemini_service.py`, twice over — once in the
system instruction, once on the response:

1. **Always HTML5** — one complete self-contained document (inline CSS, no
   external assets), no markdown fences, no commentary. `clean_html()` strips
   anything the model wraps around it.
2. **Always WIT-branded** — the palette is injected into the instruction,
   including the contrast rule (bright `#00AEEF` for accents only; deep
   `#007EAE` for headings/links/fills).

It also refuses to invent insurance facts: anything not supplied comes back as
`TBD`, and every proposal carries a "subject to carrier approval / policy
language governs" disclaimer.

```bash
GEMINI_API_KEY=...            # server-side only, never sent to the browser
GEMINI_MODEL=gemini-2.5-flash # override if the model ID changes
```

Endpoint: `POST /api/proposal/generate {fields:{...}}` → `{html, model}`.
Unconfigured returns 503 with a readable message; upstream failures return 502.
Request shape verified against the Gemini v1beta discovery document
(`systemInstruction` + `contents` + `generationConfig`, `x-goog-api-key` header).

---

## Tests

```bash
python -m pytest tests/ -v
```

The six brief acceptance tests live in `tests/test_acceptance.py`. Test 1
(full fill → flatten → text extraction) **skips** automatically unless a licensed
`ACORD_25_clean.pdf` and `pdftk` are present, since templates aren’t committed.

---

## Deployment (Oracle Cloud Ubuntu)

```bash
sudo mkdir -p /opt/wit-forms && sudo rsync -a ./ /opt/wit-forms/   # minus gitignored
cd /opt/wit-forms && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo apt-get install pdftk

# Secrets (chmod 600, owned by the witforms user — NOT in git):
sudo mkdir -p /etc/wit-forms && sudo cp .env.example /etc/wit-forms/witforms.env
sudo chmod 600 /etc/wit-forms/witforms.env   # then edit with real values

sudo cp witforms.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now witforms

# nginx + TLS:
sudo cp nginx.conf.example /etc/nginx/sites-available/forms.weinsurethings.com
sudo ln -s /etc/nginx/sites-available/forms.weinsurethings.com /etc/nginx/sites-enabled/
sudo certbot --nginx -d forms.weinsurethings.com
sudo nginx -t && sudo systemctl reload nginx
```

Health check: `GET /healthz`.

---

## Schema contract

`_meta.field_name_prefix` is prepended to every `pdf_field` before filling.

`insurers` is the A–F reference table: the UI renders filled rows as a dropdown
and writes the chosen **letter** to each coverage block’s `insurer_ref` field.

`sections[].fields[]`:

| key | meaning |
|---|---|
| `key` | internal answer key |
| `label` | UI label |
| `type` | `text` `textarea` `number` `currency` `date` `phone` `email` `state` `select` `checkbox` `radio_group` `insurer_ref` `yn_code` |
| `priority` | `core` / `common` / `rare` (rare collapses under "More fields") |
| `required` | client + server enforced (respecting block inclusion) |
| `pdf_field` | relative AcroForm name (radio_group uses per-option `pdf_field`) |
| `show_if` | optional: reveal when the named answer is truthy |

`optional_block` + `include_toggle`: when a block is excluded, **all** its
`pdf_field`s are skipped (left blank); at least one coverage block is required.

`logic`: radio-group exclusivity (one `"1"`, rest `"Off"`), reveals, literal
`Y`/`N` for `yn_code`, and "≥1 coverage block required."

New schemas are validated at load by `schema_validator.py` — a malformed schema
fails loudly at startup, not at fill time.

---

## Hard rules (enforced in code)

1. Never recreate/scrape ACORD forms — fill licensed templates only; missing
   template → stop and report (`tools/prep_template.py`, `_prepare_fill`).
2. `templates/` and `data/` are gitignored (licensed IP + PII).
3. Always flatten before email/print/download (`pdf_fill.flatten_pdf`).
4. Owner CC enforced server-side on every email (`email_service.send_form_email`).
5. Domain-restricted auth (`auth.email_allowed`).
6. No PII in plaintext logs — masked in debug output (`submissions.mask_pii`).
7. Schema-driven always — no form-specific branches in code.

---

## Build status

| Milestone | Status |
|---|---|
| M0 Scaffold & deploy skeleton | ✅ |
| M1 Google SSO + domain restriction | ✅ (needs OAuth creds from Logan) |
| M2 PDF fill + flatten pipeline | ✅ (verified recipe; needs licensed template to run live) |
| M3 Catalog + search + schema render | ✅ |
| M4 Preview + download/print/email + audit log | ✅ (email needs transport creds) |
| M5 Agency/client profiles + prefill | ✅ |
| M6 Author remaining forms | 🟡 in progress — generator + 4 schemas in repo (25 verified; 128, 131, 135 NC drafts). 8 more draft via the generator once their PDFs land |
| M7 Field-usage tracking | ✅ |
| Phase 2 (NowCerts, drafts, admin re-tag) | hooks stubbed only |

### M6 — authoring the other forms

`tools/build_schema.py` auto-drafts a schema straight from a fillable ACORD PDF
(field names + `FieldNameAlt` tooltips + checkbox export values), matching the
`acord_25.json` shape:

```bash
python tools/build_schema.py templates/acord/ACORD_140_clean.pdf 140 2016/03 > schemas/acord_140.json
```

Drafts are intentionally partial: labels/types/sections/PDF-field-names are
accurate, but **priorities, required flags, and cross-field logic**
(insurer-letter refs, optional coverage blocks, radio groups, Y/N codes) need a
human pass — see `SCHEMA-STATUS.md` for the per-form checklist and current
status. Draft schemas omit `_meta.title`; the catalog supplies a display title
(`forms_catalog.derive_title` / `FORM_TITLES`). Checkbox export values are read
from each field's `on_value` (default `"1"`).

Three forms beyond the original nine were supplied (128 Garage, 131 Umbrella,
135 NC assigned-risk WC) and are included as active drafts — flagged for Logan's
keep/defer call in `SCHEMA-STATUS.md`. The duplicate `Acord_130_WC_page_2.pdf`
is ignored (the main 130 already contains all pages).

### Waiting on Logan
- `OWNER_CC_EMAIL`, email transport (SMTP creds or SendGrid key)
- Google OAuth client ID/secret for `forms.weinsurethings.com`
- The licensed fillable PDFs dropped into `templates/acord/` (gitignored). The
  schemas reference real field names; to fill live, prep each template
  (`tools/prep_template.py`) and — for forms without a schema yet — draft one
  with `tools/build_schema.py`, then hand-tune per `SCHEMA-STATUS.md`.
