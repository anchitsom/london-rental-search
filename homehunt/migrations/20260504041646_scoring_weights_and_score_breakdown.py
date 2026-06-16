"""
Migration 20260504041646: scoring_weights table + score_breakdown column on listing.

Revision id: 20260504041646
Created at:  2026-05-04T04:16:46Z

Changes:
  - CREATE TABLE scoring_weights (id, created_at, source, weights_json,
    fit_metrics_json, notes)
  - INSERT seed row into scoring_weights with the original hard-coded WEIGHTS
    dict from homehunt/scorer.py before it was refactored to be table-driven.
  - ALTER TABLE listing ADD COLUMN score_breakdown TEXT NULL

Apply:
    python homehunt/migrations/20260504041646_scoring_weights_and_score_breakdown.py <db_path>

The script is idempotent: it checks for existing tables/columns before
creating or altering them, so it is safe to run twice.
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone


REVISION = "20260504041646"

# Seed weights captured from homehunt/scorer.py WEIGHTS constant before
# the table-driven refactor was applied.
SEED_WEIGHTS = {
    "price": 0.25,
    "carpet": 0.20,
    "size": 0.15,
    "bedrooms": 0.15,
    "epc": 0.10,
    "transport": 0.10,
    "outside": 0.05,
}


def upgrade(db_path: str) -> None:
    """Apply the migration."""
    conn = sqlite3.connect(db_path)
    try:
        _create_scoring_weights_table(conn)
        _insert_seed_row(conn)
        _add_score_breakdown_column(conn)
        conn.commit()
        print(f"[{REVISION}] upgrade applied to {db_path}")
    finally:
        conn.close()


def _create_scoring_weights_table(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='scoring_weights'"
    ).fetchone()
    if existing:
        print(f"[{REVISION}] scoring_weights table already exists, skipping CREATE")
        return
    conn.execute("""
        CREATE TABLE scoring_weights (
            id              INTEGER PRIMARY KEY,
            created_at      TEXT NOT NULL,
            source          TEXT NOT NULL,
            weights_json    TEXT NOT NULL,
            fit_metrics_json TEXT NULL,
            notes           TEXT NULL
        )
    """)
    print(f"[{REVISION}] created scoring_weights table")


def _insert_seed_row(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT COUNT(*) FROM scoring_weights WHERE source = 'seed'"
    ).fetchone()
    if existing and existing[0] > 0:
        print(f"[{REVISION}] seed row already present, skipping INSERT")
        return
    conn.execute(
        """
        INSERT INTO scoring_weights (created_at, source, weights_json)
        VALUES (?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            "seed",
            json.dumps(SEED_WEIGHTS),
        ),
    )
    print(f"[{REVISION}] inserted seed row into scoring_weights: {SEED_WEIGHTS}")


def _add_score_breakdown_column(conn: sqlite3.Connection) -> None:
    columns = [
        row[1]
        for row in conn.execute("PRAGMA table_info(listing)").fetchall()
    ]
    if "score_breakdown" in columns:
        print(f"[{REVISION}] score_breakdown column already exists on listing, skipping ALTER")
        return
    # listing table may not exist in a fresh test db; only alter if it exists.
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='listing'"
    ).fetchone()
    if not table_exists:
        print(f"[{REVISION}] listing table does not exist yet, skipping ALTER")
        return
    conn.execute("ALTER TABLE listing ADD COLUMN score_breakdown TEXT NULL")
    print(f"[{REVISION}] added score_breakdown column to listing")


def downgrade(db_path: str) -> None:
    """
    SQLite does not support DROP COLUMN before version 3.35.0 and does not
    support DROP TABLE on a table referenced by foreign keys without
    pragma foreign_keys = off. For safety, downgrade drops the whole table
    and leaves score_breakdown in place (SQLite has no simple column removal).
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS scoring_weights")
        conn.commit()
        print(f"[{REVISION}] downgrade applied to {db_path} (scoring_weights dropped)")
        print(f"[{REVISION}] NOTE: score_breakdown column left in place (SQLite limitation)")
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python {__file__} <db_path> [--downgrade]")
        sys.exit(1)
    db_path = sys.argv[1]
    if "--downgrade" in sys.argv:
        downgrade(db_path)
    else:
        upgrade(db_path)
