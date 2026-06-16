"""
Test that null EPC produces a measurably lower total score than a confirmed
EPC = "C" rating, with all other component inputs held identical.

Decision 2026-05-04: scoring.components.epc.null_score = 0 (was 50).
This means a survivor with an unmatched EPC sits at the bottom of the EPC
soft-component, so the total score is lower than the same listing with a
confirmed mid-tier rating.

Run:
  .venv/bin/python tests/test_epc_null_scoring.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_null_epc_scores_lower_than_C():
    from homehunt.scorer import compute_score

    base = dict(
        price_pcm=2200,
        size_sqft=700,
        bedrooms=2,
        carpet_detected=False,
        carpet_confidence=0.9,
        commute_mins=20,
        tfl_zone=2,
        garden=True,
        balcony=False,
    )

    total_c, breakdown_c = compute_score(epc_rating="C", **base)
    total_null, breakdown_null = compute_score(epc_rating=None, **base)

    print(f"  EPC=C   total={total_c}, breakdown={breakdown_c}")
    print(f"  EPC=None total={total_null}, breakdown={breakdown_null}")

    # The null case must score strictly lower than C (null_score 0 vs C-score 100).
    if not (total_null < total_c):
        raise AssertionError(
            f"FAIL: total_null ({total_null}) is not < total_c ({total_c})"
        )

    # And the EPC component contribution itself must be 0 for null.
    if breakdown_null.get("epc", -1) != 0:
        raise AssertionError(
            f"FAIL: expected breakdown.epc == 0 for null EPC, got {breakdown_null.get('epc')}"
        )

    # And 100 for C.
    if breakdown_c.get("epc", -1) != 100:
        raise AssertionError(
            f"FAIL: expected breakdown.epc == 100 for C, got {breakdown_c.get('epc')}"
        )

    print("  PASS: null EPC scores lower than C and component score is 0")


if __name__ == "__main__":
    failures = []
    print("\n=== test_null_epc_scores_lower_than_C ===")
    try:
        test_null_epc_scores_lower_than_C()
    except AssertionError as exc:
        print(f"  {exc}")
        failures.append("test_null_epc_scores_lower_than_C")
    except Exception as exc:
        import traceback
        traceback.print_exc()
        failures.append("test_null_epc_scores_lower_than_C")

    print(f"\n{'=' * 60}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print("ALL 1 test groups passed.")
