"""Hedge Specialty integration for WIT Forms.

Layout:
    public_client.py  Phase 1 — public JSON feeds: SQLite cache, TTL,
                      changes.json staleness probe, stale-with-warning serve.
    feed_adapter.py   Map-driven reading of feed entries (hedge/feed_map.json);
                      verbatim raw fallback until the real feeds are captured.
    api_client.py     Phase 2 — authenticated submission pipeline (idempotency,
                      state discipline, review->finalize approval, audit) over
                      the transport in hedge_service.py.
    credstore.py      OAuth material at rest: Fernet ciphertext in the
                      hedge_credentials table (dev fallback: 0600 file).
    states.py         The lifecycle state machine. States are never collapsed.
    fixtures/         Captured public feeds + openapi.json (see
                      tools/fetch_hedge_feeds.py). Public data, committed.

The build environment cannot reach *.hedgespecialty.com, so everything
schema-dependent reads captured fixtures or the confirmed hedge-cli contract —
nothing here guesses field names.
"""
