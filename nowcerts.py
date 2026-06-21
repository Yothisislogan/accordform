"""NowCerts customer-lookup prefill — PHASE 2 STUB (do not implement yet).

The brief says to leave a clean hook, not build this. When implemented, this
will reuse the existing WIT Sales Tracker NowCerts integration to look up a
customer and return a {field_key: value} map suitable for prefilling the
`insured` (client) section — mirroring how `profiles.apply_profiles` merges
data.

Interface is stubbed so app.py can wire the route without a hard dependency.
"""
from __future__ import annotations


class NowCertsNotConfigured(RuntimeError):
    pass


def lookup_customer(query: str) -> dict:
    """Return prefill data for a customer. Phase 2 — not yet implemented.

    Intended return shape (when built):
        {"insured_name": ..., "insured_addr1": ..., "insured_city": ..., ...}
    """
    raise NowCertsNotConfigured(
        "NowCerts lookup is a Phase-2 feature and is not implemented yet."
    )
