"""
Stage 4: TfL commute enrichment — isolated test.
No API key required for the free tier (500 req/min).
Tests postcodes.io lookup, nearest station, and journey planner.

Run inside container: python tests/test_stage4_tfl.py
"""

import asyncio
import sys

# Dalston area postcodes for testing
TEST_POSTCODES = ["E8 1DP", "N4 2DR", "EC1A 1BB"]
TEST_DESTINATION = "Old Street Station, London"


async def main():
    try:
        from homehunt.tfl import enrich_commute
    except ImportError as e:
        print(f"Import failed: {e}")
        sys.exit(1)

    for postcode in TEST_POSTCODES:
        print(f"\n--- {postcode} ---")
        try:
            result = await enrich_commute(postcode, destination=TEST_DESTINATION)
            print(f"  nearest_station:         {result['nearest_station']}")
            print(f"  commute_walking (mins):  {result['commute_walking']}")
            print(f"  tfl_zone:                {result['tfl_zone']}")
            print(f"  commute_public_transport:{result['commute_public_transport']}")
        except Exception as e:
            print(f"  FAILED: {e}")

    print("\nStage 4: DONE")


asyncio.run(main())
