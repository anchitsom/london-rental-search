"""Backfill listing.let_available_iso from listing.let_available_date."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_normaliser() -> Callable[[object, date], str | None]:
    sys.path.insert(0, str(PROJECT_ROOT))
    from homehunt.feature_extractor import normalise_let_available_date

    return normalise_let_available_date


def main() -> int:
    db = os.environ.get("HOMEHUNT_DB")
    if not db:
        print("error: HOMEHUNT_DB env var is required", file=sys.stderr)
        return 2

    normalise_let_available_date = _load_normaliser()
    today = date.today()
    con = sqlite3.connect(db)

    try:
        rows = con.execute(
            "SELECT uid, let_available_date FROM listing "
            "WHERE let_available_iso IS NULL AND let_available_date IS NOT NULL"
        ).fetchall()

        parsed = 0
        unparsed = 0
        updates: list[tuple[str, str]] = []

        for uid, raw in rows:
            try:
                iso = normalise_let_available_date(raw, today)
            except Exception:
                iso = None
            if iso is not None:
                updates.append((iso, uid))
                parsed += 1
            else:
                updates.append(("", uid))
                unparsed += 1

        con.executemany(
            "UPDATE listing SET let_available_iso = ? WHERE uid = ?",
            updates,
        )
        con.commit()
    finally:
        con.close()

    print(f"backfill complete: parsed={parsed} unparsed={unparsed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
