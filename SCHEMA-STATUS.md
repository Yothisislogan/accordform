# WIT Forms — Schema Status

All 12 uploaded ACORD forms now have field-map schemas. One is hand-verified (ACORD 25); the rest are **auto-generated drafts** produced by `tools/build_schema.py` and need a human pass on **priorities, required flags, and cross-field logic** (insurer-letter refs, optional coverage blocks, radio groups). Labels, types, sections, and exact PDF field names are extracted from the real files and are accurate.

| Form | Edition | Fields | Status | Notes |
|---|---|---|---|---|
| 25 — Certificate of Liability | 2016/03 | 128 | ✅ hand-verified | reference pattern; fill pipeline proven |
| 28 — Evidence of Commercial Property | 2014/01 | 158 | draft | `prefix=F[0].` |
| 35 — Cancellation / Policy Release | 2011/09 | 98 | draft | lightest form |
| 125 — Commercial Insurance App | 2013/09 | 550 | draft | 4 pages; heaviest applicant form |
| 126 — Commercial GL Section | 2016/09 | 255 | draft | attaches to 125 |
| 127 — Business Auto Section | 2012/03 | 623 | draft | most fields (vehicle blocks repeat) |
| 130 — Workers Comp App | 2013/09 | 484 | draft | 4 pages (full form) |
| 140 — Property Section | 2016/03 | 356 | draft | attaches to 125 |
| 141 — Crime Section | 2016/03 | 364 | draft | attaches to 125 |
| **128 — Garage & Dealers** | 2012/04 | 360 | draft | ⚠️ beyond original 9 |
| **131 — Umbrella / Excess** | 2013/12 | 396 | draft | ⚠️ beyond original 9 |
| **135 NC — NC Assigned-Risk WC** | 2015/10 | 420 | draft | ⚠️ beyond original 9; `prefix=""` (different designer, not XFA layout) |

## Decisions for Logan
1. **Three extra forms** (128, 131, 135 NC) were in this batch but not in the agreed Phase-1 nine. 131 (Umbrella/Excess) is a natural fit; 128 (Garage) is niche; 135 NC is a NC Rate Bureau assigned-risk form (relevant since WIT writes NC). Keep all three, or defer some?
2. **Duplicate upload:** `Acord_130_WC_page_2.pdf` is redundant — the main ACORD 130 file already contains all 4 pages. Ignore it.

## Per-form review checklist (for the draft schemas)
For each draft, a human pass should:
- Set `priority` (core/common/rare) per field — generator guessed from name tokens.
- Set `required` flags (generator left most `false`).
- Add cross-field logic where the form has it: insurer-letter dropdowns, optional coverage blocks (`optional_block` + `include_toggle`), mutually-exclusive `radio_group`s, and `yn_code` Y/N text fields. See `acord_25.json` for the worked pattern.
- Spot-check a filled+flattened output renders (use the verified pipeline in the build spec §7).

## Regenerating / adding forms
`python3 tools/build_schema.py <form.pdf> <acord_number> <edition> > schemas/acord_<n>.json`
Reads field names + tooltips straight from the PDF; output matches the ACORD 25 shape.
