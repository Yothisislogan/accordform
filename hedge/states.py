"""Submission lifecycle states — local and remote, never collapsed.

Two separate columns, two separate vocabularies:

LOCAL (ours, drives the wizard):
    draft -> uploading -> needs_requirements -> awaiting_approval -> finalized

REMOTE (Hedge's, recorded VERBATIM from `state`/`status_label` in responses):
    the brief names submitted / released to market / quoted / bound / issued;
    the hedge-cli source shows the server defines these, so anything Hedge
    sends is stored as-is — unknown values are preserved, never mapped onto a
    known one. "Submitted" must never render as anything implying quoted or
    bound, so the UI always prints the remote label verbatim next to the
    local state.
"""
from __future__ import annotations

LOCAL_STATES = ("draft", "uploading", "needs_requirements",
                "awaiting_approval", "finalized")

# Every legal local transition. Anything else is a bug, not a request.
TRANSITIONS = {
    "draft": {"uploading"},                       # remote create succeeded
    "uploading": {"needs_requirements"},          # requirements pulled
    "needs_requirements": {"awaiting_approval",   # review screen opened/completed
                           "uploading"},          # more docs needed after all
    "awaiting_approval": {"finalized",            # explicit human approval only
                          "needs_requirements",   # went back to fix something
                          "uploading"},
    "finalized": set(),                           # local terminal; remote drives on
}

# Known remote states, for ORDERING/grouping in the UI only — display always
# uses the verbatim value from Hedge, and unknown values are fully legal.
KNOWN_REMOTE = ("submitted", "released_to_market", "quoted", "bound", "issued")


class StateError(ValueError):
    pass


def check_transition(current: str, new: str) -> None:
    if current not in TRANSITIONS:
        raise StateError(f"unknown local state {current!r}")
    if new not in TRANSITIONS[current]:
        raise StateError(f"illegal transition {current!r} -> {new!r}")


def advance(db, local_id: int, new_state: str) -> None:
    """Move a local submission, enforcing the transition table."""
    row = db.execute("SELECT local_state FROM hedge_submissions WHERE id=?",
                     (local_id,)).fetchone()
    if row is None:
        raise StateError(f"no local submission {local_id}")
    check_transition(row["local_state"] if hasattr(row, "keys") else row[0], new_state)
    db.execute("UPDATE hedge_submissions SET local_state=?, updated_at=CURRENT_TIMESTAMP "
               "WHERE id=?", (new_state, local_id))
    db.commit()
