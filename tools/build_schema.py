#!/usr/bin/env python3
"""
generate_schema.py — Auto-draft a WIT Forms field-map schema from a fillable ACORD PDF.

Usage:
    python3 generate_schema.py <prepped_or_raw.pdf> <acord_number> <edition> > schemas/acord_<n>.json

It shells out to `pdftk <pdf> dump_data_fields`, then infers:
  - field_name_prefix (longest common prefix, e.g. "F[0].P1[0]." or "F[0].")
  - per-field: relative pdf_field, type, label, help (from FieldNameAlt), priority, section
  - checkbox on-value (from FieldStateOption)

Output matches the hand-verified acord_25.json shape. It is a DRAFT:
labels/types/sections are inferred; PRIORITIES and CROSS-FIELD LOGIC need human review.
"""
import json, re, subprocess, sys, os

# ---- type inference from the relative field name + pdftk field type ----
def infer_type(rel, ftype):
    n = rel.lower()
    if ftype == "Button":
        return "checkbox"
    if "emailaddress" in n:                      return "email"
    if "phonenumber" in n or "faxnumber" in n:   return "phone"
    if "stateorprovincecode" in n:               return "state"
    if "date" in n:                              return "date"
    if any(k in n for k in ("limitamount","amount","premium","_cost","costnew",
                            "value","payroll","remuneration","grosssales","_sales",
                            "deposit","deductible")): return "currency"
    if any(k in n for k in ("remarktext","description","descriptiontext","operations",
                            "explanation","comments")): return "textarea"
    if any(k in n for k in ("numberof","_count","quantity","yearsin","numemployees",
                            "employeecount")): return "number"
    return "text"   # NAICCode, identifiers, postal code, names, etc.

# ---- section + prefill inference from the leading token of the relative name ----
SECTION_LABELS = {
    "Form": "Certificate Info", "Producer": "Producer / Agency",
    "NamedInsured": "Named Insured", "Applicant": "Applicant",
    "FirstNamedInsured": "Named Insured", "Insurer": "Insurer / Carrier",
    "Policy": "Policy", "CertificateHolder": "Certificate Holder",
    "CertificateOfInsurance": "Certificate", "GeneralLiability": "General Liability",
    "Vehicle": "Automobile", "ExcessUmbrella": "Umbrella / Excess",
    "WorkersCompensationEmployersLiability": "Workers Comp",
    "Property": "Property", "Crime": "Crime", "AdditionalInterest": "Additional Interest",
}
PREFILL = {"Producer": "agency", "NamedInsured": "client",
           "FirstNamedInsured": "client", "Applicant": "client"}

# ---- priority heuristic (sane default UX; tune by hand later) ----
CORE_HINTS = ("Form_CompletionDate", "Producer_FullName", "NamedInsured_FullName",
              "FirstNamedInsured", "Applicant_FullName",
              "PolicyNumberIdentifier", "EffectiveDate", "ExpirationDate")
COMMON_TOKENS = ("MailingAddress", "ContactPerson", "Insurer", "LimitAmount",
                 "Amount", "Premium", "PhoneNumber", "EmailAddress", "NAICCode",
                 "PolicyType", "CoverageIndicator")

def infer_priority(rel):
    if any(h in rel for h in CORE_HINTS):      return "core"
    if any(t in rel for t in COMMON_TOKENS):   return "common"
    return "rare"

def humanize(rel):
    base = re.sub(r"\[\d+\]$", "", rel).split(".")[-1]      # drop [0], keep last segment
    base = re.sub(r"_[A-Z]$", "", base)                     # drop trailing _A/_B suffix
    base = base.replace("_", " ")
    base = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base)        # split camelCase
    return base.strip().capitalize()

def clean_alt(alt):
    if not alt: return None
    alt = re.sub(r"^(Enter|Check|Select)\s+\w+:\s*", "", alt).strip()
    alt = alt.replace("&apos;", "'").rstrip(". ").strip()
    return alt or None

def parse_dump(text):
    recs, cur = [], {}
    for line in text.splitlines():
        if line.strip() == "---":
            if cur: recs.append(cur); cur = {}
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            cur.setdefault(k.strip(), []).append(v.strip())
    if cur: recs.append(cur)
    return recs

def common_prefix(names):
    if not names: return ""
    p = os.path.commonprefix(names)
    # snap back to a clean boundary ending in "]." so relative names stay valid
    m = re.search(r"^(.*\]\.)", p)
    return m.group(1) if m else ""

def main():
    pdf, acord_no, edition = sys.argv[1], sys.argv[2], sys.argv[3]
    dump = subprocess.run(["pdftk", pdf, "dump_data_fields"],
                          capture_output=True, text=True).stdout
    recs = [r for r in parse_dump(dump) if r.get("FieldName")]
    names = [r["FieldName"][0] for r in recs]
    prefix = common_prefix(names)

    sections, order = {}, []
    for r in recs:
        full = r["FieldName"][0]
        rel = full[len(prefix):]
        ftype = (r.get("FieldType") or ["Text"])[0]
        alt = clean_alt((r.get("FieldNameAlt") or [None])[0])
        # leading token (after any P<n>[0]. page segment) -> section
        tok = re.sub(r"^P\d+\[\d+\]\.", "", rel).split("_")[0].split("[")[0]
        sec_id = tok or "general"
        if sec_id not in sections:
            sections[sec_id] = {"id": sec_id.lower(),
                                "label": SECTION_LABELS.get(sec_id, humanize(sec_id)),
                                "fields": []}
            if sec_id in PREFILL: sections[sec_id]["prefill_from"] = PREFILL[sec_id]
            order.append(sec_id)
        field = {"key": re.sub(r"[^a-z0-9]+", "_", rel.lower()).strip("_"),
                 "label": humanize(rel),
                 "type": infer_type(rel, ftype),
                 "priority": infer_priority(rel),
                 "required": False,
                 "pdf_field": rel}
        if alt: field["help"] = alt
        if ftype == "Button":
            ons = [o for o in r.get("FieldStateOption", []) if o != "Off"]
            field["on_value"] = ons[0] if ons else "1"
        sections[sec_id]["fields"].append(field)

    schema = {
        "_meta": {
            "acord_number": acord_no, "edition": edition,
            "template_source": os.path.basename(pdf),
            "field_name_prefix": prefix,
            "total_pdf_fields": len(recs),
            "checkbox_off_value": "Off",
            "_DRAFT": "Auto-generated. Labels/types/sections inferred; "
                      "REVIEW priorities, required flags, and cross-field logic "
                      "(insurer letter refs, optional blocks, radio groups) by hand."
        },
        "sections": [sections[s] for s in order]
    }
    print(json.dumps(schema, indent=2))

if __name__ == "__main__":
    main()
