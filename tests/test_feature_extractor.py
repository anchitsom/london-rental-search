"""
Unit tests for homehunt.feature_extractor.

Run: .venv/bin/python tests/test_feature_extractor.py

All fixtures are drawn from realistic Rightmove listing text patterns.
Tests use plain assertions so failures surface clear diffs without pytest.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homehunt.feature_extractor import (
    extract_garden,
    extract_balcony,
    extract_bills_included,
    extract_furnished,
    extract_council_tax_band,
    extract_let_available_date,
    extract_all,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def assert_eq(label, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


# ---------------------------------------------------------------------------
# garden
# ---------------------------------------------------------------------------

def test_garden():
    assert_eq("garden_private", extract_garden(
        "Beautifully presented two double bedroom flat with private garden and parking.",
        [], ""
    ), True)

    assert_eq("garden_shared", extract_garden(
        "The property benefits from access to a shared garden.",
        [], ""
    ), True)

    assert_eq("garden_feature_bullet", extract_garden(
        "Modern open plan kitchen.", ["Garden", "Allocated Parking"], ""
    ), True)

    assert_eq("garden_feature_rear", extract_garden(
        "Light filled apartment.", ["Rear Garden"], ""
    ), True)

    assert_eq("garden_false", extract_garden(
        "Fourth floor apartment with no outdoor space.", [], ""
    ), False)

    assert_eq("garden_roof_terrace_not_garden", extract_garden(
        "Roof terrace with stunning views.", [], ""
    ), False)


# ---------------------------------------------------------------------------
# balcony
# ---------------------------------------------------------------------------

def test_balcony():
    assert_eq("balcony_explicit", extract_balcony(
        "Balcony with views over the canal.", [], ""
    ), True)

    assert_eq("balcony_terrace", extract_balcony(
        "Private south-facing terrace off the living room.", [], ""
    ), True)

    assert_eq("balcony_feature_bullet", extract_balcony(
        "High spec finish throughout.", ["Juliet Balcony", "Gym"], ""
    ), True)

    assert_eq("balcony_false", extract_balcony(
        "Ground floor flat with garden access.", [], ""
    ), False)

    assert_eq("balcony_patio", extract_balcony(
        "Large private patio area to the rear.", [], ""
    ), True)


# ---------------------------------------------------------------------------
# bills_included
# ---------------------------------------------------------------------------

def test_bills_included():
    assert_eq("bills_simple", extract_bills_included(
        "Bills included in the monthly rent.", [], ""
    ), True)

    assert_eq("bills_utility", extract_bills_included(
        "All utility bills are included in the rental price.", [], ""
    ), True)

    assert_eq("bills_feature_bullet", extract_bills_included(
        "Modern kitchen.", ["Bills Included", "Furnished"], ""
    ), True)

    assert_eq("bills_false_not_included", extract_bills_included(
        "Bills not included. Tenant is responsible for utilities.", [], ""
    ), False)

    assert_eq("bills_false_absent", extract_bills_included(
        "Spacious two bedroom flat in sought after location.", [], ""
    ), False)

    assert_eq("bills_partial_water", extract_bills_included(
        "Water rates and council tax included.", [], ""
    ), True)


# ---------------------------------------------------------------------------
# furnished
# ---------------------------------------------------------------------------

def test_furnished():
    assert_eq("furnished_explicit", extract_furnished(
        "The property is available fully furnished.", [], ""
    ), "furnished")

    assert_eq("unfurnished_explicit", extract_furnished(
        "Available unfurnished to suitable tenant.", [], ""
    ), "unfurnished")

    assert_eq("part_furnished", extract_furnished(
        "Part furnished. White goods included.", [], ""
    ), "part_furnished")

    assert_eq("furnished_feature_bullet", extract_furnished(
        "Two bedroom flat.", ["Furnished", "Parking"], ""
    ), "furnished")

    assert_eq("unfurnished_feature_bullet", extract_furnished(
        "Bright flat.", ["Unfurnished", "Garden"], ""
    ), "unfurnished")

    assert_eq("part_furnished_feature", extract_furnished(
        "Nice property.", ["Part Furnished"], ""
    ), "part_furnished")

    assert_eq("furnished_unknown", extract_furnished(
        "A lovely flat near the shops.", [], ""
    ), "unknown")


# ---------------------------------------------------------------------------
# council_tax_band
# ---------------------------------------------------------------------------

def test_council_tax_band():
    assert_eq("ctb_colon_c", extract_council_tax_band(
        "Council tax band: C. EPC rating D.", [], ""
    ), "C")

    assert_eq("ctb_no_colon", extract_council_tax_band(
        "Council Tax Band D.", [], ""
    ), "D")

    assert_eq("ctb_lowercase_band", extract_council_tax_band(
        "council tax band e.", [], ""
    ), "E")

    assert_eq("ctb_feature_bullet", extract_council_tax_band(
        "Modern flat.", ["Council Tax Band B"], ""
    ), "B")

    assert_eq("ctb_none", extract_council_tax_band(
        "Two bed flat in Hackney.", [], ""
    ), None)

    assert_eq("ctb_inline_text", extract_council_tax_band(
        "The current council tax band is F.", [], ""
    ), "F")


# ---------------------------------------------------------------------------
# let_available_date
# ---------------------------------------------------------------------------

def test_let_available_date():
    assert_eq("available_from_date", extract_let_available_date(
        "Available from 1st June 2026.", [], ""
    ), "1st June 2026")

    assert_eq("available_now", extract_let_available_date(
        "Available now for immediate occupation.", [], ""
    ), "now")

    assert_eq("available_immediately", extract_let_available_date(
        "Property available immediately.", [], ""
    ), "immediately")

    assert_eq("available_feature", extract_let_available_date(
        "Nice flat.", ["Available From: 15th May 2026"], ""
    ), "15th May 2026")

    assert_eq("available_none", extract_let_available_date(
        "Modern two bedroom apartment in the heart of Shoreditch.", [], ""
    ), None)


# ---------------------------------------------------------------------------
# extract_all aggregator
# ---------------------------------------------------------------------------

def test_extract_all():
    desc = (
        "Beautifully presented two bedroom flat with private garden. Bills included. "
        "Available fully furnished from 1st June 2026. Council tax band C. "
        "Private balcony."
    )
    feats = ["Garden", "Bills Included", "Furnished"]
    result = extract_all(desc, feats, "2 bed flat in Hackney")

    assert_eq("all_garden", result["garden"], True)
    assert_eq("all_balcony", result["balcony"], True)
    assert_eq("all_bills", result["bills_included"], True)
    assert_eq("all_furnished", result["furnished"], "furnished")
    assert_eq("all_ctb", result["council_tax_band"], "C")
    assert_eq("all_date", result["let_available_date"], "1st June 2026")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        ("garden", test_garden),
        ("balcony", test_balcony),
        ("bills_included", test_bills_included),
        ("furnished", test_furnished),
        ("council_tax_band", test_council_tax_band),
        ("let_available_date", test_let_available_date),
        ("extract_all", test_extract_all),
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
            print(f"  ERROR in {name}: {exc}")
            failures.append(name)

    print(f"\n{'=' * 50}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print(f"ALL {len(tests)} test groups passed.")
