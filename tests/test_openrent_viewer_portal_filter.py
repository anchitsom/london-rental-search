"""
OpenRent Viewer Integration - Task 4: ?source=openrent narrows to OpenRent only.

Hermetic test using the same fixture DB as Tasks 1-2. Hit:
  /viewer?source=openrent  -> only OPENRENT chips
  /viewer?source=rightmove -> only RIGHTMOVE chips
  /viewer                  -> all three portals
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURE_DB = PROJECT_ROOT / "tests" / "fixtures" / "openrent_viewer.db"
os.environ["HOMEHUNT_DB"] = str(FIXTURE_DB)


def _chip_counts(html: str) -> dict[str, int]:
    return {
        "RIGHTMOVE": html.count("RIGHTMOVE"),
        "ZOOPLA": html.count("ZOOPLA"),
        "OPENRENT": html.count("OPENRENT"),
    }


def main() -> int:
    if not FIXTURE_DB.exists():
        sys.path.insert(0, str(PROJECT_ROOT / "tests"))
        import test_openrent_viewer_appears as t1
        t1.build_fixture_db(FIXTURE_DB)

    from fastapi.testclient import TestClient
    from homehunt import api as api_module
    client = TestClient(api_module.app)

    # Unfiltered: all three portals visible
    r_all = client.get("/viewer?per_page=50&sort=last_scraped_desc")
    assert r_all.status_code == 200, f"unfiltered status {r_all.status_code}"
    counts_all = _chip_counts(r_all.text)
    assert counts_all["RIGHTMOVE"] >= 3, f"unfiltered RM missing: {counts_all}"
    assert counts_all["ZOOPLA"] >= 3, f"unfiltered ZP missing: {counts_all}"
    assert counts_all["OPENRENT"] >= 3, f"unfiltered OR missing: {counts_all}"

    # OpenRent only — the chip-filter row may include "OPENRENT" once for its
    # own link, so we count cards by looking for the card-source-chip pattern.
    r_or = client.get("/viewer?per_page=50&sort=last_scraped_desc&source=openrent")
    assert r_or.status_code == 200, f"?source=openrent status {r_or.status_code}"
    or_card_chips = r_or.text.count('class="chip chip--source">OPENRENT')
    or_rm_card_chips = r_or.text.count('class="chip chip--source">RIGHTMOVE')
    or_zp_card_chips = r_or.text.count('class="chip chip--source">ZOOPLA')
    assert or_card_chips >= 3, f"?source=openrent missing OR card chips ({or_card_chips})"
    assert or_rm_card_chips == 0, f"?source=openrent leaked RM card chips ({or_rm_card_chips})"
    assert or_zp_card_chips == 0, f"?source=openrent leaked ZP card chips ({or_zp_card_chips})"

    # Rightmove only
    r_rm = client.get("/viewer?per_page=50&sort=last_scraped_desc&source=rightmove")
    assert r_rm.status_code == 200, f"?source=rightmove status {r_rm.status_code}"
    rm_card_chips = r_rm.text.count('class="chip chip--source">RIGHTMOVE')
    rm_or_card_chips = r_rm.text.count('class="chip chip--source">OPENRENT')
    assert rm_card_chips >= 3, f"?source=rightmove missing RM card chips ({rm_card_chips})"
    assert rm_or_card_chips == 0, f"?source=rightmove leaked OR card chips ({rm_or_card_chips})"

    print(f"PASS - portal filter narrows correctly: all={counts_all}, "
          f"or-only cards={or_card_chips}, rm-only cards={rm_card_chips}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
