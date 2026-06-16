"""
OpenRent Viewer Integration - Task 5: real-DB smoke against data/homehunt.db.

Asserts the live data has at least one OpenRent row that renders cleanly in
the viewer. No counts because real-data counts churn over time; just
presence + 200 status + the detail page for one OpenRent uid.
"""

import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

REAL_DB = PROJECT_ROOT / "data" / "homehunt.db"
assert REAL_DB.exists(), f"production DB missing at {REAL_DB}"
os.environ["HOMEHUNT_DB"] = str(REAL_DB)


def main() -> int:
    from fastapi.testclient import TestClient
    from homehunt import api as api_module
    client = TestClient(api_module.app)

    # /viewer with source=openrent
    resp = client.get("/viewer?per_page=20&sort=last_scraped_desc&source=openrent")
    assert resp.status_code == 200, f"real-DB /viewer?source=openrent returned {resp.status_code}"
    html = resp.text
    chips = html.count('class="chip chip--source">OPENRENT')
    assert chips >= 1, f"real DB has no OpenRent rows visible in viewer (card chips={chips})"

    # Detail page for the most recently-scraped OpenRent row
    conn = sqlite3.connect(REAL_DB)
    row = conn.execute(
        "SELECT uid FROM listing WHERE portal = 'OPENRENT' "
        "ORDER BY last_scraped DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row, "no OpenRent rows in production DB - run the ad-hoc cron first"
    uid = row[0]

    detail = client.get(f"/listing/{uid}")
    assert detail.status_code == 200, (
        f"real-DB /listing/{uid} returned {detail.status_code}\n"
        f"body tail: {detail.text[-1500:]}"
    )
    print(f"PASS - real DB shows {chips} OpenRent cards via ?source=openrent; "
          f"/listing/{uid} renders 200")
    return 0


if __name__ == "__main__":
    sys.exit(main())
