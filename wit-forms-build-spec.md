# WIT Forms — ACORD Form Prefill Web App
### Build Specification for a Coding AI Agent

**Owner:** Logan / We Insure Things (WIT)
**Working name:** WIT Forms
**Suggested subdomain:** `forms.weinsurethings.com`
**Status:** v1 build spec

---

## 0. One-paragraph summary

A small internal web app (≤5 users) that lets WIT staff search for an ACORD insurance form, answer a simple set of questions, preview the filled PDF in the browser, and then **email**, **print**, or **download** it. The app does *not* generate ACORD forms from scratch — it fills the **official licensed ACORD PDF templates** (supplied by WIT) using a per-form **field map**. Everything is schema-driven so adding a new form means dropping in a PDF and writing one JSON field map, not changing application code. Built to match WIT's existing stack: Flask + SQLite + Google SSO, deployed on Oracle Cloud Ubuntu behind nginx via systemd.

---

## 1. Goals & non-goals

### Goals
- Dead-simple UX: a search box → pick a form → answer questions → preview → email/print/download.
- Prefill repeated data (agency info, client info) so users don't retype the same fields on every form.
- Keep an audit record of what was filled and sent.
- Make it trivial to add new forms over time (schema/JSON, no redeploy of logic).
- Learn over time which fields are actually used vs. ignored, to simplify forms.
- WIT-branded throughout.

### Non-goals (v1)
- Not a full AMS. It does not replace NowCerts.
- No e-signature in v1 (flag a hook for later).
- No public/external access — internal only.
- Not generating ACORD layouts from scratch (we fill licensed templates only).

---

## 2. ⚠️ Critical dependency: the ACORD PDF templates

