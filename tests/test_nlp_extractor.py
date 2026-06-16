"""
Unit tests for homehunt.nlp_extractor.extract_size_and_epc.

Run: .venv/bin/python tests/test_nlp_extractor.py

Tests cover:
  - Size extraction from sq ft phrases
  - Size extraction from sqm phrases with conversion (factor 10.7639)
  - EPC rating extraction from "EPC Rating: C" pattern
  - EPC rating extraction from "Energy efficiency D" pattern
  - Empty / None description returns both None
  - Mixed-content description returns both fields
  - Features list parses correctly when description is empty
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homehunt.nlp_extractor import extract_size_and_epc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def assert_eq(label, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


# ---------------------------------------------------------------------------
# Size extraction
# ---------------------------------------------------------------------------

def test_size_sqft_phrase():
    result = extract_size_and_epc("approximately 720 sq ft of well-arranged living space", None)
    assert_eq("sqft_approx_720", result["size_sqft"], 720)


def test_size_sqm_conversion():
    # 65 sqm * 10.7639 = 699.65 -> rounded to 700
    result = extract_size_and_epc("The property is 65 sqm in size", None)
    assert_eq("sqm_65_to_700", result["size_sqft"], 700)


# ---------------------------------------------------------------------------
# EPC extraction
# ---------------------------------------------------------------------------

def test_epc_rating_colon_pattern():
    result = extract_size_and_epc("EPC Rating: C. Available from June.", None)
    assert_eq("epc_rating_colon_C", result["epc_rating"], "C")


def test_epc_energy_efficiency_pattern():
    result = extract_size_and_epc("Energy efficiency D. Council tax band B.", None)
    assert_eq("epc_energy_efficiency_D", result["epc_rating"], "D")


# ---------------------------------------------------------------------------
# Empty / None inputs
# ---------------------------------------------------------------------------

def test_empty_description():
    result = extract_size_and_epc("", None)
    assert_eq("empty_desc_size_none", result["size_sqft"], None)
    assert_eq("empty_desc_epc_none", result["epc_rating"], None)


def test_none_description():
    result = extract_size_and_epc(None, None)
    assert_eq("none_desc_size_none", result["size_sqft"], None)
    assert_eq("none_desc_epc_none", result["epc_rating"], None)


# ---------------------------------------------------------------------------
# Mixed-content description
# ---------------------------------------------------------------------------

def test_mixed_content_both_fields():
    desc = (
        "Spacious two bedroom flat measuring approximately 720 sq ft. "
        "EPC Rating: B. Available now."
    )
    result = extract_size_and_epc(desc, None)
    assert_eq("mixed_size", result["size_sqft"], 720)
    assert_eq("mixed_epc", result["epc_rating"], "B")


# ---------------------------------------------------------------------------
# Features list (description empty)
# ---------------------------------------------------------------------------

def test_features_list_parses_when_description_empty():
    features = ["EPC Rating: B", "720 sq ft", "Private Garden"]
    result = extract_size_and_epc("", features)
    assert_eq("features_size", result["size_sqft"], 720)
    assert_eq("features_epc", result["epc_rating"], "B")


# ---------------------------------------------------------------------------
# Wide-pattern size extraction (Wave 1.6 regression fix)
# ---------------------------------------------------------------------------


def test_unicode_ft2_superscript():
    """ft2 rendered as Unicode superscript (U+00B2) must be matched."""
    result = extract_size_and_epc("720 ft² of well-arranged living space", None)
    assert_eq("ft2_unicode", result["size_sqft"], 720)


def test_unicode_m2_superscript():
    """m2 rendered as Unicode superscript (U+00B2) must be matched and converted."""
    # 65 * 10.7639 = 699.65 -> 700
    result = extract_size_and_epc("65 m² flat", None)
    assert_eq("m2_unicode_converted", result["size_sqft"], 700)


def test_square_feet_spelled_out():
    """'square feet' (no abbreviation) must be matched."""
    result = extract_size_and_epc("approximately 720 square feet of space", None)
    assert_eq("square_feet_spelled", result["size_sqft"], 720)


def test_square_metres_spelled_out():
    """'square metres' spelled out must be matched and converted."""
    # 65 * 10.7639 = 699.65 -> 700
    result = extract_size_and_epc("65 square metres", None)
    assert_eq("square_metres_spelled", result["size_sqft"], 700)


def test_sq_metres_variant():
    """'sq metres' (mixed abbreviation) must be matched."""
    # 67 * 10.7639 = 721.18 -> 721
    result = extract_size_and_epc("Total area: 67 sq metres", None)
    assert_eq("sq_metres_variant", result["size_sqft"], int(round(67 * 10.7639)))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        ("size_sqft_phrase", test_size_sqft_phrase),
        ("size_sqm_conversion", test_size_sqm_conversion),
        ("epc_rating_colon_pattern", test_epc_rating_colon_pattern),
        ("epc_energy_efficiency_pattern", test_epc_energy_efficiency_pattern),
        ("empty_description", test_empty_description),
        ("none_description", test_none_description),
        ("mixed_content_both_fields", test_mixed_content_both_fields),
        ("features_list_parses_when_description_empty", test_features_list_parses_when_description_empty),
        ("unicode_ft2_superscript", test_unicode_ft2_superscript),
        ("unicode_m2_superscript", test_unicode_m2_superscript),
        ("square_feet_spelled_out", test_square_feet_spelled_out),
        ("square_metres_spelled_out", test_square_metres_spelled_out),
        ("sq_metres_variant", test_sq_metres_variant),
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
