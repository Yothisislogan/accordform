"""What has WIT Forms already generated for a client?

Reads the existing `submissions` audit log (every produced ACORD / loss run
snapshot lands there) so the Hedge checklist can show a concrete gap list:
"have: 125, 126, loss runs · missing: …". Matching is by the client's name
appearing in the stored answers snapshot — the same reference a CSR uses.
"""
from __future__ import annotations


def artifacts_for_client(db, client_ref: str) -> dict:
    """{"acords": ["125", ...], "loss_runs": bool} for a client reference."""
    ref = (client_ref or "").strip()
    if not ref:
        return {"acords": [], "loss_runs": False}
    like = f"%{ref}%"
    rows = db.execute(
        """SELECT s.action, f.acord_number
           FROM submissions s LEFT JOIN forms f ON f.id = s.form_id
           WHERE s.answers_snapshot LIKE ?""", (like,)).fetchall()
    acords: set[str] = set()
    loss_runs = False
    for r in rows:
        action = r["action"] if hasattr(r, "keys") else r[0]
        number = r["acord_number"] if hasattr(r, "keys") else r[1]
        if action == "loss_run":
            loss_runs = True
        elif number:
            acords.add(str(number))
    return {"acords": sorted(acords, key=lambda n: (len(n), n)),
            "loss_runs": loss_runs}
