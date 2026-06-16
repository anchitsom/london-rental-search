"""
OpenRent Viewer - lifecycle smoke.

The /listing/{uid}/transition endpoint is generic across portals. Verify it
accepts an OpenRent uid and writes a row to listing_lifecycle.

Pattern: set HOMEHUNT_DB before any homehunt import.
"""

import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURE_DB = PROJECT_ROOT / "tests" / "fixtures" / "openrent_viewer.db"
os.environ["HOMEHUNT_DB"] = str(FIXTURE_DB)


def main() -> int:
    if not FIXTURE_DB.exists():
        sys.path.insert(0, str(PROJECT_ROOT / "tests"))
        import test_openrent_viewer_appears as t1
        t1.build_fixture_db(FIXTURE_DB)

    from fastapi.testclient import TestClient
    from homehunt import api as api_module
    client = TestClient(api_module.app)

    uid = "openrent:or1"

    # Transition new -> contact_queued (the documented valid first step per
    # the CRM lifecycle in homehunt/lifecycle.py).
    resp = client.post(
        f"/listing/{uid}/transition",
        data={"to_status": "contact_queued"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, (
        f"transition returned {resp.status_code}; body: {resp.text[:500]}"
    )

    # Confirm the audit row landed in listing_lifecycle.
    con = sqlite3.connect(FIXTURE_DB)
    rows = con.execute(
        "SELECT to_status FROM listing_lifecycle WHERE uid = ? ORDER BY id DESC LIMIT 1",
        (uid,),
    ).fetchall()
    con.close()
    assert rows, f"no audit row in listing_lifecycle for {uid}"
    assert rows[0][0] == "contact_queued", f"audit row has wrong status {rows[0][0]}"

    # Confirm the listing's current_status flipped.
    con = sqlite3.connect(FIXTURE_DB)
    row = con.execute(
        "SELECT current_status FROM listing WHERE uid = ?", (uid,)
    ).fetchone()
    con.close()
    assert row and row[0] == "contact_queued", (
        f"listing.current_status not updated: {row}"
    )

    print(f"PASS - {uid} transitioned new->contact_queued, audit row written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
