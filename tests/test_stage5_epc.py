"""
Stage 5: EPC rating lookup — isolated test.
Requires EPC_EMAIL and EPC_API_KEY env vars (free registration at
https://epc.opendatacommunities.org/).

If credentials are not set, the test confirms graceful degradation.

Run inside container: python tests/test_stage5_epc.py
"""

import asyncio
import os
import sys

TEST_POSTCODES = ["E8 1DP", "N4 2DR", "EC1A 1BB"]


async def main():
    try:
        from homehunt.epc import get_epc_rating, score_epc, EPC_EMAIL, EPC_API_KEY
    except ImportError as e:
        print(f"Import failed: {e}")
        sys.exit(1)

    if not EPC_EMAIL or not EPC_API_KEY:
        print("EPC credentials not set (EPC_EMAIL / EPC_API_KEY).")
        print("Testing graceful degradation — expect None results.")
    else:
        print(f"Credentials set: email={EPC_EMAIL[:4]}***")

    for postcode in TEST_POSTCODES:
        print(f"\n--- {postcode} ---")
        try:
            rating = await get_epc_rating(postcode)
            score = score_epc(rating)
            print(f"  rating: {rating}")
            print(f"  score:  {score}")
        except Exception as e:
            print(f"  FAILED: {e}")

    print("\nStage 5: DONE")
    if not EPC_EMAIL:
        print("Next step: register at https://epc.opendatacommunities.org/")
        print("Set EPC_EMAIL and EPC_API_KEY in ~/.agent-secrets/.env")


asyncio.run(main())
