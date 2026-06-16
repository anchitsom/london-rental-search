"""
Wave 1.7 Item 4: EPC unit gate (precision floor).

Cases covered:
  1. Round 2 finding rightmove:87972531: listing "3-5 Crouch End Hill" must
     REJECT the EPC row "Flat 8, Zachary Lodge / 33-35 Crouch End Hill".
  2. Round 2 finding rightmove:87876822: listing "117 Junction Road" must
     REJECT row "Flat 3, 115 Junction Road".
  3. Regression: listing "22 Baron Street" must PASS row "22a, Baron Street"
     because the bare number 22 is recoverable from "22a".
  4. Building-name case: listing "Solar House, 915 High Road" must PASS a
     row whose address1 is "Flat 4, Solar House" because the listing has no
     leading numeric token (the gate falls open).
  5. Wired into _select_best_row: with one bad row at high score and one
     good row at any score, the gate forces the good row to be selected.
  6. All-fail case: when every above-threshold row fails the gate,
     _select_best_row returns None.

Cached EPC rows for 87972531 and 87876822 from
epc_enrichment/round2/output/epc_rows_control/.

Run:
  .venv/bin/python tests/test_stage_epc_unit_gate.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homehunt.epc import _select_best_row
from homehunt.epc_unit_gate import passes_unit_gate

_FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "epc_enrichment", "round2", "output", "epc_rows_control",
)


def assert_true(label: str, val: bool):
    if not val:
        raise AssertionError(f"FAIL [{label}]: expected True, got False")
    print(f"  PASS [{label}]")


def assert_false(label: str, val: bool):
    if val:
        raise AssertionError(f"FAIL [{label}]: expected False, got True")
    print(f"  PASS [{label}]")


def assert_eq(label: str, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


def assert_none(label: str, got):
    if got is not None:
        raise AssertionError(f"FAIL [{label}]: expected None, got {got!r}")
    print(f"  PASS [{label}] (None as expected)")


def _load_rows(uid: str) -> list[dict]:
    path = os.path.join(_FIXTURE_DIR, f"{uid}.json")
    with open(path, "r") as f:
        cached = json.load(f)
    return cached.get("rows", [])


def _find_row(rows: list[dict], address1_substr: str, address2_substr: str = "") -> dict:
    """Return the first cached row whose address1+2 contains the given substrings."""
    for r in rows:
        a1 = (r.get("address1") or "")
        a2 = (r.get("address2") or "")
        if address1_substr in a1 and (not address2_substr or address2_substr in a2):
            return r
    raise AssertionError(
        f"fixture row not found: address1 contains {address1_substr!r}, "
        f"address2 contains {address2_substr!r}"
    )


# ---------------------------------------------------------------------------
# Test 1: 87972531 -- 3-5 Crouch End Hill rejects 33-35 Crouch End Hill rows
# ---------------------------------------------------------------------------

def test_87972531_rejects_33_35_crouch_end_hill():
    print("Test 1: 87972531 listing '3-5 Crouch End Hill' rejects '33-35' rows")
    rows = _load_rows("rightmove_87972531")
    listing_addr = "3-5 Crouch End Hill, N8 8DH"

    # Bad row: Flat 8, Zachary Lodge / 33-35 Crouch End Hill
    bad = _find_row(rows, "Flat 8, Zachary Lodge", "33-35 Crouch End Hill")
    assert_false("bad 33-35 row rejected", passes_unit_gate(listing_addr, bad))

    # Good row: Flat 3 / 3-5 Crouch End Hill
    good = _find_row(rows, "Flat 3", "3-5 Crouch End Hill")
    assert_true("good 3-5 row passes", passes_unit_gate(listing_addr, good))


# ---------------------------------------------------------------------------
# Test 2: 87876822 -- 117 Junction Road rejects Flat 3, 115 Junction Road
# ---------------------------------------------------------------------------

def test_87876822_rejects_115_junction_road():
    print("Test 2: 87876822 listing '117 Junction Road' rejects '115' rows")
    rows = _load_rows("rightmove_87876822")
    listing_addr = "117 Junction Road, N19 5PX"

    bad = _find_row(rows, "Flat 3", "115 Junction Road")
    assert_false("bad 115 row rejected", passes_unit_gate(listing_addr, bad))

    good = _find_row(rows, "Flat C", "117 Junction Road")
    assert_true("good 117 row passes", passes_unit_gate(listing_addr, good))


# ---------------------------------------------------------------------------
# Test 3: regression -- 22 Baron Street passes 22a, Baron Street
# ---------------------------------------------------------------------------

def test_22_baron_street_passes_22a():
    print("Test 3: '22 Baron Street' passes '22a, Baron Street'")
    listing_addr = "22 Baron Street, N1 9ES"
    row = {"address1": "22a", "address2": "Baron Street"}
    assert_true("22a row passes", passes_unit_gate(listing_addr, row))


# ---------------------------------------------------------------------------
# Test 4: building-name listing -- gate falls open
# ---------------------------------------------------------------------------

def test_solar_house_building_name_falls_open():
    print("Test 4: 'Solar House, 915 High Road' has no leading number; gate falls open")
    listing_addr = "Solar House, 915 High Road"
    row = {"address1": "Flat 4, Solar House", "address2": "915 High Road"}
    assert_true("building-name row passes", passes_unit_gate(listing_addr, row))

    # Even a row that does not name "Solar House" falls open if the listing
    # has no leading number; the gate's contract is to NOT regress on the
    # building-name case.
    row_other = {"address1": "Some other place", "address2": "915 High Road"}
    assert_true("any row passes when listing has no number", passes_unit_gate(listing_addr, row_other))


# ---------------------------------------------------------------------------
# Test 5: _select_best_row applies the gate before tie-break
# ---------------------------------------------------------------------------

def test_select_best_row_applies_gate():
    """
    With a higher-scoring bad row and a lower-scoring good row, the gate must
    remove the bad row from contention so the good row is selected.
    """
    print("Test 5: _select_best_row applies the gate before tie-break")
    listing_addr = "117 Junction Road, N19 5PX"
    # The bad row's address tokens contain "junction" and "road" which token-
    # set scores high; the good row also scores high. The fuzzy matcher would
    # otherwise pick the bad one (existing baseline pre-Wave 1.7). With the
    # gate the good row wins.
    bad = {
        "address1": "Flat 3",
        "address2": "115 Junction Road",
        "current-energy-rating": "C",
        "lodgement-date": "2023-01-01",
    }
    good = {
        "address1": "Flat C",
        "address2": "117 Junction Road",
        "current-energy-rating": "D",
        "lodgement-date": "2022-01-01",
    }
    selected = _select_best_row([bad, good], listing_addr)
    assert_eq("selected.address2", selected["address2"], "117 Junction Road")
    assert_eq("selected.current-energy-rating", selected["current-energy-rating"], "D")


# ---------------------------------------------------------------------------
# Test 6: all rows fail gate -> None
# ---------------------------------------------------------------------------

def test_select_best_row_returns_none_when_all_fail_gate():
    print("Test 6: _select_best_row returns None when all rows fail the gate")
    listing_addr = "117 Junction Road, N19 5PX"
    rows = [
        {
            "address1": "Flat 1",
            "address2": "115 Junction Road",
            "current-energy-rating": "C",
            "lodgement-date": "2023-01-01",
        },
        {
            "address1": "Flat 2",
            "address2": "115 Junction Road",
            "current-energy-rating": "D",
            "lodgement-date": "2023-01-01",
        },
    ]
    selected = _select_best_row(rows, listing_addr)
    assert_none("no row selected", selected)


def main():
    tests = [
        test_87972531_rejects_33_35_crouch_end_hill,
        test_87876822_rejects_115_junction_road,
        test_22_baron_street_passes_22a,
        test_solar_house_building_name_falls_open,
        test_select_best_row_applies_gate,
        test_select_best_row_returns_none_when_all_fail_gate,
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
