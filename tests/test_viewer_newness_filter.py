"""
Newness viewer filter tests (Track A, Task 1).

Project convention: plain async-style script, NOT pytest. Each test asserts and
prints PASS / FAIL; run via .venv/bin/python tests/test_viewer_newness_filter.py.

Strategy: build a fixture SQLite db with the Listing schema and insert active
rows whose first_seen ages are 6h, 18h, 2d, 5d, 20d, 60d before "now". Point
HOMEHUNT_DB at it, then call list_listings with each newness bucket and assert
the returned uid set matches the expected window.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_DB = os.path.join(FIXTURES_DIR, "newness.db")

# uid -> hours-ago that first_seen should be set to
AGES_HOURS = {
    "n:6h": 6,
    "n:18h": 18,
    "n:2d": 48,
    "n:5d": 120,
    "n:20d": 480,
    "n:60d": 1440,
}


def _fmt(dt: datetime) -> str:
    # Match production storage: naive-UTC "YYYY-MM-DD HH:MM:SS.ffffff".
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _build_fixture_db(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    from sqlalchemy import create_engine
    from homehunt.core.db import Listing  # noqa: F401  (registers the table)
    from sqlmodel import SQLModel
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()

    now = datetime.utcnow()
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    for uid, hours in AGES_HOURS.items():
        ts = _fmt(now - timedelta(hours=hours))
        # NOT NULL columns without a DB default must all be supplied:
        # property_id, extraction_method, status (distinct from current_status).
        cur.execute(
            "INSERT INTO listing (uid, portal, property_id, url, extraction_method, "
            "status, is_active, current_status, first_seen, last_scraped, scrape_count) "
            "VALUES (?, 'OPENRENT', ?, ?, 'direct_http', 'active', 1, 'new', ?, ?, 1)",
            (uid, uid.split(":")[-1], f"https://example.test/{uid}", ts, ts),
        )
    conn.commit()
    conn.close()


def _uids(result) -> set:
    return {row["uid"] for row in result["listings"]}


def main() -> int:
    _build_fixture_db(FIXTURE_DB)
    os.environ["HOMEHUNT_DB"] = FIXTURE_DB
    from homehunt.viewer_query import list_listings

    cases = {
        "12h": {"n:6h"},
        "24h": {"n:6h", "n:18h"},
        "3d": {"n:6h", "n:18h", "n:2d"},
        "1w": {"n:6h", "n:18h", "n:2d", "n:5d"},
        "1m": {"n:6h", "n:18h", "n:2d", "n:5d", "n:20d"},
    }

    failures = 0
    for bucket, expected in cases.items():
        result = list_listings(
            filters={"tab": "main", "newness": bucket},
            sort="first_seen_desc",
            page=1,
            page_size=50,
        )
        got = _uids(result)
        if got == expected:
            print(f"PASS newness={bucket} -> {sorted(got)}")
        else:
            print(f"FAIL newness={bucket}: expected {sorted(expected)}, got {sorted(got)}")
            failures += 1

    # No newness filter returns all six.
    allr = list_listings(filters={"tab": "main"}, sort="first_seen_desc", page=1, page_size=50)
    if _uids(allr) == set(AGES_HOURS):
        print("PASS no-newness returns all 6")
    else:
        print(f"FAIL no-newness: got {sorted(_uids(allr))}")
        failures += 1

    # Unknown bucket value is ignored (returns all), not an error.
    bad = list_listings(filters={"tab": "main", "newness": "bogus"}, sort="first_seen_desc", page=1, page_size=50)
    if _uids(bad) == set(AGES_HOURS):
        print("PASS unknown-newness ignored")
    else:
        print(f"FAIL unknown-newness: got {sorted(_uids(bad))}")
        failures += 1

    print("ALL PASS" if failures == 0 else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
