"""
Stage 4b: TfL Journey Planner with NaPTAN StopPoint resolution.

Tests that destinations are resolved to unambiguous StopPoint IDs before
Journey Planner requests, eliminating 300 Multiple Choices errors.

Run inside container:
  PATH="/opt/homebrew/bin:$PATH" docker exec -e PYTHONPATH=/app rental-engine \
    python3 tests/test_stage4b_tfl_journey.py
"""

import asyncio
import sys

TEST_DESTINATION_REAL = "Old Street Station, London"
TEST_DESTINATION_FAKE = "This Is Not A Real Place XYZ"
TEST_POSTCODES = ["E8 1DP", "N4 2DR", "EC1A 1BB"]

PASS = "PASS"
FAIL = "FAIL"


def report(label: str, ok: bool, detail: str = "") -> bool:
    status = PASS if ok else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    return ok


async def main() -> None:
    try:
        from homehunt.tfl import enrich_commute, resolve_destination_to_stopid
    except ImportError as exc:
        print(f"Import failed: {exc}")
        sys.exit(1)

    results = []

    print("\n--- Test 1: resolve real destination returns a NaPTAN ID ---")
    stop_id = await resolve_destination_to_stopid(TEST_DESTINATION_REAL)
    ok = stop_id is not None and isinstance(stop_id, str) and len(stop_id) > 0
    results.append(report("resolve_destination_to_stopid (real)", ok, f"id={stop_id}"))

    print("\n--- Test 2: resolve fake destination returns None ---")
    fake_id = await resolve_destination_to_stopid(TEST_DESTINATION_FAKE)
    ok = fake_id is None
    results.append(report("resolve_destination_to_stopid (fake)", ok, f"returned={fake_id!r}"))

    print(f"\n--- Test 3: enrich_commute('E8 1DP') returns non-None journey time ---")
    result = await enrich_commute("E8 1DP", destination=TEST_DESTINATION_REAL)
    pt = result.get("commute_public_transport")
    ok = pt is not None and isinstance(pt, int)
    results.append(report("enrich_commute E8 1DP", ok, f"commute_public_transport={pt}"))

    print("\n--- Test 4: at least one of N4 2DR, EC1A 1BB returns non-None journey time ---")
    extra_results = []
    for postcode in ["N4 2DR", "EC1A 1BB"]:
        r = await enrich_commute(postcode, destination=TEST_DESTINATION_REAL)
        pt2 = r.get("commute_public_transport")
        print(f"  {postcode}: commute_public_transport={pt2}")
        extra_results.append(pt2)
    ok = any(v is not None and isinstance(v, int) for v in extra_results)
    results.append(report("at least one extra postcode", ok))

    print(f"\n--- Summary: {sum(results)}/{len(results)} passed ---")
    if not all(results):
        sys.exit(1)
    print("Stage 4b: ALL PASS")


asyncio.run(main())