The app fills **fillable AcroForm PDFs**. These must be the **official licensed ACORD form editions** that WIT already has rights to (export from NowCerts or WIT's ACORD license). The coding agent must NOT recreate or scrape ACORD forms.

Required handling:
- Templates live in a versioned folder, e.g. `templates/acord/ACORD_25_2016-03.pdf` (filename encodes form number + edition date).
- Each template is registered in the DB with its edition/version.
- ACORD forms revise periodically — the data model tracks edition so a future edition can be added side-by-side without breaking old submissions.
- **First task of the build:** write a small utility (`tools/dump_fields.py`) that extracts and prints every AcroForm field name from a given PDF. Field maps are written against these exact names.

---

## 3. Scope — phased

### Phase 1 (MVP)
- Google SSO (restricted to `@weinsurethings.com`).
- Form catalog + search box.
- Schema-driven dynamic form rendering for a starting set of ~8–10 forms (see §13).
- Agency profile + client profile prefill ("answer once").
- Fill PDF → in-browser preview → **download** + **print**.
- **Email** with auto-CC to the WIT owner address; attaches flattened PDF.
- Submission audit log.

### Phase 2
- NowCerts customer lookup → prefill named insured / address / policy data (reuse existing Sales Tracker NowCerts integration).
- Field-usage analytics + admin tagging of fields (core/common/rare, required toggle).
- Draft save/resume.
- More forms.

### Phase 3 (later, optional)
- E-signature (reuse pattern from WIT quote-checkout eSignature plugin).
- Multi-edition handling UI.
- Bulk / batch fill.

---

## 4. Architecture & stack

| Layer | Choice | Notes |
|---|---|---|
| Frontend | Single-page app, vanilla JS (or light framework), WIT-branded | Matches Sales Tracker SPA pattern; keep it simple |
| Backend | Python + Flask | Same as WIT Connect / Sales Tracker |
| DB | SQLite | Same as Sales Tracker |
| Auth | Google OAuth 2.0, domain-restricted | Same pattern as Sales Tracker |
| PDF fill | See §7 (pdftk primary, with fallbacks) | Consult the `pdf` skill before coding this step |
| Email | SMTP / transactional provider (Twilio SendGrid available) | CC owner, attach PDF |
| PDF preview | PDF.js (or native iframe embed of the generated PDF) | Render before action |
| Deploy | Oracle Cloud Ubuntu, nginx reverse proxy, systemd service | New subdomain |
| Port | Suggest `8097` (8095/8096 already used by WIT Connect) | Avoid collision |

Keep the backend as a single Flask app with a clear module split:

```
wit-forms/
  app.py                 # Flask app factory, route registration
  auth.py                # Google SSO, session handling, domain restriction
  db.py                  # SQLite connection, schema init, migrations
  forms_catalog.py       # search + list + get form schema
  pdf_fill.py            # fill + flatten pipeline
  email_service.py       # send with CC + attachment
  profiles.py            # agency/client reusable profiles
  submissions.py         # audit log + field-usage tracking
  nowcerts.py            # (phase 2) customer lookup prefill
  tools/
    dump_fields.py       # extract AcroForm field names from a PDF
  templates/acord/       # licensed ACORD PDFs (gitignored / not committed)
  schemas/               # one JSON field-map per form (see §6)
  static/                # SPA: index.html, app.js, styles.css, wit assets
  data/witforms.db       # SQLite
  requirements.txt
  witforms.service       # systemd unit
  nginx.conf.example
```

---

## 5. Data model (SQLite)

```sql
-- Staff users (auto-provisioned on first SSO login if domain matches)
CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  email         TEXT UNIQUE NOT NULL,
  name          TEXT,
  role          TEXT DEFAULT 'user',   -- 'user' | 'admin'
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Form catalog
CREATE TABLE forms (
  id            INTEGER PRIMARY KEY,
  acord_number  TEXT NOT NULL,         -- e.g. '25'
  edition       TEXT,                  -- e.g. '2016-03'
  title         TEXT NOT NULL,         -- 'Certificate of Liability Insurance'
  description   TEXT,
  category      TEXT,                  -- 'Certificate' | 'Commercial' | 'Personal' | 'Change' ...
  keywords      TEXT,                  -- searchable text: 'auto liability cert COI'
  template_path TEXT NOT NULL,         -- templates/acord/ACORD_25_2016-03.pdf
  schema_path   TEXT NOT NULL,         -- schemas/acord_25.json
  active        INTEGER DEFAULT 1,
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Reusable answer sets ("answer once")
CREATE TABLE profiles (
  id            INTEGER PRIMARY KEY,
  type          TEXT NOT NULL,         -- 'agency' | 'client'
  name          TEXT NOT NULL,         -- 'WIT Norfolk' or 'Acme LLC'
  data_json     TEXT NOT NULL,         -- { field_key: value, ... }
  owner_user_id INTEGER,
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- In-progress drafts (phase 2)
CREATE TABLE drafts (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL,
  form_id       INTEGER NOT NULL,
  profile_id    INTEGER,
  answers_json  TEXT NOT NULL,
  status        TEXT DEFAULT 'draft',
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Audit log of every produced form
CREATE TABLE submissions (
  id              INTEGER PRIMARY KEY,
  user_id         INTEGER NOT NULL,
  form_id         INTEGER NOT NULL,
  action          TEXT NOT NULL,       -- 'email' | 'download' | 'print'
  recipient_emails TEXT,               -- comma-separated, email only
  cc_emails       TEXT,
  output_path     TEXT,                -- stored flattened PDF (retention policy applies)
  answers_snapshot TEXT,               -- JSON snapshot of answers used
  created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Field usage analytics → drives "which fields are truly necessary"
CREATE TABLE field_usage (
  id          INTEGER PRIMARY KEY,
  form_id     INTEGER NOT NULL,
  field_key   TEXT NOT NULL,
  times_filled INTEGER DEFAULT 0,
  times_skipped INTEGER DEFAULT 0,
  last_used   TEXT,
  UNIQUE(form_id, field_key)
);
```

---

## 6. The core concept: schema-driven field maps

Each form has one JSON schema in `schemas/`. It does three jobs at once:
1. Tells the frontend what questions to render (label, type, grouping, validation).
2. Tells the backend which **PDF AcroForm field(s)** each answer writes to.
3. Tags each field's **priority** (`core` / `common` / `rare`) and whether it's `required`, so the UX can show the essentials first and collapse the rest — this is the mechanism behind the "which fields are truly necessary" goal.

The frontend NEVER hardcodes form layouts — it renders whatever the schema says.

### Schema shape (abbreviated example — ACORD 25)

```json
{
  "acord_number": "25",
  "edition": "2016-03",
  "title": "Certificate of Liability Insurance",
  "template_path": "templates/acord/ACORD_25_2016-03.pdf",
  "sections": [
    {
      "id": "producer",
      "label": "Agency / Producer",
      "prefill_from": "agency",
      "fields": [
        {
          "key": "producer_name",
          "label": "Agency name",
          "type": "text",
          "priority": "core",
          "required": true,
          "pdf_fields": ["Producer_FullName_A"]
        },
        {
          "key": "producer_phone",
          "label": "Agency phone",
          "type": "phone",
          "priority": "common",
          "required": false,
          "pdf_fields": ["Producer_ContactPhone_A"]
        }
      ]
    },
    {
      "id": "insured",
      "label": "Named Insured",
      "prefill_from": "client",
      "fields": [
        {
          "key": "insured_name",
          "label": "Insured name",
          "type": "text",
          "priority": "core",
          "required": true,
          "pdf_fields": ["NamedInsured_FullName_A"]
        }
      ]
    },
    {
      "id": "certholder",
      "label": "Certificate Holder",
      "fields": [
        {
          "key": "holder_name",
          "label": "Certificate holder name & address",
          "type": "textarea",
          "priority": "core",
          "required": true,
          "pdf_fields": ["CertificateHolder_FullName_A"]
        }
      ]
    }
  ]
}
```

### Supported field `type` values
`text`, `textarea`, `number`, `currency`, `date`, `phone`, `email`, `state` (dropdown of US states), `select` (with `options`), `checkbox`, `radio`.

### Validation
Validate by type on both client and server: dates (MM/DD/YYYY), phone, email, state code, EIN/SSN format (mask + don't log raw values beyond the snapshot), required fields. Reject submission if required fields are empty.

### Notes for whoever writes the maps
- One answer can write to multiple `pdf_fields` (some ACORD fields repeat the value).
- PDF field names come from `tools/dump_fields.py` — copy them exactly (they are case-sensitive and often cryptic).
- Checkboxes in ACORD PDFs use specific "on" state names — dump them and record the exact export value.

---

## 7. PDF fill pipeline (`pdf_fill.py`)

> This pipeline has been **verified end-to-end on the real ACORD 25 (2016/03)** file. Use it as the reference for all forms. Read the `pdf` skill at `/mnt/skills/public/pdf/SKILL.md` for context.

### Critical: real ACORD PDFs are XFA + owner-password encrypted
`pdfinfo` on the supplied ACORD 25 reports `Form: XFA` and `Encrypted: yes (copy:no change:no, RC4)`. Two consequences:
- **XFA shadows AcroForm.** If you fill only the AcroForm layer, Adobe (and some other viewers) render from the XFA layer and your values appear blank. You MUST drop the XFA layer.
- **Owner-password restriction.** The copy/change restriction must be removed before programmatic fill. There is no *user* password, so it strips cleanly.

Both are solved in one prep step with pdftk.

### Verified pipeline (per form)
**One-time template prep** (do this once per template, store the clean copy):
```bash
pdftk template.pdf output template_clean.pdf drop_xfa
# result: Form: AcroForm, Encrypted: no
```

**Per fill:**
```python
from pypdf import PdfReader, PdfWriter
reader = PdfReader("template_clean.pdf")
writer = PdfWriter(); writer.append(reader)
prefix = "F[0].P1[0]."                      # from schema _meta.field_name_prefix
data = { prefix + rel_name: value for rel_name, value in mapped_answers }
for page in writer.pages:
    writer.update_page_form_field_values(page, data, auto_regenerate=False)
writer.write("filled.pdf")
```

**Flatten before email/print/download** (locks the form, renders identically everywhere):
```bash
pdftk filled.pdf output final.pdf flatten
```

### Field facts (verified, ACORD 25)
- 128 fields total: 104 Text, 24 Button.
- Field names are hierarchical: `F[0].P1[0].<RelativeName>`. Schema stores the relative name; backend prepends the prefix.
- **Checkboxes:** on = `"1"`, off = `"Off"` (uniform across all 24 buttons).
- **ADDL INSD / SUBR WVD** are **Text** fields expecting `"Y"`/`"N"` — not checkboxes.
- Source of truth for names: `pdftk template.pdf dump_data_fields` (each field also carries a human-readable `FieldNameAlt` tooltip — useful for auto-generating labels for forms you haven't hand-mapped yet).

### Form logic to enforce (the "connected" behavior)
The ACORD 25 has real cross-field logic; the renderer + validator must honor it (fully specified in `schemas/acord_25.json` under `logic`):
1. **Insurer A–F reference table.** User enters up to 6 insurers (name + NAIC) once. Each coverage block has an `InsurerLetterCode` that points to one. UX: render those as a **dropdown of the insurers already entered**, write the chosen **letter** to the field — no re-typing insurer names per line.
2. **Optional coverage blocks.** GL / Auto / Umbrella-Excess / Workers Comp are each optional. When a block is excluded, skip all its `pdf_field`s. At least one block required for a valid cert.
3. **Mutually-exclusive radio groups** (only one `"1"`, rest `"Off"`): Occurrence vs Claims-Made (GL and Umbrella), Umbrella vs Excess, Deductible vs Retention, and the aggregate-applies-per row (Policy/Project/Loc/Other).
4. **Reveals:** aggregate "Other" reveals a description field; Workers Comp "excluded = Y" should prompt filling Description of Operations.

### Edge cases to handle
Missing optional fields (leave blank), checkbox export values, fields absent in a given edition (skip with a warning, don't crash), and the unused "Other" auto / other-policy slots (omitted from v1 as `rare` — see schema).

### A worked, verified example ships with this spec
`schemas/acord_25.json` is the complete, real field map for ACORD 25 — actual field names, sections, types, priorities, required flags, and the logic rules above. **Use it as the pattern** when mapping the other eight forms (dump their fields, follow the same structure).

---

## 8. Backend API (Flask)

All routes require an authenticated session except the auth routes. JSON in/out unless noted.

```
GET  /auth/login                 -> redirect to Google
GET  /auth/callback              -> verify, enforce @weinsurethings.com, create session
POST /auth/logout

GET  /api/forms?q=<query>        -> search catalog (matches title/number/keywords/category)
GET  /api/forms/<id>             -> full schema for rendering

GET  /api/profiles?type=agency   -> list reusable profiles
POST /api/profiles               -> create/update a profile
GET  /api/profiles/<id>

POST /api/forms/<id>/preview     -> body: {answers, profile_ids?}; returns filled PDF (bytes or short-lived URL) for in-browser preview
POST /api/forms/<id>/download    -> fill+flatten, log submission(action=download), return file
POST /api/forms/<id>/print       -> same as download but log action=print (frontend opens print dialog on the returned PDF)
POST /api/forms/<id>/email       -> body: {answers, recipients[], message?}; fill+flatten, send email with CC=owner, log submission(action=email)

# Phase 2
POST /api/drafts                 -> save draft
GET  /api/drafts/<id>
GET  /api/nowcerts/lookup?customer=<q>  -> prefill data from NowCerts
GET  /api/admin/field-usage/<form_id>   -> usage stats (admin)
POST /api/admin/forms/<id>/retag        -> update field priority/required (admin)
```

On every successful fill, increment `field_usage.times_filled` for non-empty fields and `times_skipped` for empty optional ones.

---

## 9. Frontend UX

Single page, WIT-branded, mobile-friendly. Flow:

1. **Landing = search box** (centered, prominent). Typing filters the form catalog live (number, title, keyword, category). Show a short recent/favorites list below.
2. **Select form** → render the dynamic form from schema:
   - Group fields by section.
   - Show `core` + `common` fields by default; collapse `rare` fields under a "More fields" expander.
   - Prefill from selected agency/client profile (a dropdown at top: "Use profile: WIT Norfolk / Acme LLC").
   - Inline validation per field type.
3. **Preview** button → fill server-side → embed the returned PDF (PDF.js or iframe) in a preview pane/modal. User visually confirms.
4. **Three actions** under the preview:
   - **Email** → small dialog: recipient(s), optional message. CC field is pre-filled with the owner address and **locked/read-only** (always CC'd). Send.
   - **Print** → open browser print dialog on the generated PDF.
   - **Download** → download the flattened PDF.
5. Confirmation toast + the action is logged.

### Branding (apply throughout — WIT palette)
- WIT Blue (primary) `#00AEEF`
- Deep WIT Blue `#007EAE`
- Ink / Navy Black `#06121D`
- White `#FFFFFF`
- Soft background `#F4F8FB`
- Muted gray `#6B7280`
- Success green `#33D17A`
- Alert red `#FF6868`
- Mascot: friendly black monster with a bright blue circular face (use in header/empty states).

Keep it clean and uncluttered — search-first, generous whitespace, large tap targets.

---

## 10. Auth

- Google OAuth 2.0. On callback, **reject any email not ending in `@weinsurethings.com`** (configurable allowlist for edge cases). Auto-create the `users` row on first valid login. Server-side session cookie (HTTP-only, secure). `admin` role set manually in DB for Logan.

---

## 11. Email flow

- Send via SMTP or Twilio SendGrid (already in WIT's stack).
- **Always CC the WIT owner address** (config value, e.g. `OWNER_CC_EMAIL`), enforced server-side regardless of what the UI sends.
- Attach the flattened PDF.
- Subject/body templated (e.g. "Your [Form Title] from We Insure Things").
- Because forms carry PII, ensure SPF/DKIM/DMARC are set on the sending domain for deliverability, and never put PII in the subject line.
- Log recipients + CC + timestamp in `submissions`.

---

## 12. Security, compliance & config

- **PII**: ACORD forms can contain SSN/EIN/DOB/addresses. Encrypt the DB file at rest (Oracle volume encryption is fine), restrict file permissions on `data/` and generated PDFs.
- **Retention**: define a retention window for generated PDFs in `output_path` (e.g. auto-purge after N days; keep the `submissions` metadata row). Make N a config value.
- **Logging**: do NOT log raw SSN/EIN values. Mask in any debug output.
- **Secrets**: Google client secret, SendGrid key, OWNER_CC_EMAIL, NowCerts creds → env file / systemd `EnvironmentFile`, not committed.
- **Templates not in git**: licensed ACORD PDFs stay out of source control.
- **CSRF** protection on state-changing POSTs; rate-limit auth.

---

## 13. Starting form set (Phase 1 catalog)

Begin with the forms WIT actually runs day to day (confirm/trim this list):

| ACORD | Title | Category |
|---|---|---|
| 25 | Certificate of Liability Insurance | Certificate |
| 28 | Evidence of Commercial Property Insurance | Certificate |
| 35 | Cancellation Request / Policy Release | Change |
| 125 | Commercial Insurance Application (Applicant Information Section) | Commercial |
| 126 | Commercial General Liability Section | Commercial |
| 127 | Business Auto Section | Commercial |
| 130 | Workers Compensation Application | Commercial |
| 140 | Property Section | Commercial |
| 141 | Crime Section | Commercial |

Add personal lines (homeowner/auto) and others later. **Note:** WIT operates in VA, NV, NC, GA, AZ — some forms have state-specific supplements; track edition/state where it matters.

---

## 14. The "which fields are truly necessary" mechanism

This is built into the design, not bolted on:
1. Every field is tagged `core` / `common` / `rare` + `required` in the schema.
2. UX shows core+common, collapses rare.
3. `field_usage` records fills vs. skips per field per form.
4. Admin view (phase 2) shows usage stats; Logan can re-tag a field (e.g. demote a never-used field to `rare`, or hide it) **without code changes** — just a schema/DB update.
5. Over time the forms self-simplify based on real usage.

---

## 15. Open decisions for Logan (defaults assumed in this spec)

1. ✅ **Phase-1 form list** — confirmed: ACORD 25, 28, 35, 125, 126, 127, 130, 140, 141 (see §13).
2. ✅ **NowCerts prefill** — stays a Phase-2 hook, not in MVP.
3. **ACORD template source** — Logan to supply the licensed fillable PDFs for the nine forms. *(Gating input for the field maps — real PDF field names come from these.)*
4. **Owner CC address** — which email is always CC'd on outbound forms?
5. **Sending email** — SMTP relay vs Twilio SendGrid? *(Default: SendGrid.)*
6. **Profiles** — Agency profile per office (Norfolk/Vegas/Marshall) + ad-hoc client profiles? *(Default: yes.)*
7. **Retention window** for stored generated PDFs.

---

## 16. Build milestones (for the coding agent)

1. Scaffold Flask app, SQLite schema, config/env, systemd + nginx, deploy skeleton to `forms.weinsurethings.com`.
2. Google SSO with domain restriction.
3. `tools/dump_fields.py` + fill+flatten pipeline; prove it on ACORD 25 end-to-end (hardcoded answers → flattened PDF).
4. Schema loader + form catalog + search API + SPA shell (WIT-branded, search-first).
5. Dynamic schema-driven form rendering + validation + preview embed.
6. Download + print + email (with enforced owner CC) + submissions logging.
7. Agency/client profiles + prefill.
8. Author field maps for the rest of the Phase-1 form set.
9. Field-usage tracking.
10. (Phase 2) NowCerts lookup, drafts, admin field-tagging UI.

---

*End of spec.*
