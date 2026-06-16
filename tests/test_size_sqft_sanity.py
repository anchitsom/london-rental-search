"""
Tests for size_sqft merge-layer sanity rules and source trust order.

Run:
  .venv/bin/python tests/test_size_sqft_sanity.py

Tests:
  1. Source trust order: structured > nlp > floorplan_vision > epc_interpolated > epc_direct.
  2. Sanity rule: size_sqft < 100 is rejected (OCR digit-drop case 174140525).
  3. Sanity rule: size_sqft / bedrooms > 800 is rejected (EPC mismatch 87910716, 88015827).
  4. Sanity rule: EPC vs floorplan >30% disagreement prefers floorplan and emits warning.
  5. Provenance: size_sqft_source set correctly for every source path.
  6. Provenance: size_sqft_confidence set correctly per source.
  7. When no source provides a value, size_sqft and size_sqft_source remain None.
  8. Listing 174140525: NLP size 545 passes rule 2 (>= 100), so NLP wins over EPC.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homehunt.size_merge import merge_size_sqft, SIZE_SOURCE_CONFIDENCE


def assert_eq(label: str, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


def assert_none(label: str, got):
    if got is not None:
        raise AssertionError(f"FAIL [{label}]: expected None, got {got!r}")
    print(f"  PASS [{label}]")


def assert_not_none(label: str, got):
    if got is None:
        raise AssertionError(f"FAIL [{label}]: expected not-None")
    print(f"  PASS [{label}]")


def assert_in_range(label: str, got, lo, hi):
    if not (lo <= got <= hi):
        raise AssertionError(f"FAIL [{label}]: expected {lo}..{hi}, got {got!r}")
    print(f"  PASS [{label}]")


# ---------------------------------------------------------------------------
# Test 1: Source trust order
# ---------------------------------------------------------------------------

def test_source_trust_order():
    print("Test 1: Source trust order")

    # structured wins over all
    result = merge_size_sqft(
        structured=600,
        nlp=500,
        floorplan_vision=400,
        epc_interpolated=300,
        epc_direct=250,
        bedrooms=2,
    )
    assert_eq("structured wins", result["size_sqft"], 600)
    assert_eq("source structured", result["size_sqft_source"], "structured")

    # nlp wins over floorplan, epc
    result = merge_size_sqft(
        structured=None,
        nlp=500,
        floorplan_vision=400,
        epc_interpolated=300,
        epc_direct=250,
        bedrooms=2,
    )
    assert_eq("nlp wins over fp+epc", result["size_sqft"], 500)
    assert_eq("source nlp", result["size_sqft_source"], "nlp")

    # floorplan_vision wins over epc
    result = merge_size_sqft(
        structured=None,
        nlp=None,
        floorplan_vision=400,
        epc_interpolated=300,
        epc_direct=250,
        bedrooms=2,
    )
    assert_eq("fp wins over epc", result["size_sqft"], 400)
    assert_eq("source floorplan", result["size_sqft_source"], "floorplan_vision")

    # epc_interpolated wins over epc_direct
    result = merge_size_sqft(
        structured=None,
        nlp=None,
        floorplan_vision=None,
        epc_interpolated=300,
        epc_direct=250,
        bedrooms=2,
    )
    assert_eq("epc_interpolated wins over direct", result["size_sqft"], 300)
    assert_eq("source epc_interp", result["size_sqft_source"], "epc_interpolated")

    # epc_direct last resort
    result = merge_size_sqft(
        structured=None,
        nlp=None,
        floorplan_vision=None,
        epc_interpolated=None,
        epc_direct=250,
        bedrooms=2,
    )
    assert_eq("epc_direct last", result["size_sqft"], 250)
    assert_eq("source epc_direct", result["size_sqft_source"], "epc_direct")


# ---------------------------------------------------------------------------
# Test 2: size_sqft < 100 rejected (OCR digit-drop, listing 174140525)
# ---------------------------------------------------------------------------

def test_sanity_rule_min_100():
    print("Test 2: Sanity rule -- size_sqft < 100 rejected")

    # 54 sqft from floorplan is rejected; falls through to EPC
    result = merge_size_sqft(
        structured=None,
        nlp=None,
        floorplan_vision=54,  # OCR digit-drop
        epc_interpolated=None,
        epc_direct=474,
        bedrooms=1,
    )
    assert_eq("ocr digit-drop skipped", result["size_sqft"], 474)
    assert_eq("falls through to epc_direct", result["size_sqft_source"], "epc_direct")

    # All sources below 100 -> None
    result = merge_size_sqft(
        structured=None,
        nlp=50,
        floorplan_vision=30,
        epc_interpolated=None,
        epc_direct=None,
        bedrooms=1,
    )
    assert_none("all below 100 -> None size", result["size_sqft"])
    assert_none("source is None", result["size_sqft_source"])


# ---------------------------------------------------------------------------
# Test 3: size_sqft / bedrooms > 800 rejected (EPC mismatch)
# ---------------------------------------------------------------------------

def test_sanity_rule_per_bedroom_cap():
    print("Test 3: Sanity rule -- size_sqft / bedrooms > 800 rejected")

    # 1313 sqft / 1 bed = 1313 > 800, reject epc_direct
    result = merge_size_sqft(
        structured=None,
        nlp=None,
        floorplan_vision=536,
        epc_interpolated=None,
        epc_direct=1313,  # wrong EPC match for 87910716
        bedrooms=1,
    )
    assert_eq("epc too big, fp wins", result["size_sqft"], 536)
    assert_eq("source is floorplan", result["size_sqft_source"], "floorplan_vision")

    # 1292 sqft / 1 bed = 1292 > 800, reject epc_direct
    result = merge_size_sqft(
        structured=None,
        nlp=None,
        floorplan_vision=412,
        epc_interpolated=None,
        epc_direct=1292,  # wrong EPC match for 88015827
        bedrooms=1,
    )
    assert_eq("epc too big 88015827", result["size_sqft"], 412)
    assert_eq("source floorplan 88015827", result["size_sqft_source"], "floorplan_vision")

    # 2 bed, 1200 sqft = 600/bed < 800, should pass
    result = merge_size_sqft(
        structured=None,
        nlp=None,
        floorplan_vision=None,
        epc_interpolated=None,
        epc_direct=1200,
        bedrooms=2,
    )
    assert_eq("1200/2bed passes", result["size_sqft"], 1200)

    # bedrooms=None: skip the per-bedroom rule
    result = merge_size_sqft(
        structured=None,
        nlp=None,
        floorplan_vision=None,
        epc_interpolated=None,
        epc_direct=1313,
        bedrooms=None,
    )
    assert_eq("no bedrooms info, rule skipped", result["size_sqft"], 1313)


# ---------------------------------------------------------------------------
# Test 4: EPC vs floorplan >30% disagreement -- prefer floorplan
# ---------------------------------------------------------------------------

def test_epc_floorplan_disagreement_warning():
    print("Test 4: EPC vs floorplan >30% disagreement prefers floorplan")

    # EPC says 700, floorplan says 400: disagree by 43%; prefer floorplan
    result = merge_size_sqft(
        structured=None,
        nlp=None,
        floorplan_vision=400,
        epc_interpolated=None,
        epc_direct=700,
        bedrooms=2,
    )
    assert_eq("fp wins on 30pct disagreement", result["size_sqft"], 400)
    assert_eq("source fp", result["size_sqft_source"], "floorplan_vision")
    assert_eq("disagreement warning set", result.get("epc_floorplan_disagreement"), True)

    # EPC says 500, floorplan says 480: only 4% difference; normal trust order
    # floorplan still wins (it's higher priority) but no disagreement flag
    result = merge_size_sqft(
        structured=None,
        nlp=None,
        floorplan_vision=480,
        epc_interpolated=None,
        epc_direct=500,
        bedrooms=2,
    )
    assert_eq("small diff no flag", result.get("epc_floorplan_disagreement"), False)


# ---------------------------------------------------------------------------
# Test 5: Provenance size_sqft_source is set for every source path
# ---------------------------------------------------------------------------

def test_provenance_source():
    print("Test 5: Provenance size_sqft_source is set for every source path")

    for source, val, kwargs in [
        ("structured", 600, {"structured": 600, "nlp": None, "floorplan_vision": None, "epc_interpolated": None, "epc_direct": None, "bedrooms": 2}),
        ("nlp", 500, {"structured": None, "nlp": 500, "floorplan_vision": None, "epc_interpolated": None, "epc_direct": None, "bedrooms": 2}),
        ("floorplan_vision", 400, {"structured": None, "nlp": None, "floorplan_vision": 400, "epc_interpolated": None, "epc_direct": None, "bedrooms": 2}),
        ("epc_interpolated", 300, {"structured": None, "nlp": None, "floorplan_vision": None, "epc_interpolated": 300, "epc_direct": None, "bedrooms": 2}),
        ("epc_direct", 250, {"structured": None, "nlp": None, "floorplan_vision": None, "epc_interpolated": None, "epc_direct": 250, "bedrooms": 2}),
    ]:
        result = merge_size_sqft(**kwargs)
        assert_eq(f"source={source}", result["size_sqft_source"], source)
        assert_eq(f"value={source}", result["size_sqft"], val)


# ---------------------------------------------------------------------------
# Test 6: Provenance size_sqft_confidence values
# ---------------------------------------------------------------------------

def test_provenance_confidence():
    print("Test 6: Provenance size_sqft_confidence values")

    assert_eq("structured confidence", SIZE_SOURCE_CONFIDENCE["structured"], 0.95)
    assert_eq("nlp confidence", SIZE_SOURCE_CONFIDENCE["nlp"], 0.85)
    assert_eq("floorplan_vision confidence", SIZE_SOURCE_CONFIDENCE["floorplan_vision"], 0.80)
    assert_eq("epc_direct confidence", SIZE_SOURCE_CONFIDENCE["epc_direct"], 0.70)
    assert_eq("epc_interpolated confidence", SIZE_SOURCE_CONFIDENCE["epc_interpolated"], 0.50)

    result = merge_size_sqft(
        structured=600,
        nlp=None,
        floorplan_vision=None,
        epc_interpolated=None,
        epc_direct=None,
        bedrooms=2,
    )
    assert_eq("structured conf in result", result["size_sqft_confidence"], 0.95)

    result = merge_size_sqft(
        structured=None,
        nlp=None,
        floorplan_vision=None,
        epc_interpolated=None,
        epc_direct=500,
        bedrooms=2,
    )
    assert_eq("epc_direct conf in result", result["size_sqft_confidence"], 0.70)


# ---------------------------------------------------------------------------
# Test 7: No source -> size_sqft and size_sqft_source both None
# ---------------------------------------------------------------------------

def test_no_source():
    print("Test 7: No source -- both size fields None")
    result = merge_size_sqft(
        structured=None,
        nlp=None,
        floorplan_vision=None,
        epc_interpolated=None,
        epc_direct=None,
        bedrooms=2,
    )
    assert_none("size_sqft None", result["size_sqft"])
    assert_none("source None", result["size_sqft_source"])
    assert_none("confidence None", result["size_sqft_confidence"])


# ---------------------------------------------------------------------------
# Test 8: NLP size 545 for listing 174140525 -- passes >= 100 rule
# ---------------------------------------------------------------------------

def test_nlp_wins_over_epc_when_sane():
    print("Test 8: NLP 545 passes sanity, wins over EPC 474")
    result = merge_size_sqft(
        structured=None,
        nlp=545,
        floorplan_vision=54,  # OCR digit-drop from model, fails < 100
        epc_interpolated=None,
        epc_direct=474,
        bedrooms=1,
    )
    assert_eq("nlp wins", result["size_sqft"], 545)
    assert_eq("source nlp", result["size_sqft_source"], "nlp")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    tests = [
        test_source_trust_order,
        test_sanity_rule_min_100,
        test_sanity_rule_per_bedroom_cap,
        test_epc_floorplan_disagreement_warning,
        test_provenance_source,
        test_provenance_confidence,
        test_no_source,
        test_nlp_wins_over_epc_when_sane,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as exc:
            print(f"  {exc}")
            failed += 1
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"  ERROR [{t.__name__}]: {exc}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
