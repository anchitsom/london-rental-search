"""
Newness route + template tests (Track A, Task 2 + Task 3).

Plain async-style script, NOT pytest. Run via
.venv/bin/python tests/test_viewer_newness_route.py.

Builds a fixture db with rows at 6h and 20d old, points the FastAPI app at it
via HOMEHUNT_DB, and exercises /viewer through TestClient.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_DB = os.path.join(FIXTURES_DIR, "newness_route.db")


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _build_fixture_db(path):
    if os.path.exists(path):
        os.remove(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    from sqlalchemy import create_engine
    from homehunt.core.db import Listing  # noqa: F401
    from sqlmodel import SQLModel
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()

    now = datetime.utcnow()
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    rows = [
        ("n:fresh", 6, "Fresh Flat Highbury"),
        ("n:stale", 480, "Stale Flat Highbury"),  # 20 days
    ]
    for uid, hours, title in rows:
        ts = _fmt(now - timedelta(hours=hours))
        # NOT NULL columns without a DB default must all be supplied:
        # property_id, extraction_method, status (distinct from current_status).
        cur.execute(
            "INSERT INTO listing (uid, portal, property_id, url, title, extraction_method, "
            "status, is_active, current_status, first_seen, last_scraped, scrape_count) "
            "VALUES (?, 'OPENRENT', ?, ?, ?, 'direct_http', 'active', 1, 'new', ?, ?, 1)",
            (uid, uid.split(":")[-1], f"https://example.test/{uid}", title, ts, ts),
        )
    conn.commit()
    conn.close()


def main():
    _build_fixture_db(FIXTURE_DB)
    os.environ["HOMEHUNT_DB"] = FIXTURE_DB
    from fastapi.testclient import TestClient
    from homehunt.api import app

    client = TestClient(app)
    failures = 0

    # 1. newness=24h shows only the fresh listing, not the stale one.
    r = client.get("/viewer?newness=24h")
    if r.status_code == 200 and "Fresh Flat Highbury" in r.text and "Stale Flat Highbury" not in r.text:
        print("PASS newness=24h filters to fresh only")
    else:
        print(f"FAIL newness=24h: status={r.status_code} fresh={'Fresh Flat Highbury' in r.text} stale={'Stale Flat Highbury' in r.text}")
        failures += 1

    # 2. no newness shows both.
    r = client.get("/viewer")
    if "Fresh Flat Highbury" in r.text and "Stale Flat Highbury" in r.text:
        print("PASS no-newness shows both")
    else:
        print("FAIL no-newness should show both")
        failures += 1

    # 3. the newness <select> renders and reflects the active value.
    r = client.get("/viewer?newness=1w")
    if 'name="newness"' in r.text and 'value="1w"' in r.text and "selected" in r.text:
        print("PASS newness dropdown renders with selected value")
    else:
        print("FAIL newness dropdown missing or not selected")
        failures += 1

    # 4. pagination/query-url preserves newness (appears in a build_query link).
    if "newness=1w" in r.text:
        print("PASS newness preserved in query URLs")
    else:
        print("FAIL newness not preserved in query URLs")
        failures += 1

    print("ALL PASS" if failures == 0 else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
