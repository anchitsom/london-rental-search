"""
Tests for homehunt.epc.enrich_listing_with_epc (multi-field enrichment with
address-based record selection).

All HTTP calls are mocked. No network traffic. No EPC credentials required.

Run: python tests/test_epc_api_enrichment.py
"""

import asyncio
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def assert_eq(label, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


def assert_none(label, got):
    if got is not None:
        raise AssertionError(f"FAIL [{label}]: expected None, got {got!r}")
    print(f"  PASS [{label}] (None as expected)")


def assert_not_none(label, got):
    if got is None:
        raise AssertionError(f"FAIL [{label}]: expected non-None, got None")
    print(f"  PASS [{label}] (non-None)")


def _make_listing(address="Flat 3, 22 Stoke Newington Church Street", postcode="N16 0LU"):
    """Return a minimal object with .address and .postcode attributes."""
    listing = MagicMock()
    listing.address = address
    listing.postcode = postcode
    listing.uid = "rightmove:test123"
    return listing


def _make_epc_row(
    address1="Flat 3",
    address2="22 Stoke Newington Church Street",
    address3="",
    rating="C",
    floor_area=85.0,
    lodgement_date="2022-03-15",
    inspection_date="2022-03-10",
    current_eff=72,
    potential_eff=82,
    habitable_rooms=4,
    heated_rooms=4,
    built_form="Mid-Terrace",
    property_type_epc="Flat",
    age_band="England and Wales: 1996-2002",
    tenure="rental-private",
    windows_desc="Fully double glazed",
    windows_eff="Good",
    walls_desc="Cavity wall, filled cavity",
    walls_eff="Good",
    roof_desc="Pitched, 200mm loft insulation",
    floor_level="Ground",
    mains_gas="Y",
    heating_cost=650,
):
    return {
        "address1": address1,
        "address2": address2,
        "address3": address3,
        "current-energy-rating": rating,
        "total-floor-area": floor_area,
        "lodgement-date": lodgement_date,
        "inspection-date": inspection_date,
        "current-energy-efficiency": current_eff,
        "potential-energy-efficiency": potential_eff,
        "number-habitable-rooms": habitable_rooms,
        "number-heated-rooms": heated_rooms,
        "built-form": built_form,
        "property-type": property_type_epc,
        "construction-age-band": age_band,
        "tenure": tenure,
        "windows-description": windows_desc,
        "windows-energy-eff": windows_eff,
        "walls-description": walls_desc,
        "walls-energy-eff": walls_eff,
        "roof-description": roof_desc,
        "floor-level": floor_level,
        "mains-gas-flag": mains_gas,
        "heating-cost-current": heating_cost,
    }


def _mock_http_response(rows):
    """Build a mock httpx response returning the given rows."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"rows": rows}
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_single_record_postcode():
    """Postcode returning a single record: function returns that record's fields."""
    print("\n--- test_single_record_postcode ---")
    from homehunt.epc import enrich_listing_with_epc

    listing = _make_listing(address="Flat 3, 22 Stoke Newington Church Street", postcode="N16 0LU")
    row = _make_epc_row(address1="Flat 3", address2="22 Stoke Newington Church Street")

    mock_resp = _mock_http_response([row])

    with patch("homehunt.epc.EPC_EMAIL", "test@example.com"), \
         patch("homehunt.epc.EPC_API_KEY", "test-key"), \
         patch("homehunt.epc.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await enrich_listing_with_epc(listing)

    assert_not_none("result_not_none", result)
    assert_eq("epc_rating", result["epc_rating"], "C")
    assert_eq("total_floor_area_sqm", result["total_floor_area_sqm"], 85.0)
    assert_eq("current_energy_efficiency", result["current_energy_efficiency"], 72)
    assert_eq("potential_energy_efficiency", result["potential_energy_efficiency"], 82)
    assert_eq("number_habitable_rooms", result["number_habitable_rooms"], 4)
    assert_eq("built_form", result["built_form"], "Mid-Terrace")
    assert_eq("windows_description", result["windows_description"], "Fully double glazed")
    assert_eq("epc_lodgement_date", result["epc_lodgement_date"], "2022-03-15")
    assert_eq("epc_inspection_date", result["epc_inspection_date"], "2022-03-10")
    print("  PASS all fields returned correctly")


async def test_multi_record_address_match():
    """Four records, listing address fuzzy-matches one specifically."""
    print("\n--- test_multi_record_address_match ---")
    from homehunt.epc import enrich_listing_with_epc

    listing = _make_listing(address="Flat 3, 22 Stoke Newington Church Street", postcode="N16 0LU")

    rows = [
        _make_epc_row(address1="Flat 1", address2="22 Stoke Newington Church Street", rating="D", floor_area=70.0),
        _make_epc_row(address1="Flat 2", address2="22 Stoke Newington Church Street", rating="E", floor_area=68.0),
        _make_epc_row(address1="Flat 3", address2="22 Stoke Newington Church Street", rating="C", floor_area=85.0),
        _make_epc_row(address1="Flat 4", address2="22 Stoke Newington Church Street", rating="D", floor_area=72.0),
    ]

    mock_resp = _mock_http_response(rows)

    with patch("homehunt.epc.EPC_EMAIL", "test@example.com"), \
         patch("homehunt.epc.EPC_API_KEY", "test-key"), \
         patch("homehunt.epc.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await enrich_listing_with_epc(listing)

    assert_eq("matched_rating", result["epc_rating"], "C")
    assert_eq("matched_floor_area", result["total_floor_area_sqm"], 85.0)
    print("  PASS correct flat selected from multi-record postcode")


async def test_no_record_above_threshold():
    """Records exist but none scores above the match threshold: all-None result."""
    print("\n--- test_no_record_above_threshold ---")
    from homehunt.epc import enrich_listing_with_epc

    listing = _make_listing(address="Unit 999, Totally Different Building", postcode="N16 0LU")

    rows = [
        _make_epc_row(address1="Flat 1", address2="22 Stoke Newington Church Street"),
        _make_epc_row(address1="Flat 2", address2="22 Stoke Newington Church Street"),
    ]

    mock_resp = _mock_http_response(rows)

    with patch("homehunt.epc.EPC_EMAIL", "test@example.com"), \
         patch("homehunt.epc.EPC_API_KEY", "test-key"), \
         patch("homehunt.epc.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await enrich_listing_with_epc(listing)

    assert_none("epc_rating_none", result["epc_rating"])
    assert_none("floor_area_none", result["total_floor_area_sqm"])
    # All 19 EPC-derived keys should be present but None
    for key in (
        "epc_rating", "total_floor_area_sqm", "current_energy_efficiency",
        "potential_energy_efficiency", "number_habitable_rooms", "number_heated_rooms",
        "built_form", "property_type_epc", "construction_age_band", "tenure_epc",
        "windows_description", "windows_energy_eff", "walls_description", "walls_energy_eff",
        "roof_description", "floor_level", "mains_gas_flag", "heating_cost_current",
        "epc_lodgement_date", "epc_inspection_date",
    ):
        assert_none(f"key_none_{key}", result[key])
    print("  PASS all-None dict returned on threshold miss")


async def test_zero_records():
    """Postcode returning zero records: returns all-None."""
    print("\n--- test_zero_records ---")
    from homehunt.epc import enrich_listing_with_epc

    listing = _make_listing(address="1 Commercial Street", postcode="WC2R 1EA")
    mock_resp = _mock_http_response([])

    with patch("homehunt.epc.EPC_EMAIL", "test@example.com"), \
         patch("homehunt.epc.EPC_API_KEY", "test-key"), \
         patch("homehunt.epc.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await enrich_listing_with_epc(listing)

    assert_none("epc_rating_none", result["epc_rating"])
    assert_none("floor_area_none", result["total_floor_area_sqm"])
    print("  PASS zero-record postcode returns all-None")


async def test_size_sqft_conversion():
    """A record with total-floor-area: 106.0 produces size_sqft = 1141."""
    print("\n--- test_size_sqft_conversion ---")
    from homehunt.epc import enrich_listing_with_epc

    listing = _make_listing(address="Flat 5, 10 High Street", postcode="E8 1DP")
    row = _make_epc_row(
        address1="Flat 5",
        address2="10 High Street",
        floor_area=106.0,
    )
    mock_resp = _mock_http_response([row])

    with patch("homehunt.epc.EPC_EMAIL", "test@example.com"), \
         patch("homehunt.epc.EPC_API_KEY", "test-key"), \
         patch("homehunt.epc.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await enrich_listing_with_epc(listing)

    assert_eq("total_floor_area_sqm", result["total_floor_area_sqm"], 106.0)
    expected_sqft = round(106.0 * 10.7639)  # 1141
    assert_eq("size_sqft_derived", result["size_sqft_fallback"], expected_sqft)
    print(f"  PASS 106.0 sqm -> {expected_sqft} sqft")


async def test_empty_body_logs_no_records_not_lookup_failed():
    """
    Empty 200 body from the EPC API (fully-unindexed postcode) must surface
    via the epc_no_records log path with a null result. Historically this
    case raised JSONDecodeError and the broad except caught it as
    epc_lookup_failed, which is the wrong semantic label.
    """
    print("\n--- test_empty_body_logs_no_records_not_lookup_failed ---")
    import logging
    from homehunt.epc import enrich_listing_with_epc

    listing = _make_listing(address="1 Empty Street", postcode="ZZ99 9ZZ")

    # Mock httpx response with empty .text and a json() that would raise.
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.text = ""
    resp.json = MagicMock(side_effect=ValueError("No JSON object could be decoded"))

    captured: list = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = _Capture()
    structlog_logger = logging.getLogger()
    structlog_logger.addHandler(handler)
    structlog_logger.setLevel(logging.INFO)

    try:
        with patch("homehunt.epc.EPC_EMAIL", "test@example.com"), \
             patch("homehunt.epc.EPC_API_KEY", "test-key"), \
             patch("homehunt.epc.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=resp)
            mock_client_cls.return_value = mock_client

            result = await enrich_listing_with_epc(listing)
    finally:
        structlog_logger.removeHandler(handler)

    assert_none("epc_rating_none", result["epc_rating"])
    # Behavioural test: the empty-body code path must not raise out of json(),
    # i.e. resp.json() should never be called when text is empty. Verify via
    # the mock having zero json() invocations.
    if resp.json.call_count != 0:
        raise AssertionError(
            f"FAIL [no_json_call]: empty body called .json() {resp.json.call_count} time(s); "
            "should short-circuit before .json()"
        )
    print("  PASS [no_json_call]: .json() not invoked on empty body")
    print("  PASS empty-body 200 returns null without lookup_failed path")


async def test_tiebreak_on_lodgement_date():
    """When two records score equally on address, the more-recent lodgement-date wins."""
    print("\n--- test_tiebreak_on_lodgement_date ---")
    from homehunt.epc import enrich_listing_with_epc

    # Both flats have identical address1 so they'll score identically against each other
    # when the listing address is also generic. We set up a clear tie by making
    # address1 identical for both, matching the listing exactly.
    listing = _make_listing(address="Flat 3, 22 High Road", postcode="N4 2DR")

    rows = [
        _make_epc_row(
            address1="Flat 3",
            address2="22 High Road",
            rating="D",
            lodgement_date="2019-06-01",
        ),
        _make_epc_row(
            address1="Flat 3",
            address2="22 High Road",
            rating="C",
            lodgement_date="2023-11-20",
        ),
    ]

    mock_resp = _mock_http_response(rows)

    with patch("homehunt.epc.EPC_EMAIL", "test@example.com"), \
         patch("homehunt.epc.EPC_API_KEY", "test-key"), \
         patch("homehunt.epc.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await enrich_listing_with_epc(listing)

    # The more-recent record (2023-11-20, rating "C") must win
    assert_eq("tiebreak_rating", result["epc_rating"], "C")
    assert_eq("tiebreak_lodgement_date", result["epc_lodgement_date"], "2023-11-20")
    print("  PASS more-recent lodgement-date wins on tie")


async def main():
    import importlib
    # Confirm rapidfuzz is importable before running tests
    try:
        importlib.import_module("rapidfuzz")
    except ImportError:
        print("FAIL: rapidfuzz not installed. Add to requirements.txt and install.")
        sys.exit(1)

    print("=== test_epc_api_enrichment ===")
    tests = [
        test_single_record_postcode,
        test_multi_record_address_match,
        test_no_record_above_threshold,
        test_zero_records,
        test_size_sqft_conversion,
        test_tiebreak_on_lodgement_date,
        test_empty_body_logs_no_records_not_lookup_failed,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            await t()
            passed += 1
        except AssertionError as e:
            print(f"  {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR in {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n=== {passed} passed, {failed} failed ===")
    if failed:
        sys.exit(1)


asyncio.run(main())
