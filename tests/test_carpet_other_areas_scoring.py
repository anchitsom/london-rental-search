"""
Test that carpet_detected = True produces a measurably lower total score
than carpet_detected = False, with all other component inputs held identical.

rental-carpet-binary: the active carpet component is room-agnostic and
binary. Carpet detected anywhere scores 20; no carpet scores 100; unknown
scores 50. Legacy split/count fields are not part of the active path.

Run:
  .venv/bin/python tests/test_carpet_other_areas_scoring.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_carpet_detected_true_scores_lower_than_false():
    from homehunt.scorer import compute_score

    base = dict(
        price_pcm=2200,
        size_sqft=700,
        bedrooms=2,
        epc_rating="C",
        commute_mins=20,
        tfl_zone=2,
        garden=True,
        balcony=False,
        carpet_in_bedroom=None,
        carpet_other_areas=None,
        bedrooms_with_carpet=None,
        has_living_area_carpet=None,
        carpet_confidence=0.9,
    )

    total_clean, breakdown_clean = compute_score(carpet_detected=False, **base)
    total_carpet, breakdown_carpet = compute_score(carpet_detected=True, **base)

    print(f"  carpet_detected=False total={total_clean}, breakdown={breakdown_clean}")
    print(f"  carpet_detected=True  total={total_carpet}, breakdown={breakdown_carpet}")

    if not (total_carpet < total_clean):
        raise AssertionError(
            f"FAIL: carpet=True total ({total_carpet}) not < carpet=False total ({total_clean})"
        )

    carpet_true = breakdown_carpet.get("carpet")
    carpet_false = breakdown_clean.get("carpet")
    if carpet_true != 20:
        raise AssertionError(
            f"FAIL: expected breakdown.carpet == 20 when True, got {carpet_true!r}"
        )
    if carpet_false != 100:
        raise AssertionError(
            f"FAIL: expected breakdown.carpet == 100 when False, got {carpet_false!r}"
        )

    # The drop is 20 points (0.25 weight * (100 - 20) = 20).
    drop = total_clean - total_carpet
    if drop < 19:
        raise AssertionError(
            f"FAIL: expected drop of approximately 20 points, got {drop}"
        )

    print(f"  PASS: True scores {drop} points lower than False (>=20 expected)")


if __name__ == "__main__":
    failures = []
    print("\n=== test_carpet_detected_true_scores_lower_than_false ===")
    try:
        test_carpet_detected_true_scores_lower_than_false()
    except AssertionError as exc:
        print(f"  {exc}")
        failures.append("test_carpet_detected_true_scores_lower_than_false")
    except Exception as exc:
        import traceback
        traceback.print_exc()
        failures.append("test_carpet_detected_true_scores_lower_than_false")

    print(f"\n{'=' * 60}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print("ALL 1 test groups passed.")
