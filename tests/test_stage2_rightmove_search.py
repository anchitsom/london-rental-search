"""
Stage 2: Test the httpx-based Rightmove discovery layer using explicit region IDs.

Mirrors the SEARCH_PROFILES in scrape.py — verifies each region ID returns results.

Run inside container: python tests/test_stage2_rightmove_search.py
"""

import asyncio
import sys

# Region IDs matching scrape.py SEARCH_PROFILES exactly
REGION_TESTS = [
    ("Shoreditch",      "REGION^87490"),
    ("Islington",       "REGION^85279"),
    ("Canonbury",       "REGION^85331"),
    ("Stoke Newington", "REGION^87498"),
    ("Farringdon",      "REGION^87487"),
]


async def main():
    sys.path.insert(0, "/app")
    from homehunt.scrapers.rightmove_api import discover_properties

    print("=== Region ID discovery test ===")
    print(f"{'Location':<20} {'Region ID':<18} {'URLs found'}")
    print("-" * 55)

    for location, region_id in REGION_TESTS:
        urls = await discover_properties(
            location=location,
            location_id=region_id,
            min_price=1600,
            max_price=2800,
            min_bedrooms=1,
            max_bedrooms=2,
            radius=1.0,
            max_results=24,
        )
        status = "OK" if urls else "FAIL — 0 results"
        print(f"  {location:<18} {region_id:<18} {len(urls):>3}  [{status}]")
        if urls:
            print(f"    sample: {urls[0]}")

    print("\nStage 2: DONE")


asyncio.run(main())
