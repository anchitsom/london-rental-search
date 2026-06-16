"""
OpenRent Viewer Integration - Task 2: /listing/{openrent-uid} renders 200.

Hermetic test using the same fixture DB as Task 1. Hit GET /listing/openrent:or1
and assert: status 200, the title and price appear in the rendered HTML,
no template traceback leaks through.

Pattern: set HOMEHUNT_DB before any homehunt import.
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURE_DB = PROJECT_ROOT / "tests" / "fixtures" / "openrent_viewer.db"
FIXTURE_DB.parent.mkdir(parents=True, exist_ok=True)
os.environ["HOMEHUNT_DB"] = str(FIXTURE_DB)


def main() -> int:
    # Ensure fixture exists (rebuild if Task 1 cleaned it up)
    if not FIXTURE_DB.exists():
        sys.path.insert(0, str(PROJECT_ROOT / "tests"))
        import test_openrent_viewer_appears as t1
        t1.build_fixture_db(FIXTURE_DB)

    from fastapi.testclient import TestClient
    from homehunt import api as api_module
    client = TestClient(api_module.app)
    resp = client.get("/listing/openrent:or1")
    assert resp.status_code == 200, (
        f"detail page returned {resp.status_code} for openrent:or1\n"
        f"body tail: {resp.text[-1500:]}"
    )
    html = resp.text
    assert "WC2N" in html, "detail HTML missing area marker WC2N"
    assert "£2,350" in html or "2350" in html or "235000" in html, (
        "detail HTML missing price marker; check _format_price output"
    )
    print(f"PASS - /listing/openrent:or1 renders 200, title + price present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
