"""Listing lifecycle CRM (introduced 2026-05-10).

State machine and transition helper for the per-listing CRM. The denormalised
current state lives on `listing.current_status`; the audit trail of all
transitions lives in `listing_lifecycle`.

States:
    new                 default after enrichment
    contact_queued      user marked it interesting; should contact agent
    contacted           agent replied / viewing booked
    shortlisted         viewed and approved (working set)
    triage_reject       rejected before contact (terminal)
    not_contacted       contact_queued but never reached (terminal)
    final_reject        viewed and rejected (terminal)

Reason taxonomy lives in `rental-crm-reasons.yaml` at the project root.
The set of legal transitions is enforced here; if a caller asks for an
illegal transition the function raises ValueError so the bug shows up
loudly.

The function is designed to be called from FastAPI route handlers (which
get a SQLAlchemy session) or from one-off scripts (which open their own
sqlite3 connection). Both shapes are supported via the `executor` parameter.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = os.environ.get("HOMEHUNT_DB", str(PROJECT_ROOT / "data" / "homehunt.db"))
REASONS_YAML_PATH = PROJECT_ROOT / "rental-crm-reasons.yaml"

STATES = {
    "new",
    "contact_queued",
    "contacted",
    "shortlisted",
    "triage_reject",
    "not_contacted",
    "final_reject",
}

TERMINAL_STATES = {"triage_reject", "not_contacted", "final_reject", "shortlisted"}

LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "new": {"triage_reject", "contact_queued", "contacted"},
    "contact_queued": {"contacted", "not_contacted", "triage_reject"},
    "contacted": {"shortlisted", "final_reject", "not_contacted"},
    "shortlisted": {"final_reject"},
    "triage_reject": {"contact_queued"},  # resurrection: user changes mind
    "not_contacted": {"contact_queued"},
    "final_reject": set(),
}


class IllegalTransition(ValueError):
    pass


def load_reasons() -> dict[str, list[str]]:
    """Read the reason taxonomy from rental-crm-reasons.yaml.

    Returns a mapping like {"triage_reject": ["bad_photos", ...], ...}.
    Empty dict if the file is missing.
    """
    if not REASONS_YAML_PATH.exists():
        return {}
    return yaml.safe_load(REASONS_YAML_PATH.read_text()) or {}


def is_legal(from_status: Optional[str], to_status: str) -> bool:
    if to_status not in STATES:
        return False
    if from_status is None:
        from_status = "new"
    if from_status not in STATES:
        return False
    return to_status in LEGAL_TRANSITIONS.get(from_status, set())


def transition(
    uid: str,
    to_status: str,
    reason: Optional[str] = None,
    notes: Optional[str] = None,
    transitioned_by: str = "user",
    db_path: str = DEFAULT_DB_PATH,
) -> dict:
    """Record a transition and update the denormalised state.

    Raises IllegalTransition if the (from, to) pair is not allowed.
    Returns the lifecycle row that was inserted.

    Idempotent on the to_status side: a transition to the same state is
    rejected as illegal (set diff). To re-record a state with a new reason,
    call with the legal off-then-back path explicitly.
    """
    if to_status not in STATES:
        raise IllegalTransition(f"Unknown to_status: {to_status}")

    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT current_status FROM listing WHERE uid = ?",
            (uid,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Listing not found: {uid}")
        from_status = row[0] or "new"

        if not is_legal(from_status, to_status):
            raise IllegalTransition(
                f"Illegal transition {from_status} -> {to_status} for {uid}. "
                f"Allowed from {from_status}: {sorted(LEGAL_TRANSITIONS.get(from_status, set()))}"
            )

        now = datetime.now(timezone.utc).isoformat()
        cur = con.execute(
            """
            INSERT INTO listing_lifecycle
                (uid, from_status, to_status, reason, notes, transitioned_at, transitioned_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (uid, from_status, to_status, reason, notes, now, transitioned_by),
        )
        lifecycle_id = cur.lastrowid

        # Mark terminal-reject listings as inactive so they fall out of
        # default queries and the freshness gate stops touching them.
        if to_status in {"triage_reject", "final_reject", "not_contacted"}:
            is_active_update = ", is_active = 0, status = 'inactive'"
        elif to_status in {"contact_queued", "contacted", "shortlisted"}:
            is_active_update = ""  # leave is_active alone
        else:
            is_active_update = ""

        con.execute(
            f"""
            UPDATE listing
            SET current_status = ?,
                current_status_reason = ?,
                last_transition_at = ?
                {is_active_update}
            WHERE uid = ?
            """,
            (to_status, reason, now, uid),
        )
        con.commit()
        return {
            "id": lifecycle_id,
            "uid": uid,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "notes": notes,
            "transitioned_at": now,
            "transitioned_by": transitioned_by,
        }
    finally:
        con.close()


def get_history(uid: str, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT id, uid, from_status, to_status, reason, notes,
                   transitioned_at, transitioned_by
            FROM listing_lifecycle
            WHERE uid = ?
            ORDER BY transitioned_at ASC, id ASC
            """,
            (uid,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()
