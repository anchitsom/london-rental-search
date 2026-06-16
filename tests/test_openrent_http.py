"""
OpenRent Integration - Task 3: HTTP client fetches a real search + detail with
the project rate-limiter, returns (status, body) tuples.
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from homehunt.scrapers.openrent_http import OpenRentHTTP


SEARCH_URL = (
    "https://www.openrent.co.uk/properties-to-rent/london?term=London"
    "&prices_min=1500&prices_max=2800&bedrooms_min=1&bedrooms_max=2"
)


async def main() -> int:
    async with OpenRentHTTP() as client:
        status, body = await client.fetch_search(SEARCH_URL)
        assert status == 200, f"search status {status}"
        assert len(body) > 500_000, f"search body too short ({len(body)}b)"
        assert "PROPERTYIDS" in body, "PROPERTYIDS missing"

        from homehunt.scrapers.openrent_extract import extract_search
        ids = extract_search(body)["property_ids"]
        pid = ids[0]
        status2, body2 = await client.fetch_detail(pid)
        assert status2 == 200, f"detail status {status2}"
        assert len(body2) > 30_000, f"detail body too short ({len(body2)}b)"
    print(f"PASS - search ({len(body)}b) + detail ({len(body2)}b) through rate-limited client")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
