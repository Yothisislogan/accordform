# WIT Forms — Coding Agent Brief

You are building **WIT Forms**, an internal web app that fills ACORD insurance PDFs for We Insure Things (≤5 users). Follow this brief exactly. When this brief and your own assumptions conflict, this brief wins.

## Input files (authoritative — read first)
1. `wit-forms-build-spec.md` — full specification. The source of truth for architecture, data model, API, UX, deployment.
2. `acord_25.json` — a complete, **verified** field map for ACORD 25. The pattern every other form schema must follow.
3. Licensed ACORD PDFs (provided by Logan; ACORD 25 confirmed). Treat as read-only licensed assets.

---

## HARD RULES (non-negotiable)
1. **Do NOT recreate, redraw, or scrape ACORD forms.** You only fill the licensed PDF templates Logan supplies. If a template is missing, stop and ask — do not synthesize one.
2. **Templates and prepped copies are NOT committed to git.** Add `templates/` and `data/` to `.gitignore`. They contain licensed IP and PII.
3. **Always flatten** the PDF before email / print / download. The emailed/printed copy must not be editable.
4. **Owner CC is enforced server-side** on every email, regardless of what the client sends. Never trust the client to set it.
5. **Auth is domain-restricted.** Reject any Google account not on the `@weinsurethings.com` allowlist.
6. **Never log PII** (SSN/EIN/DOB/full addresses) in plaintext logs. Mask in debug output.
7. **Schema-driven, always.** No form layout is hardcoded in the frontend or backend. Adding a form = drop a template + write one schema JSON. If you find yourself writing form-specific `if` branches, stop and move that logic into the schema.

---

## Environment & stack (match WIT's existing tools — do not substitute)
- **Host:** Oracle Cloud Ubuntu. Deploy behind **nginx** reverse proxy via a **systemd** service.
- **Subdomain:** `forms.weinsurethings.com`. **App port:** `8097` (8095/8096 are taken).
- **Backend:** Python + **Flask**.
- **DB:** **SQLite** (schema in spec §5).
- **Auth:** **Google OAuth 2.0**, domain-restricted (same pattern as the existing WIT Sales Tracker).
- **PDF:** `pdftk` (system) + `pypdf` (Python). Both confirmed working in the target environment.
- **Email:** SMTP or Twilio SendGrid — read `OWNER_CC_EMAIL` and email transport from config; do not hardcode.
- **Frontend:** single-page app, WIT-branded, search-first. Vanilla JS is fine; keep it simple. WIT palette is in spec §9.
- **Secrets** via env / systemd `EnvironmentFile`. Nothing secret in git.

---

## Repo layout to create
```
wit-forms/
  app.py  auth.py  db.py  forms_catalog.py  pdf_fill.py
  email_service.py  profiles.py  submissions.py
  tools/dump_fields.py  tools/prep_template.py
  templates/acord/            # licensed PDFs + *_clean.pdf  (gitignored)
  schemas/acord_25.json       # provided; author the other 8 here
  static/                     # index.html app.js styles.css wit assets
  data/witforms.db            # gitignored
  requirements.txt  witforms.service  nginx.conf.example  .gitignore  README.md
```

---

## The PDF pipeline is already solved — reproduce it, don't redesign it
Real ACORD PDFs are **XFA + owner-password encrypted**. Filling the AcroForm layer naively makes values invisible in Adobe. Verified working recipe:

**`tools/prep_template.py`** (run once per template, output stored alongside):
```bash
pdftk template.pdf output template_clean.pdf drop_xfa   # -> Form: AcroForm, Encrypted: no
```

**`pdf_fill.py`** core:
```python
from pypdf import PdfReader, PdfWriter
reader = PdfReader(clean_template_path); writer = PdfWriter(); writer.append(reader)
prefix = schema["_meta"]["field_name_prefix"]          # "F[0].P1[0]."
data = { prefix + rel: val for rel, val in mapped_pairs }   # rel = schema pdf_field
for page in writer.pages:
    writer.update_page_form_field_values(page, data, auto_regenerate=False)
writer.write(filled_path)
```
**Flatten** before returning:
```bash
pdftk filled.pdf output final.pdf flatten
```
Facts (verified on ACORD 25): checkbox on=`"1"` off=`"Off"`; ADDL INSD / SUBR WVD are **text** `"Y"`/`"N"`; field names are `F[0].P1[0].<relative>`.

---

## Build order (vertical slice first — get ONE form fully working before adding others)

