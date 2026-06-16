"""
OpenRent Integration - Task 5: discover_openrent_properties does one search GET,
extracts PROPERTYIDS + parallel lat/lng, prunes to within 2km of a known
Islington centroid, and returns a list of (id, lat, lng) tuples.
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from homehunt.scrapers.openrent_discover import discover_openrent_properties

# Angel/Islington centroid
ISLINGTON_LAT, ISLINGTON_LNG = 51.5326, -0.1058


async def main() -> int:
    candidates = await discover_openrent_properties(
        search_url=(
            "https://www.openrent.co.uk/properties-to-rent/london?term=London"
            "&prices_min=1500&prices_max=2800&bedrooms_min=1&bedrooms_max=2"
        ),
        centroids=[(ISLINGTON_LAT, ISLINGTON_LNG)],
        radius_km=2.0,
    )
    assert candidates, "no candidates returned"
    assert all(isinstance(c, tuple) and len(c) == 3 for c in candidates), "expected (id, lat, lng) tuples"
    assert all(isinstance(c[0], int) for c in candidates), "ids must be int"
    assert len(candidates) < 5000, f"geo-prune returned {len(candidates)}; not pruning"
    assert len(candidates) > 5, f"only {len(candidates)} listings near Islington; suspicious"
    print(f"PASS - geo-pruned to {len(candidates)} candidates within 2km of Islington")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
