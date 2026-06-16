"""OpenRent ad-hoc runner.

Bypasses london-search.yaml and invokes run.run_pipeline with a single
OpenRent-specific profile holding centroids (Angel, Camden Town, Hackney
Central) and a Haversine radius. Triggered from scripts/openrent-adhoc-run.sh
or via launchctl kickstart gui/$UID/com.anchitsom.openrent-adhoc.

OPENRENT_MAX_LISTINGS env (read inside _run_openrent_pipeline) caps the
per-run detail-fetch count; sensible default 10 for a smoke, override for
larger runs.
"""

import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Centroids correspond to existing target boroughs from london-search.yaml.
PILOT_PROFILE = {
    "name": "openrent-pilot",
    "search_url": (
        "https://www.openrent.co.uk/properties-to-rent/london?term=London"
        "&prices_min=1500&prices_max=2800&bedrooms_min=1&bedrooms_max=2"
    ),
    "centroids": [
        (51.5326, -0.1058),  # Angel / Islington
        (51.5390, -0.1426),  # Camden Town
        (51.5450, -0.0553),  # Hackney Central
    ],
    "radius_km": 2.0,
}


async def _main() -> int:
    os.environ["ENABLED_PORTALS"] = "openrent"
    import run as _run
    summary = await _run.run_pipeline(
        profiles_override=[PILOT_PROFILE],
        enabled_portals=["openrent"],
    )
    print(f"\nopenrent-adhoc complete: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
