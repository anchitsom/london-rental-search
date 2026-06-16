"""
OpenRent Probe — Task 2: plain httpx + Chrome User-Agent reaches OpenRent search, no Cloudflare wall.

Run: python tests/test_openrent_probe_fetch_search.py
Pass: status 200, body > 500_000 bytes, PROPERTYIDS string appears in body, fixture written.
Fail: any non-200, or PROPERTYIDS missing (means architecture shifted again).
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXP = PROJECT_ROOT / "experiments" / "openrent-probe"

spec = importlib.util.spec_from_file_location("openrent_probe_fetch", EXP / "probe" / "fetch.py")
fm = importlib.util.module_from_spec(spec); spec.loader.exec_module(fm)


async def main() -> int:
    url = "https://www.openrent.co.uk/properties-to-rent/london?term=London&prices_min=1500&prices_max=2800&bedrooms_min=1&bedrooms_max=2"
    status, body = await fm.fetch_search(url)
    fixture = EXP / "fixtures" / "search-page-0.html"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(body)
    assert status == 200, f"expected 200, got {status}"
    assert len(body) > 500_000, f"body too short ({len(body)} bytes); something is wrong"
    assert "PROPERTYIDS" in body, "PROPERTYIDS string missing from body; architecture may have shifted"
    print(f"PASS — status={status}, {len(body)} bytes, PROPERTYIDS present")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
