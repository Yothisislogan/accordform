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
