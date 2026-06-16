"""
OpenRent Integration - Task 10: field-coverage report on all OpenRent rows in the DB.

REQUIRED fields must be 100% (hard assertion).
OPTIONAL fields are reported but not asserted - they show the actual delivered
coverage so the integration owner can see where OpenRent falls short of
Rightmove/Zoopla. Enrichment fields (epc_rating, commute_*, score, region,
cluster_id) are expected to be 0% on the pilot since enrichment is deliberately
deferred (see plan Open Questions).
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("HOMEHUNT_DB", str(PROJECT_ROOT / "data" / "homehunt.db"))


REQUIRED = [
    "uid", "portal", "property_id", "url", "title",
    "price_numeric", "bedrooms", "area",
    "latitude", "longitude",
    "first_seen", "last_scraped", "is_active",
]

OPTIONAL_SCRAPER = [
    "address", "postcode", "bathrooms",
    "furnished", "garden", "bills_included",
    "let_available_date", "images",
]

OPTIONAL_ENRICHMENT = [
    "tube_distance", "cycle_canary_wharf",
    "epc_rating", "tfl_zone", "nearest_station",
    "commute_canary_wharf", "commute_whitechapel", "commute_pass_40min",
    "score", "region", "cluster_id",
    "carpet_in_bedroom", "carpet_other_areas",
    "is_let_agreed",
]


def main() -> int:
    from sqlmodel import Session, select
    from homehunt.core.db import Database, Listing
    from homehunt.core.models import Portal
    db = Database()
    with Session(db.engine) as s:
        rows = s.exec(select(Listing).where(Listing.portal == Portal.OPENRENT)).all()
    assert rows, "no OpenRent rows in the DB; run the ad-hoc cron first (Task 8 or 9)"
    n = len(rows)

    print(f"\nOpenRent field-coverage report over {n} rows")
    print("=" * 72)

    print("\nREQUIRED fields (must be 100%):")
    failures = []
    for f in REQUIRED:
        present = sum(1 for r in rows if getattr(r, f) not in (None, ""))
        pct = 100 * present / n
        flag = "OK" if present == n else "FAIL"
        print(f"  [{flag:4}] {f:30s} {present:4d}/{n} ({pct:5.1f}%)")
        if present != n:
            failures.append(f)

    print("\nOPTIONAL fields - scraper outputs (informational):")
    for f in OPTIONAL_SCRAPER:
        present = sum(1 for r in rows if getattr(r, f) not in (None, ""))
        pct = 100 * present / n
        print(f"         {f:30s} {present:4d}/{n} ({pct:5.1f}%)")

    print("\nOPTIONAL fields - enrichment (expected 0% on pilot, deferred):")
    for f in OPTIONAL_ENRICHMENT:
        present = sum(1 for r in rows if getattr(r, f) not in (None, "", False))
        pct = 100 * present / n
        print(f"         {f:30s} {present:4d}/{n} ({pct:5.1f}%)")

    if failures:
        print(f"\nFAIL - REQUIRED fields below 100%: {failures}")
        return 1
    print(f"\nPASS - all REQUIRED fields at 100% across {n} OpenRent rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
