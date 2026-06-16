"""
Binary carpet detector and scorer tests for rental-carpet-binary.

Run:
  ./.venv/bin/python tests/test_rental_carpet_binary.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def assert_eq(label, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


async def test_detect_carpet_any_batches_all_photos():
    import homehunt.carpet as carpet

    old_batch_size = carpet.CARPET_BATCH_SIZE
    old_fetch = carpet._fetch_image_b64
    old_query = carpet._query_carpet_batch
    calls = []

    async def fake_fetch(client, url):
        return f"b64:{url}"

    async def fake_query(client, images):
        calls.append(list(images))
        return [
            {"carpet": image.endswith("photo-8"), "confidence": 0.8}
            for image in images
        ]

    try:
        carpet.CARPET_BATCH_SIZE = 4
        carpet._fetch_image_b64 = fake_fetch
        carpet._query_carpet_batch = fake_query
        result = await carpet.detect_carpet_any([f"photo-{i}" for i in range(9)])
    finally:
        carpet.CARPET_BATCH_SIZE = old_batch_size
        carpet._fetch_image_b64 = old_fetch
        carpet._query_carpet_batch = old_query

    assert_eq("all_photos_in_per_photo", len(result["per_photo"]), 9)
    assert_eq("chunked_batch_calls", len(calls), 3)
    assert_eq("any_carpet_true", result["carpet_detected"], True)
    assert_eq("room_field_carpet_in_bedroom_null", result["carpet_in_bedroom"], None)
    assert_eq("room_field_carpet_other_areas_null", result["carpet_other_areas"], None)
    assert_eq("room_field_bedrooms_with_carpet_null", result["bedrooms_with_carpet"], None)
    assert_eq("room_field_has_living_area_carpet_null", result["has_living_area_carpet"], None)


async def test_detect_carpet_any_false_empty_and_mismatch():
    import homehunt.carpet as carpet

    old_batch_size = carpet.CARPET_BATCH_SIZE
    old_fetch = carpet._fetch_image_b64
    old_query = carpet._query_carpet_batch

    async def fake_fetch(client, url):
        return f"b64:{url}"

    async def all_false(client, images):
        return [{"carpet": False, "confidence": 0.7} for _ in images]

    async def mismatched_true(client, images):
        return [{"carpet": True, "confidence": 0.99}]

    try:
        carpet.CARPET_BATCH_SIZE = 4
        carpet._fetch_image_b64 = fake_fetch
        carpet._query_carpet_batch = all_false
        result_false = await carpet.detect_carpet_any([f"photo-{i}" for i in range(5)])

        result_empty = await carpet.detect_carpet_any([])

        carpet._query_carpet_batch = mismatched_true
        result_mismatch = await carpet.detect_carpet_any(["a", "b", "c"])
    finally:
        carpet.CARPET_BATCH_SIZE = old_batch_size
        carpet._fetch_image_b64 = old_fetch
        carpet._query_carpet_batch = old_query

    assert_eq("all_false_detected_false", result_false["carpet_detected"], False)
    assert_eq("empty_detected_none", result_empty["carpet_detected"], None)
    assert_eq("mismatch_not_true", result_mismatch["carpet_detected"], None)


def test_scorer_binary_carpet_config():
    from homehunt.scorer import compute_score

    base = dict(
        price_pcm=2500,
        size_sqft=700,
        bedrooms=2,
        carpet_confidence=0.9,
        carpet_in_bedroom=None,
        carpet_other_areas=None,
        bedrooms_with_carpet=None,
        has_living_area_carpet=None,
        epc_rating="C",
        commute_mins=20,
        tfl_zone=2,
        garden=True,
        balcony=False,
    )

    total_false, breakdown_false = compute_score(carpet_detected=False, **base)
    total_true, breakdown_true = compute_score(carpet_detected=True, **base)
    _, breakdown_none = compute_score(carpet_detected=None, **base)

    assert_eq("carpet_true_detected_score", breakdown_true["carpet"], 20)
    assert_eq("carpet_false_not_detected_score", breakdown_false["carpet"], 100)
    assert_eq("carpet_none_null_score", breakdown_none["carpet"], 50)
    if not total_true < total_false:
        raise AssertionError(
            f"FAIL [weighted_total_drop]: true={total_true}, false={total_false}"
        )
    print("  PASS [weighted_total_drop]")


async def main():
    failures = []
    tests = [
        ("detect_carpet_any_batches_all_photos", test_detect_carpet_any_batches_all_photos),
        ("detect_carpet_any_false_empty_and_mismatch", test_detect_carpet_any_false_empty_and_mismatch),
        ("scorer_binary_carpet_config", test_scorer_binary_carpet_config),
    ]

    for name, fn in tests:
        print(f"\n=== {name} ===")
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            import traceback
            traceback.print_exc()
            failures.append(name)

    print(f"\n{'=' * 60}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("ALL test groups passed.")


if __name__ == "__main__":
    asyncio.run(main())
