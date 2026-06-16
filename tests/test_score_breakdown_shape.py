"""
Pre-Wave-3 unblocker 2.1: scorer call site must produce the expected breakdown.

When split carpet signals are passed directly, the breakdown carries:
  price, size, bedrooms, epc, transport, outside,
  carpet_bedroom, carpet_other_areas, region_pref

When scoring is driven by the binary carpet_detected signal (including
_finalise_listing), the breakdown carries:
  price, size, bedrooms, epc, transport, outside, carpet, region_pref

Cases covered:
  1. Both carpet flags present + region present: nine keys, eight non-region.
  2. Carpet signals null: single carpet key present, neutral score (50).
  3. Region null: region_pref key present with value 0.

Run:
  HOMEHUNT_DB=tests/fixtures/score_breakdown_shape.db \
  ./.venv/bin/python \
  tests/test_score_breakdown_shape.py
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

EXPECTED_NON_REGION_KEYS = {
    "price",
    "size",
    "bedrooms",
    "epc",
    "transport",
    "outside",
    "carpet_bedroom",
    "carpet_other_areas",
}
EXPECTED_ALL_KEYS = EXPECTED_NON_REGION_KEYS | {"region_pref"}
EXPECTED_BINARY_NON_REGION_KEYS = {
    "price",
    "size",
    "bedrooms",
    "epc",
    "transport",
    "outside",
    "carpet",
}
EXPECTED_BINARY_ALL_KEYS = EXPECTED_BINARY_NON_REGION_KEYS | {"region_pref"}

# Eight-key seed with both legacy and split fields, sums to 1.0 across the
# eight non-region components plus region_tier.
SEED_WEIGHTS_SPLIT = {
    "price": 0.25,
    "carpet_bedroom": 0.10,
    "carpet_other_areas": 0.10,
    "size": 0.15,
    "bedrooms": 0.10,
    "epc": 0.10,
    "transport": 0.10,
    "outside": 0.05,
    "region_tier": 0.05,
}


def make_fixture_db(name: str) -> str:
    os.makedirs(FIXTURES_DIR, exist_ok=True)
    path = os.path.join(FIXTURES_DIR, f"{name}.db")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE scoring_weights (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL,
            weights_json TEXT NOT NULL,
            fit_metrics_json TEXT NULL,
            notes TEXT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO scoring_weights (created_at, source, weights_json) VALUES (?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            "seed",
            json.dumps(SEED_WEIGHTS_SPLIT),
        ),
    )
    conn.commit()
    conn.close()
    return path


def test_breakdown_shape_with_carpet_and_region():
    """Both carpet flags + region populated -> nine keys, all expected."""
    from homehunt.scorer import compute_score_from_db

    db_path = make_fixture_db("breakdown_shape_full")

    fixture = dict(
        price_pcm=2400,
        size_sqft=750,
        bedrooms=2,
        carpet_in_bedroom=False,
        carpet_other_areas=False,
        epc_rating="B",
        commute_mins=20,
        tfl_zone=2,
        garden=False,
        balcony=True,
        region="Highbury, North London",
    )
    total, breakdown = compute_score_from_db(db_path=db_path, **fixture)
    print(f"  full breakdown keys: {sorted(breakdown.keys())}, total={total}")

    if set(breakdown.keys()) != EXPECTED_ALL_KEYS:
        raise AssertionError(
            f"FAIL: expected {EXPECTED_ALL_KEYS}, got {set(breakdown.keys())}"
        )
    for key in EXPECTED_ALL_KEYS:
        if breakdown[key] is None:
            raise AssertionError(f"FAIL: breakdown[{key}] is None, expected int")
    print("  PASS: all nine keys present with non-null values")


def test_breakdown_shape_with_null_carpet():
    """Carpet signals null -> single binary key present with neutral score (50)."""
    from homehunt.scorer import compute_score_from_db

    db_path = make_fixture_db("breakdown_shape_null_carpet")

    fixture = dict(
        price_pcm=2400,
        size_sqft=750,
        bedrooms=2,
        carpet_in_bedroom=None,
        carpet_other_areas=None,
        epc_rating="B",
        commute_mins=20,
        tfl_zone=2,
        garden=False,
        balcony=True,
        region="Highbury, North London",
    )
    total, breakdown = compute_score_from_db(db_path=db_path, **fixture)
    print(f"  null-carpet breakdown: {breakdown}")

    if set(breakdown.keys()) != EXPECTED_BINARY_ALL_KEYS:
        raise AssertionError(
            f"FAIL: expected {EXPECTED_BINARY_ALL_KEYS}, got {set(breakdown.keys())}"
        )
    if breakdown["carpet"] != 50:
        raise AssertionError(
            f"FAIL: expected carpet = 50 on null, got {breakdown['carpet']}"
        )
    if "carpet_bedroom" in breakdown or "carpet_other_areas" in breakdown:
        raise AssertionError(
            "FAIL: split carpet keys leaked when binary signal expected"
        )
    print("  PASS: single carpet key present with neutral 50 on null input")


def test_breakdown_shape_with_null_region():
    """Region null -> region_pref key present, value 0."""
    from homehunt.scorer import compute_score_from_db

    db_path = make_fixture_db("breakdown_shape_null_region")

    fixture = dict(
        price_pcm=2400,
        size_sqft=750,
        bedrooms=2,
        carpet_in_bedroom=False,
        carpet_other_areas=False,
        epc_rating="B",
        commute_mins=20,
        tfl_zone=2,
        garden=False,
        balcony=True,
        region=None,
    )
    total, breakdown = compute_score_from_db(db_path=db_path, **fixture)
    print(f"  null-region breakdown: {breakdown}")

    if "region_pref" not in breakdown:
        raise AssertionError(
            f"FAIL: region_pref key missing when region is null, got {list(breakdown.keys())}"
        )
    if breakdown["region_pref"] != 0:
        raise AssertionError(
            f"FAIL: expected region_pref = 0 on null region, got {breakdown['region_pref']}"
        )
    print("  PASS: region_pref key present with 0 on null region")


def test_run_finalise_emits_eight_key_shape():
    """
    Direct functional check on the run.py call site path.

    Construct a partial Listing with carpet_in_bedroom and region populated.
    Call _finalise_listing with an enriched dict that has carpet flags. Assert
    the resulting score_breakdown JSON is the eight-key shape including
    region_pref and the single binary carpet key.
    """
    # Point HOMEHUNT_DB at a freshly seeded fixture so load_active_weights
    # picks up the split weights row (else it falls back to yaml seed which
    # uses the split components by name in any case).
    db_path = make_fixture_db("run_finalise_shape")
    os.environ["HOMEHUNT_DB"] = db_path
    # Force scorer module reload so the cached _DEFAULT_DB_PATH refreshes.
    import importlib
    import homehunt.scorer as scorer_mod
    importlib.reload(scorer_mod)

    from homehunt.core.db import Listing
    from homehunt.core.models import ExtractionMethod, Portal
    import run

    partial = Listing(
        uid="rightmove:test-shape",
        portal=Portal.RIGHTMOVE,
        property_id="test-shape",
        url="https://example.test/properties/1",
        price_numeric=240000,  # 2400 pcm in pence
        bedrooms=2,
        size_sqft=750,
        garden=False,
        balcony=True,
        region="Highbury, North London",
        extraction_method=ExtractionMethod.DIRECT_HTTP,
        is_active=True,
        status="active",
    )
    enriched = {
        "size_sqft": 750,
        "size_sqft_source": "structured",
        "size_sqft_confidence": 0.9,
        "epc": "B",
        "epc_rating_source": "api",
        "epc_rating_confidence": 1.0,
        "carpet": {
            "carpet_detected": False,
            "carpet_confidence": 0.9,
            "carpet_in_bedroom": False,
            "carpet_other_areas": False,
        },
        "floorplan": None,
        "raw": {},
        "tfl": {
            "commute_to": {"canary_wharf": 22, "whitechapel": 25},
            "tfl_zone": 2,
        },
    }

    finalised = run._finalise_listing(partial, enriched)
    breakdown = json.loads(finalised.score_breakdown)
    print(f"  finalised breakdown: {breakdown}")

    if set(breakdown.keys()) != EXPECTED_BINARY_ALL_KEYS:
        raise AssertionError(
            f"FAIL: _finalise_listing emitted {set(breakdown.keys())},"
            f" expected {EXPECTED_BINARY_ALL_KEYS}"
        )
    print("  PASS: _finalise_listing produces the binary carpet + region_pref shape")


if __name__ == "__main__":
    tests = [
        ("breakdown_shape_with_carpet_and_region", test_breakdown_shape_with_carpet_and_region),
        ("breakdown_shape_with_null_carpet", test_breakdown_shape_with_null_carpet),
        ("breakdown_shape_with_null_region", test_breakdown_shape_with_null_region),
        ("run_finalise_emits_eight_key_shape", test_run_finalise_emits_eight_key_shape),
    ]

    failures = []
    for name, fn in tests:
        print(f"\n=== {name} ===")
        try:
            fn()
        except AssertionError as exc:
            print(f"  {exc}")
            failures.append(name)
        except Exception as exc:
            import traceback
            print(f"  ERROR in {name}: {exc}")
            traceback.print_exc()
            failures.append(name)

    print(f"\n{'=' * 60}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print(f"ALL {len(tests)} tests passed.")
