# WIT Forms — Schema Status

This file tracks what is actually committed in the GitHub repo, not just what exists in local handoff files.

## Current committed schemas

| Form | Edition | Status | Notes |
|---|---:|---|---|
| 25 — Certificate of Liability | 2016/03 | ✅ hand-verified | Reference pattern; PDF fill pipeline proven. |
| 125 — Commercial Insurance Application | 2013/09 | ✅ hand-verified core committed | Commercial hub. Core/app-proof fields plus Sections Attached hub are committed in `schemas/acord_125.json`. Logan also supplied the full 550-field hand-verified map as the reference source for future expansion. |
| 126 — Commercial General Liability Section | 2016/09 | draft committed | Extracted from Logan's uploaded fillable PDF. Includes coverages/limits, hazards, claims-made, employee benefits, contractors, products/completed operations, additional interest, general information, and signature. Needs a human pass for radio groups/optional logic. |
| 127 — Business Auto Section | 2012/03 | draft core committed | Extracted from Logan's uploaded fillable PDF. Maps the high-use Business Auto workflow: policy/applicant, driver rows, general underwriting questions, vehicle description rows, additional interest, remarks, and signature. PDF has 627 detected AcroForm entries; future expansion can add all repeated driver rows and full coverage checkboxes for vehicles 2-4. |
| 130 — Workers Compensation Application | 2013/09 | draft core committed | General WC application. Extracted from Logan's uploaded fillable PDF. Maps producer/applicant, billing/audit/policy, locations, contacts, included/excluded individuals, state rating worksheet, prior/loss history, operations/general questions, and signature. The separate uploaded page 2 is the state rating worksheet and is represented inside this schema. |
| 135 NC — NC Assigned-Risk WC | 2015/10 | draft committed | Special North Carolina Workers Compensation Insurance Plan / assigned-risk application. Keep this separate from ACORD 130 because it is NC bureau/assigned-risk specific. PDF has 420 detected fields. |
| 140 — Property Section | 2016/03 | draft core committed | Extracted from Logan's uploaded fillable PDF. Maps the high-use Property workflow: policy/applicant, blanket summary, first premises/building, coverage rows, additional coverages/options, construction/security, additional interest, remarks, and signature. PDF has 356 fields; future expansion can add all repeated additional-premises page 2 fields. |
| 128 — Garage & Dealers | 2012/04 | draft | Beyond original Phase 1, but useful for garage/dealer risks. |
| 131 — Umbrella / Excess | 2013/12 | draft | Natural companion to 125. |

## Known missing schema commits

These forms were listed in the broader handoff/status notes, but are not yet committed in this repo as schema files:

| Form | Edition | Target status |
|---|---:|---|
| 28 — Evidence of Commercial Property | 2014/01 | draft needed |
| 35 — Cancellation / Policy Release | 2011/09 | draft needed |
| 141 — Crime Section | 2016/03 | draft needed |

## Workers Compensation notes

ACORD 130 and ACORD 135 NC are intentionally separate workflows:

- ACORD 130 is the general Workers Compensation Application.
- ACORD 135 NC is the North Carolina Workers Compensation Insurance Plan / assigned-risk application.
- The ACORD 130 page 2 worksheet can be attached/repeated for multiple states. It is modeled inside the ACORD 130 schema under the state rating worksheet section.

## ACORD 125 implementation notes

ACORD 125 is the commercial hub. The form's page 1 "Sections Attached" area should drive companion form shortcuts:

- General Liability → ACORD 126
- Business Auto → ACORD 127
- Property → ACORD 140
- Crime → ACORD 141
- Umbrella → ACORD 131
- Garage and Dealers / Dealers → ACORD 128

The current committed `acord_125.json` duplicates the important `sections_attached` map as a normal rendered schema section. That lets the current app render/fill the checkboxes and premium fields without needing immediate renderer changes.

## Per-form review checklist for draft schemas

For each draft, a human pass should:

- Set `priority` values: `core`, `common`, or `rare`.
- Set `required` flags.
- Add cross-field logic where the form has it: insurer-letter dropdowns, optional coverage blocks, mutually exclusive radio groups, and Y/N code fields.
- Spot-check a filled and flattened output against the real ACORD PDF.

## Template handling

Licensed blank PDFs stay out of git. Drop them on the server under `templates/acord/`, prep with `tools/prep_template.py`, then run the live fill smoke test.
