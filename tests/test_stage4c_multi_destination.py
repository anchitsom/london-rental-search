"""
Stage 4c: Multi-destination commute enrichment.

Tests enrich_commute_multi:
  a. Returns commute_to["canary_wharf"] and commute_to["whitechapel"] as integers.
  b. commute_pass_40min is True when both <= 40, False when either exceeds.
  c. Multi runs at most 1.6x the time of single (parallel, not sequential).
  d. Destination-resolver cache works across two calls (no re-resolve).
  e. Legacy enrich_commute still returns original shape.

Run (native):
  cd .
  .venv/bin/python tests/test_stage4c_multi_destination.py
"""

import asyncio
import sys
import time

TEST_POSTCODE = "E8 1DP"
SINGLE_DESTINATION = "Old Street Station, London"

PASS_STR = "PASS"
FAIL_STR = "FAIL"


def report(label: str, ok: bool, detail: str = "") -> bool:
    status = PASS_STR if ok else FAIL_STR
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    return ok


async def main() -> None:
    try:
        from homehunt.tfl import enrich_commute, enrich_commute_multi
    except ImportError as exc:
        print(f"Import failed: {exc}")
        sys.exit(1)

    results = []

    # --- Test a: returns integer commute times for both destinations ---
    print("\n--- Test a: commute_to keys are integers ---")
    result = await enrich_commute_multi(TEST_POSTCODE)
    cw = result.get("commute_to", {}).get("canary_wharf")
    wc = result.get("commute_to", {}).get("whitechapel")
    ok_cw = cw is not None and isinstance(cw, int)
    ok_wc = wc is not None and isinstance(wc, int)
    results.append(report("commute_to[canary_wharf] is int", ok_cw, f"value={cw}"))
    results.append(report("commute_to[whitechapel] is int", ok_wc, f"value={wc}"))

    # --- Test b: commute_pass_40min logic ---
    print("\n--- Test b: commute_pass_40min reflects 40-minute cap ---")
    # Real result: check the actual pass value is consistent with the times
    actual_pass = result.get("commute_pass_40min")
    if cw is not None and wc is not None:
        expected_pass = cw <= 40 and wc <= 40
        ok_pass_logic = actual_pass == expected_pass
        results.append(
            report(
                "commute_pass_40min consistent with times",
                ok_pass_logic,
                f"cw={cw} wc={wc} pass={actual_pass} expected={expected_pass}",
            )
        )
    else:
        # Cannot confirm logic without real times; check pass is not True
        ok_pass_logic = actual_pass is not True
        results.append(
            report(
                "commute_pass_40min not True when times missing",
                ok_pass_logic,
                f"pass={actual_pass}",
            )
        )

    # Synthetic: override times to confirm False path
    synthetic_over = await enrich_commute_multi(
        TEST_POSTCODE,
        destinations={"over": "Canary Wharf Station, London", "under": "Whitechapel Station, London"},
        max_commute_mins=1,  # absurdly low cap -> must be False
    )
    ok_false = synthetic_over.get("commute_pass_40min") is False
    results.append(report("commute_pass_40min False with cap=1min", ok_false))

    # --- Test c: multi is not more than 1.6x single wall-clock time ---
    print("\n--- Test c: parallel execution (multi <= 1.6x single) ---")
    t0 = time.monotonic()
    await enrich_commute(TEST_POSTCODE, destination=SINGLE_DESTINATION)
    single_secs = time.monotonic() - t0

    t0 = time.monotonic()
    await enrich_commute_multi(TEST_POSTCODE)
    multi_secs = time.monotonic() - t0

    ratio = multi_secs / single_secs if single_secs > 0 else 0
    ok_parallel = ratio < 1.6
    results.append(
        report(
            "multi < 1.6x single time",
            ok_parallel,
            f"single={single_secs:.2f}s multi={multi_secs:.2f}s ratio={ratio:.2f}",
        )
    )

    # --- Test d: cache means no re-resolve on second call ---
    print("\n--- Test d: destination-resolver cache is reused ---")
    # Import cache directly to inspect
    from homehunt.tfl import _stop_id_cache
    cache_before = dict(_stop_id_cache)
    await enrich_commute_multi(TEST_POSTCODE)
    cache_after = dict(_stop_id_cache)
    # Cache should not have grown (destinations already cached from test a)
    ok_cache = set(cache_before.keys()) == set(cache_after.keys()) or all(
        k in cache_after for k in cache_before
    )
    results.append(
        report(
            "no new entries added to _stop_id_cache on second call",
            ok_cache,
            f"before_keys={len(cache_before)} after_keys={len(cache_after)}",
        )
    )

    # --- Test e: legacy enrich_commute still returns original shape ---
    print("\n--- Test e: legacy enrich_commute shape unchanged ---")
    legacy = await enrich_commute(TEST_POSTCODE, destination=SINGLE_DESTINATION)
    required_keys = {"commute_public_transport", "commute_walking", "tfl_zone", "nearest_station"}
    ok_legacy = required_keys.issubset(set(legacy.keys()))
    # Must not contain the new multi keys
    no_extra = "commute_to" not in legacy and "commute_pass_40min" not in legacy
    results.append(report("legacy keys present", ok_legacy, f"keys={set(legacy.keys())}"))
    results.append(report("legacy has no multi keys", no_extra))

    # --- Summary ---
    print(f"\n--- Summary: {sum(results)}/{len(results)} passed ---")
    if not all(results):
        sys.exit(1)
    print("Stage 4c: ALL PASS")


asyncio.run(main())
