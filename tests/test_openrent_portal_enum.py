"""
OpenRent Integration - Task 1: Portal enum has OPENRENT value and the Listing
model accepts it (constructor + assignment, no DB round-trip needed).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from homehunt.core.models import Portal
from homehunt.core.db import Listing


def main() -> int:
    assert hasattr(Portal, "OPENRENT"), "Portal enum missing OPENRENT"
    assert Portal.OPENRENT.value == "openrent", f"Portal.OPENRENT.value={Portal.OPENRENT.value!r}"

    # Listing model accepts portal='openrent' at construction (no commit needed)
    row = Listing(
        uid="openrent-2898832",
        portal="openrent",
        property_id="2898832",
        url="https://www.openrent.co.uk/2898832",
        first_seen=datetime.now(timezone.utc),
        last_scraped=datetime.now(timezone.utc),
    )
    assert row.portal == "openrent", f"Listing.portal stored as {row.portal!r}"
    assert row.uid == "openrent-2898832"

    print(f"PASS - Portal.OPENRENT exists and Listing accepts portal='openrent'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
