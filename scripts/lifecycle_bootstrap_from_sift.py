#!/usr/bin/env python
"""Seed the lifecycle from sift v1 labels.

For every listing labelled in the most recent sift session:
    tier = best or good  -> contact_queued
    tier = bad or worse  -> triage_reject

The user's free-text reasoning is copied into listing_lifecycle.notes.
A short reason tag is inferred from the reasoning text where possible
(carpet, light, basement, etc); falls back to 'sift_v1_label'.

Idempotent: only operates on listings still in 'new' state.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from homehunt.lifecycle import IllegalTransition, transition  # noqa: E402

SIFT_DB = "/tmp/sift_v1_active.db"
PROD_DB = PROJECT_ROOT / "data" / "homehunt.db"

REASON_PATTERNS = [
    (re.compile(r"\bbasement\b", re.I), "basement_or_lower"),
    (re.compile(r"\bstudio\b", re.I), "wrong_neighbourhood"),  # used as proxy for 'wrong unit type'
    (re.compile(r"both\s*bedrooms.*carpet|carpets?\s*in\s*both", re.I), "bedroom_carpet"),
    (re.compile(r"\bcarpet", re.I), "bedroom_carpet"),
    (re.compile(r"\bnot\s*enough\s*photos|not\s*enough\s*pictures", re.I), "bad_photos"),
    (re.compile(r"\b(?:low|small)\s*ceiling|ceiling\s*low\b", re.I), "low_ceiling"),
    (re.compile(r"\bno\s*privacy|no\s*door\b", re.I), "no_privacy"),
    (re.compile(r"\bnot\s*enough\s*light|too\s*dark\b|\blight\s*$", re.I), "too_dark"),
    (re.compile(r"\bkitchen\s*(?:too\s*)?small|small.*kitchen", re.I), "kitchen_too_small"),
    (re.compile(r"\bweird.layout|bad\s*layout|layout.*not\s*good", re.I), "poor_floorplan"),
    (re.compile(r"\b40th\s*floor|too\s*high\s*floor|high\s*floor", re.I), "high_floor"),
    (re.compile(r"\blet\s*agreed\b", re.I), "agent_red_flag"),
]


def infer_reason(reasoning: str | None) -> str:
    if not reasoning:
        return "sift_v1_label"
    for pattern, tag in REASON_PATTERNS:
        if pattern.search(reasoning):
            return tag
    return "other"


def main() -> None:
    con = sqlite3.connect(SIFT_DB)
    rows = con.execute(
        """
        SELECT uid, tier, reasoning
        FROM sifting_label_v2
        WHERE session_id = (SELECT MAX(id) FROM sifting_session_v2)
        """
    ).fetchall()
    con.close()
    print(f"Loaded {len(rows)} sift labels.")

    contact_queued = []
    triage_rejected = []
    skipped = []
    errored = []

    for uid, tier, reasoning in rows:
        if tier in {"best", "good"}:
            target = "contact_queued"
            target_list = contact_queued
        elif tier in {"bad", "worse"}:
            target = "triage_reject"
            target_list = triage_rejected
        else:
            skipped.append((uid, tier, "unknown_tier"))
            continue

        # Only act if the listing is still in 'new'.
        con = sqlite3.connect(PROD_DB)
        cur = con.execute(
            "SELECT current_status FROM listing WHERE uid = ?",
            (uid,),
        ).fetchone()
        con.close()
        if cur is None:
            skipped.append((uid, tier, "not_in_db"))
            continue
        if cur[0] not in (None, "new"):
            skipped.append((uid, tier, f"already_{cur[0]}"))
            continue

        reason = infer_reason(reasoning) if target == "triage_reject" else "from_sift"
        notes = (reasoning or "").strip() or None
        try:
            transition(
                uid=uid,
                to_status=target,
                reason=reason,
                notes=notes,
                transitioned_by="sift_v1_bootstrap",
                db_path=str(PROD_DB),
            )
            target_list.append((uid, tier, reason))
        except IllegalTransition as exc:
            errored.append((uid, tier, str(exc)))

    print()
    print(f"contact_queued: {len(contact_queued)}")
    for uid, tier, reason in contact_queued:
        print(f"  {uid:30s} (tier={tier})")
    print()
    print(f"triage_reject:  {len(triage_rejected)}")
    for uid, tier, reason in triage_rejected:
        print(f"  {uid:30s} (tier={tier} reason={reason})")
    print()
    if skipped:
        print(f"skipped: {len(skipped)}")
        for uid, tier, why in skipped:
            print(f"  {uid:30s} ({why})")
    if errored:
        print(f"errored: {len(errored)}")
        for uid, tier, exc in errored:
            print(f"  {uid:30s} ({exc})")


if __name__ == "__main__":
    main()