**M0 — Scaffold & deploy skeleton.** Repo, Flask app factory, SQLite init (spec §5), `.gitignore`, systemd unit, nginx config. Deploy a "hello" page to `forms.weinsurethings.com` over HTTPS.
*Done when:* the subdomain serves the app behind nginx via systemd.

**M1 — Auth.** Google SSO, domain restriction, session, auto-provision `users` row.
*Done when:* only `@weinsurethings.com` accounts can reach any `/api/*` route.

**M2 — PDF pipeline.** `tools/dump_fields.py`, `tools/prep_template.py`, `pdf_fill.py`. Prove it: fill ACORD 25 from `acord_25.json` with hardcoded answers → flattened PDF that renders correctly.
*Done when:* a flattened ACORD 25 with sample data renders all values in Chrome AND Adobe/Preview.

**M3 — Catalog + search + schema render.** `forms` table seeded with ACORD 25; `GET /api/forms?q=`; SPA search box → results → dynamic form rendered from `acord_25.json` (sections, types, priorities, the insurer A–F dropdown, optional coverage blocks, radio groups, Y/N codes). Show core+common; collapse `rare`. Client + server validation.
*Done when:* a user can search "25", open it, and see a correctly grouped, logic-aware form with no hardcoded layout.

**M4 — Preview + 3 actions.** `POST /preview` (embedded PDF.js preview) → **Download**, **Print**, **Email** (recipient dialog, CC pre-filled & locked, attaches flattened PDF). Log every action to `submissions`.
*Done when:* ACORD 25 works end-to-end: search → fill → preview → email(cc owner)/print/download, all logged.

**M5 — Profiles.** Agency profile per office (Norfolk/Vegas/Marshall) + ad-hoc client profiles; prefill via each section's `prefill_from`.
*Done when:* selecting an agency + client profile prefills the producer/insured blocks.

**M6 — Author the remaining 8 forms.** For each (28, 35, 125, 126, 127, 130, 140, 141): `prep_template`, `dump_fields`, write `schemas/acord_<n>.json` following the ACORD 25 pattern, seed catalog. Use each field's `FieldNameAlt` tooltip to auto-draft labels, then hand-tune logic/priorities.
*Done when:* all nine forms fill+flatten correctly and appear in search.

**M7 — Field-usage tracking.** Increment `field_usage` on fill (filled vs skipped). Lay groundwork for the admin re-tagging view (full feature is Phase 2).

### Phase 2 (do not build yet — leave clean hooks)
NowCerts customer-lookup prefill (reuse Sales Tracker integration), draft save/resume, admin field re-tagging UI. Stub the interfaces; don't implement.

---

## Schema contract (how to consume + extend)
- `_meta.field_name_prefix` is prepended to every `pdf_field` before filling.
- `insurers` is the A–F reference table: render a dropdown from filled rows; write the **letter** to each block's `insurer_ref` field.
- `sections[].fields[]`: `key` (internal), `label`, `type` (text/textarea/number/currency/date/phone/email/state/checkbox/radio_group/insurer_ref/yn_code), `priority` (core/common/rare), `required`, `pdf_field`, optional `show_if`.
- `optional_block` + `include_toggle`: skip all of a block's `pdf_field`s when excluded.
- `logic`: enforce radio-group exclusivity, reveals, Y/N literal text, and "≥1 coverage block required."
- New forms must validate against this same shape. Write a small JSON validator so a malformed schema fails loudly at load.

---

## Acceptance tests (write these)
1. Fill ACORD 25 with a full sample, flatten, assert key values present in extracted text + a visual spot check.
2. Excluded coverage block leaves all its fields blank.
3. Radio group never emits two `"1"` values.
4. Email path always includes `OWNER_CC_EMAIL` even if the client omits it.
5. Non-WIT Google account is rejected at auth.
6. Malformed schema raises at load, not at fill time.

---

## Values to get from Logan (use placeholders + a TODO if unset; don't block)
- `OWNER_CC_EMAIL` — the address CC'd on every outbound form.
- Email transport: SMTP creds **or** SendGrid key.
- Google OAuth client ID/secret for `forms.weinsurethings.com`.
- The 8 remaining licensed PDFs (28, 35, 125, 126, 127, 130, 140, 141).

## Conventions
Small, reviewable commits per milestone. README with run/deploy steps. WIT palette exactly (spec §9). Keep the UX search-first and uncluttered — this is for busy CSRs, not power users.

## First action
Read all three input files, confirm the repo layout and milestone plan back in one short message, then begin **M0**.
