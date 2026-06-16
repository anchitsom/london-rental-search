"""
Tests for the hard-filter pre-stage (Wave 2 Agent F).

Run:
  .venv/bin/python tests/test_hard_filters.py

Design: docs/plans/2026-05-04-unified-filter-scoring-config.md (Section 3).
Decisions resolved 2026-05-04:
  - EPC floor lowered from C to D; null EPC passes through.
  - Bedroom max stays at 2.
  - size_sqft hard floor at 400; null sqft passes through.
  - carpet_other_areas demoted from hard filter to soft score
    (so carpet_other_areas = True listings DO survive the hard filter).

Tests use a tiny stub class that mimics the duck-typed surface
filters.apply_hard_filters relies on. The Listing ORM type satisfies the
same surface in production.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "filter-scoring-config.yaml",
)


class _ListingStub:
    """Duck-typed stand-in for homehunt.core.db.Listing inside filter tests."""

    def __init__(
        self,
        uid="test:1",
        price_pcm=2200,
        bedrooms=2,
        epc_rating="C",
        tfl_zone=2,
        size_sqft=600,
        region="Highbury, North London",
        carpet_other_areas=False,
    ):
        self.uid = uid
        self.price_pcm = price_pcm
        self.bedrooms = bedrooms
        self.epc_rating = epc_rating
        self.tfl_zone = tfl_zone
        self.size_sqft = size_sqft
        self.region = region
        self.carpet_other_areas = carpet_other_areas


def assert_eq(label, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


def assert_dropped(label, listing, hard_filters):
    from homehunt.filters import apply_hard_filters

    survivors, drops = apply_hard_filters([listing], hard_filters)
    if survivors:
        raise AssertionError(
            f"FAIL [{label}]: expected drop, but listing {listing.uid!r} survived. drops={drops}"
        )
    print(f"  PASS [{label}]")


def assert_survives(label, listing, hard_filters):
    from homehunt.filters import apply_hard_filters

    survivors, drops = apply_hard_filters([listing], hard_filters)
    if not survivors:
        raise AssertionError(
            f"FAIL [{label}]: expected survive, but listing {listing.uid!r} dropped. drops={drops}"
        )
    print(f"  PASS [{label}]")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_price_above_max_dropped():
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_dropped("price_above_max", _ListingStub(price_pcm=2900), hf)


def test_price_below_min_dropped():
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_dropped("price_below_min", _ListingStub(price_pcm=1500), hf)


def test_bedrooms_above_max_dropped():
    """Bedroom max is 2 (decision 2026-05-04)."""
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    # max is 2 -> 3 should drop, 4 should drop
    assert_dropped("bedrooms_3_dropped", _ListingStub(bedrooms=3), hf)
    assert_dropped("bedrooms_4_dropped", _ListingStub(bedrooms=4), hf)


def test_bedrooms_zero_dropped():
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_dropped("bedrooms_zero", _ListingStub(bedrooms=0), hf)


def test_epc_below_floor_dropped():
    """EPC floor is D (decision 2026-05-04). E, F, G drop; D and above pass."""
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_dropped("epc_E_dropped", _ListingStub(epc_rating="E"), hf)
    assert_dropped("epc_F_dropped", _ListingStub(epc_rating="F"), hf)
    assert_dropped("epc_G_dropped", _ListingStub(epc_rating="G"), hf)


def test_epc_d_passes():
    """D survives the hard filter at the new floor."""
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_survives("epc_D_passes", _ListingStub(epc_rating="D"), hf)


def test_epc_null_passes():
    """Null EPC passes the hard filter (epc_drop_null=false)."""
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_survives("epc_null_passes", _ListingStub(epc_rating=None), hf)


def test_tfl_zone_4_dropped():
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_dropped("tfl_zone_4", _ListingStub(tfl_zone=4), hf)


def test_tfl_zone_null_passes():
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_survives("tfl_zone_null", _ListingStub(tfl_zone=None), hf)


def test_carpet_other_areas_true_survives():
    """
    Decision 2026-05-04: carpet_other_areas was demoted from hard filter
    to soft scoring. A listing with carpet_other_areas=True must SURVIVE
    the hard filter (the scorer penalises it instead).
    """
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_survives(
        "carpet_other_areas_true_NOT_dropped",
        _ListingStub(carpet_other_areas=True),
        hf,
    )


def test_size_sqft_below_400_dropped():
    """Decision 2026-05-04: hard filter at 400 sqft."""
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_dropped("size_350_dropped", _ListingStub(size_sqft=350), hf)


def test_size_sqft_null_passes():
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_survives("size_null_passes", _ListingStub(size_sqft=None), hf)


def test_region_other_passes_when_drop_unlisted_off():
    """2026-05-08: region demoted from hard filter to soft signal.
    Default seed config has region_drop_unlisted=False, so 'Other' passes.
    """
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_survives("region_other_kept", _ListingStub(region="Other"), hf)


def test_region_unlisted_passes_when_drop_unlisted_off():
    """A region not in the allowlist still passes under the soft-signal default."""
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_survives("region_walthamstow_kept", _ListingStub(region="Walthamstow, East London"), hf)


def test_region_null_passes_when_drop_unlisted_off():
    """Null region passes the soft-signal default; the scorer ranks it low instead."""
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_survives("region_null_kept", _ListingStub(region=None), hf)


def test_region_null_dropped_when_drop_unlisted_explicit():
    """Opt-in: setting region_drop_unlisted=True restores the old behaviour."""
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    hf = dict(hf)
    hf["region_drop_unlisted"] = True
    assert_dropped("region_null_drop_opt_in", _ListingStub(region=None), hf)


def test_all_pass_listing_survives():
    from homehunt.filters import load_hard_filters
    hf = load_hard_filters(CONFIG_PATH)
    assert_survives(
        "all_pass",
        _ListingStub(
            price_pcm=2200,
            bedrooms=2,
            epc_rating="C",
            tfl_zone=2,
            size_sqft=600,
            region="Highbury, North London",
            carpet_other_areas=False,
        ),
        hf,
    )


def test_drop_counts_per_filter_on_batch():
    """
    Build a batch where each listing fails exactly one filter; confirm
    drop_counts attributes the drop to the correct filter.
    """
    from homehunt.filters import apply_hard_filters, load_hard_filters

    hf = load_hard_filters(CONFIG_PATH)
    listings = [
        _ListingStub(uid="ok", price_pcm=2200, bedrooms=2, epc_rating="C",
                     tfl_zone=2, size_sqft=600, region="Highbury, North London"),
        _ListingStub(uid="price_high", price_pcm=2900),
        _ListingStub(uid="price_low", price_pcm=1500),
        _ListingStub(uid="beds_too_many", bedrooms=3),
        _ListingStub(uid="beds_zero", bedrooms=0),
        _ListingStub(uid="epc_e", epc_rating="E"),
        _ListingStub(uid="zone_4", tfl_zone=4),
        _ListingStub(uid="size_small", size_sqft=350),
        _ListingStub(uid="region_other", region="Other"),
    ]
    survivors, drops = apply_hard_filters(listings, hf)

    # 2026-05-08: region demoted from hard filter; the 'region_other' stub
    # now survives. So 2 listings pass: the all-pass 'ok' and the one whose
    # only oddity was a non-allowlisted region.
    assert_eq("survivors_count", len(survivors), 2)
    assert_eq("drops_price_count", drops.get("price", 0), 2)
    assert_eq("drops_bedrooms_count", drops.get("bedrooms", 0), 2)
    assert_eq("drops_epc_count", drops.get("epc", 0), 1)
    assert_eq("drops_tfl_zone_count", drops.get("tfl_zone", 0), 1)
    assert_eq("drops_size_count", drops.get("size_sqft", 0), 1)
    assert_eq("drops_region_count", drops.get("region", 0), 0)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        ("price_above_max_dropped", test_price_above_max_dropped),
        ("price_below_min_dropped", test_price_below_min_dropped),
        ("bedrooms_above_max_dropped", test_bedrooms_above_max_dropped),
        ("bedrooms_zero_dropped", test_bedrooms_zero_dropped),
        ("epc_below_floor_dropped", test_epc_below_floor_dropped),
        ("epc_d_passes", test_epc_d_passes),
        ("epc_null_passes", test_epc_null_passes),
        ("tfl_zone_4_dropped", test_tfl_zone_4_dropped),
        ("tfl_zone_null_passes", test_tfl_zone_null_passes),
        ("carpet_other_areas_true_survives", test_carpet_other_areas_true_survives),
        ("size_sqft_below_400_dropped", test_size_sqft_below_400_dropped),
        ("size_sqft_null_passes", test_size_sqft_null_passes),
        ("region_other_passes", test_region_other_passes_when_drop_unlisted_off),
        ("region_unlisted_passes", test_region_unlisted_passes_when_drop_unlisted_off),
        ("region_null_passes", test_region_null_passes_when_drop_unlisted_off),
        ("region_null_dropped_opt_in", test_region_null_dropped_when_drop_unlisted_explicit),
        ("all_pass_listing_survives", test_all_pass_listing_survives),
        ("drop_counts_per_filter_on_batch", test_drop_counts_per_filter_on_batch),
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
