"""
Tests for the unified filter and scoring config loader (Wave 2 Agent F).

Run:
  .venv/bin/python tests/test_unified_config_loader.py

Design: docs/plans/2026-05-04-unified-filter-scoring-config.md
Sample yaml: docs/plans/2026-05-04-unified-filter-scoring-config.sample.yaml
Active config: filter-scoring-config.yaml at the project root.

Tests:
  1. load_hard_filters returns a dict with the expected keys.
  2. load_scoring_config returns components whose weights sum to 1.0
     within float tolerance.
  3. regions_by_tier union equals hard_filters.regions (no orphans).
  4. region_tier.weight is 0.0 in the seed file.
  5. bedrooms.max is 2 (per the 2026-05-04 user decision; not 3).
  6. epc_min_rating is "D" (per the 2026-05-04 user decision; not "C").
  7. epc_drop_null is False (null EPC passes the filter).
  8. epc.null_score is 0 (null-rated listings score zero in the soft component).
  9. size_sqft_min is 400 (new hard filter).
 10. carpet_other_areas_disallowed is absent from hard_filters
     (carpet was demoted from filter to soft scoring).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "filter-scoring-config.yaml",
)


def assert_eq(label, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


def assert_in(label, item, container):
    if item not in container:
        raise AssertionError(f"FAIL [{label}]: {item!r} not in {container!r}")
    print(f"  PASS [{label}]")


def assert_not_in(label, item, container):
    if item in container:
        raise AssertionError(f"FAIL [{label}]: {item!r} unexpectedly present in {container!r}")
    print(f"  PASS [{label}]")


# ---------------------------------------------------------------------------
# Test 1: load_hard_filters returns the expected key set
# ---------------------------------------------------------------------------

def test_load_hard_filters_keys():
    from homehunt.config_loader import load_hard_filters

    hf = load_hard_filters(CONFIG_PATH)
    expected_keys = {
        "price",
        "bedrooms",
        "epc_min_rating",
        "epc_drop_null",
        "tfl_zone_max",
        "size_sqft_min",
        "regions",
    }
    missing = expected_keys - set(hf.keys())
    if missing:
        raise AssertionError(f"FAIL [load_hard_filters_keys]: missing {missing}")
    print("  PASS [load_hard_filters_keys]")


# ---------------------------------------------------------------------------
# Test 2: scoring weights sum to 1.0
# ---------------------------------------------------------------------------

def test_weights_sum_to_one():
    from homehunt.config_loader import load_scoring_config

    sc = load_scoring_config(CONFIG_PATH)
    components = sc["components"]
    total = sum(c["weight"] for c in components.values())
    if abs(total - 1.0) > 1e-6:
        raise AssertionError(f"FAIL [weights_sum_to_one]: sum={total}")
    print(f"  PASS [weights_sum_to_one]: sum={total}")


# ---------------------------------------------------------------------------
# Test 3: regions_by_tier union equals hard_filters.regions
# ---------------------------------------------------------------------------

def test_regions_by_tier_consistent_with_allowlist():
    from homehunt.config_loader import load_hard_filters, load_unified_config

    cfg = load_unified_config(CONFIG_PATH)
    hf_regions = set(cfg["hard_filters"]["regions"])
    tiers = cfg["regions_by_tier"]
    tier_union = set(tiers["tier_1"]) | set(tiers["tier_2"]) | set(tiers["tier_3"])

    only_in_hf = hf_regions - tier_union
    only_in_tiers = tier_union - hf_regions

    if only_in_hf:
        raise AssertionError(
            f"FAIL [regions_consistent]: in hard_filters.regions but no tier: {only_in_hf}"
        )
    if only_in_tiers:
        raise AssertionError(
            f"FAIL [regions_consistent]: in tiers but not in hard_filters.regions: {only_in_tiers}"
        )
    print(f"  PASS [regions_consistent]: {len(hf_regions)} regions match across both blocks")


# ---------------------------------------------------------------------------
# Test 4: region_tier weight bumped to 0.05 (2026-05-08, region soft signal)
# ---------------------------------------------------------------------------

def test_region_tier_seed_weight_is_active():
    """Region was demoted from hard filter to soft signal on 2026-05-08;
    the freed 0.05 weight from bedrooms went here so out-of-area listings
    rank measurably lower without being dropped."""
    from homehunt.config_loader import load_scoring_config

    sc = load_scoring_config(CONFIG_PATH)
    rt = sc["components"]["region_tier"]
    assert_eq("region_tier_seed_weight", rt["weight"], 0.05)


# ---------------------------------------------------------------------------
# Test 5: bedrooms.max is 2 (per 2026-05-04 user decision)
# ---------------------------------------------------------------------------

def test_bedrooms_max_is_two():
    from homehunt.config_loader import load_hard_filters

    hf = load_hard_filters(CONFIG_PATH)
    assert_eq("bedrooms_max", hf["bedrooms"]["max"], 2)
    assert_eq("bedrooms_min", hf["bedrooms"]["min"], 1)


# ---------------------------------------------------------------------------
# Test 6: epc_min_rating is D (per 2026-05-04 decision)
# ---------------------------------------------------------------------------

def test_epc_floor_is_D():
    from homehunt.config_loader import load_hard_filters

    hf = load_hard_filters(CONFIG_PATH)
    assert_eq("epc_min_rating", hf["epc_min_rating"], "D")


# ---------------------------------------------------------------------------
# Test 7: epc_drop_null is False
# ---------------------------------------------------------------------------

def test_epc_drop_null_false():
    from homehunt.config_loader import load_hard_filters

    hf = load_hard_filters(CONFIG_PATH)
    assert_eq("epc_drop_null", hf["epc_drop_null"], False)


# ---------------------------------------------------------------------------
# Test 8: scoring.components.epc.null_score is 0
# ---------------------------------------------------------------------------

def test_epc_null_score_is_zero():
    from homehunt.config_loader import load_scoring_config

    sc = load_scoring_config(CONFIG_PATH)
    assert_eq("epc_null_score", sc["components"]["epc"]["null_score"], 0)


# ---------------------------------------------------------------------------
# Test 9: size_sqft_min is 400
# ---------------------------------------------------------------------------

def test_size_sqft_min_present():
    from homehunt.config_loader import load_hard_filters

    hf = load_hard_filters(CONFIG_PATH)
    assert_in("size_sqft_min_in_hard_filters", "size_sqft_min", hf)
    assert_eq("size_sqft_min_value", hf["size_sqft_min"], 400)


# ---------------------------------------------------------------------------
# Test 10: carpet_other_areas_disallowed must NOT be in hard_filters
# ---------------------------------------------------------------------------

def test_carpet_other_areas_not_a_filter():
    from homehunt.config_loader import load_hard_filters

    hf = load_hard_filters(CONFIG_PATH)
    assert_not_in(
        "carpet_other_areas_disallowed_absent",
        "carpet_other_areas_disallowed",
        hf,
    )

    # carpet_other_areas should be a soft scoring component instead.
    from homehunt.config_loader import load_scoring_config
    sc = load_scoring_config(CONFIG_PATH)
    assert_in(
        "carpet_other_areas_in_components",
        "carpet_other_areas",
        sc["components"],
    )
    assert_in(
        "carpet_bedroom_in_components",
        "carpet_bedroom",
        sc["components"],
    )
    # Both halves at 0.10 each (carpet stays at 0.20 total)
    assert_eq(
        "carpet_bedroom_weight",
        sc["components"]["carpet_bedroom"]["weight"],
        0.10,
    )
    assert_eq(
        "carpet_other_areas_weight",
        sc["components"]["carpet_other_areas"]["weight"],
        0.10,
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        ("load_hard_filters_keys", test_load_hard_filters_keys),
        ("weights_sum_to_one", test_weights_sum_to_one),
        ("regions_by_tier_consistent_with_allowlist", test_regions_by_tier_consistent_with_allowlist),
        ("region_tier_seed_weight_is_active", test_region_tier_seed_weight_is_active),
        ("bedrooms_max_is_two", test_bedrooms_max_is_two),
        ("epc_floor_is_D", test_epc_floor_is_D),
        ("epc_drop_null_false", test_epc_drop_null_false),
        ("epc_null_score_is_zero", test_epc_null_score_is_zero),
        ("size_sqft_min_present", test_size_sqft_min_present),
        ("carpet_other_areas_not_a_filter", test_carpet_other_areas_not_a_filter),
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
        print(f"ALL {len(tests)} test groups passed.")
