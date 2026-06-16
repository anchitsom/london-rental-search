"""
Stage 6: TfL Journey Planner repair tests.

Tests four cases that expose the root cause of commute_canary_wharf being NULL
for most listings:

  1. Central London postcode (EC1Y 1AA): commute to Canary Wharf under 30 minutes.
  2. Outer-zone postcode (IG11 7QR, Zone 4): commute over 40 minutes.
  3. Terminated postcode (N16 9BQ): postcodes.io returns 404 but still has lat/lon
     in the terminated record. Must return a valid commute, not None.
  4. Invalid postcode (ZZ99 9ZZ): no coords anywhere, returns None and emits a
     structured log miss.

Root cause: TfL Journey Planner returns HTTP 300 (disambiguation) when the
origin is a terminated postcode string that TfL cannot locate. The fix is to
resolve postcode to lat/lon via postcodes.io first (including the
terminated-postcode fallback in the 404 body), then pass coordinates as
the journey origin.

Run (native):
  cd .
  .venv/bin/python tests/test_stage6_tfl.py

Run (from worktree with project venv):
  PYTHONPATH=/tmp/rental-engine-wave1-C \\
    ./.venv/bin/python3 \\
    tests/test_stage6_tfl.py
"""

import asyncio
import sys

PASS_STR = "PASS"
FAIL_STR = "FAIL"


def report(label: str, ok: bool, detail: str = "") -> bool:
    status = PASS_STR if ok else FAIL_STR
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    return ok


async def main() -> None:
    try:
        from homehunt.tfl import enrich_commute_multi
    except ImportError as exc:
        print(f"Import failed: {exc}")
        sys.exit(1)

    results = []

    # --- Test 1: Central London postcode, commute under 30 minutes ---
    print("\n--- Test 1: EC1Y 1AA -> Canary Wharf, expect < 30 min ---")
    result1 = await enrich_commute_multi(
        "EC1Y 1AA",
        destinations={"canary_wharf": "Canary Wharf Station, London"},
    )
    mins1 = result1.get("commute_to", {}).get("canary_wharf")
    ok1_not_none = mins1 is not None
    ok1_under_30 = isinstance(mins1, int) and mins1 < 30
    results.append(report("EC1Y 1AA commute not None", ok1_not_none, f"value={mins1}"))
    results.append(
        report("EC1Y 1AA commute < 30 min", ok1_under_30, f"got {mins1} min")
    )

    # --- Test 2: Outer-zone postcode, commute over 40 minutes ---
    print("\n--- Test 2: IG11 7QR -> Canary Wharf, expect > 40 min ---")
    result2 = await enrich_commute_multi(
        "IG11 7QR",
        destinations={"canary_wharf": "Canary Wharf Station, London"},
    )
    mins2 = result2.get("commute_to", {}).get("canary_wharf")
    ok2_not_none = mins2 is not None
    ok2_over_40 = isinstance(mins2, int) and mins2 > 40
    results.append(report("IG11 7QR commute not None", ok2_not_none, f"value={mins2}"))
    results.append(
        report("IG11 7QR commute > 40 min", ok2_over_40, f"got {mins2} min")
    )

    # --- Test 3: Terminated postcode with available coords ---
    # N16 9BQ is terminated (postcodes.io returns 404) but its 404 body contains
    # lat/lon. The pre-fix code passes the raw postcode to TfL Journey Planner
    # which returns HTTP 300, causing the commute to come back as None.
    # The fix: extract lat/lon from the 404 terminated body and use coordinates.
    print("\n--- Test 3: N16 9BQ (terminated postcode) -> Canary Wharf, expect non-None ---")
    result3 = await enrich_commute_multi(
        "N16 9BQ",
        destinations={"canary_wharf": "Canary Wharf Station, London"},
    )
    mins3 = result3.get("commute_to", {}).get("canary_wharf")
    ok3_not_none = mins3 is not None
    ok3_is_int = isinstance(mins3, int)
    results.append(
        report("N16 9BQ commute not None (terminated postcode fixed)", ok3_not_none, f"value={mins3}")
    )
    results.append(
        report("N16 9BQ commute is int", ok3_is_int, f"type={type(mins3).__name__}")
    )

    # --- Test 4: Invalid postcode, returns None and logs a miss ---
    print("\n--- Test 4: ZZ99 9ZZ -> Canary Wharf, expect None ---")
    result4 = await enrich_commute_multi(
        "ZZ99 9ZZ",
        destinations={"canary_wharf": "Canary Wharf Station, London"},
    )
    mins4 = result4.get("commute_to", {}).get("canary_wharf")
    ok4_none = mins4 is None
    results.append(
        report("ZZ99 9ZZ commute is None (invalid postcode)", ok4_none, f"value={mins4}")
    )

    # --- Summary ---
    print(f"\n--- Summary: {sum(results)}/{len(results)} passed ---")
    if not all(results):
        sys.exit(1)
    print("Stage 6 TfL: ALL PASS")


asyncio.run(main())
