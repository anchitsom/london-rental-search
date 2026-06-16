"""
Wave 1.7 Item 2: EPC vision OCR with SAP reconciliation.

All tests mock the Ollama call. No network. No image fetch.

Cases covered:
  1. Consistent read (current=68, potential=80) -> rating D, confidence sap_derived.
  2. Potential below current (current=92, potential=1) -> confidence rejected.
  3. SAP-derived letter override (model says A but current=67 maps to D)
     -> rating D, confidence sap_derived (SAP wins).
  4. Missing SAP -> confidence rejected.
  5. Image fetch HTTP error -> confidence error.

Run:
  .venv/bin/python tests/test_stage_epc_vision_reconciliation.py
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homehunt.epc_vision import (
    extract_epc_from_image,
    letter_from_sap,
    reconcile,
)


_TEST_URL = "https://media.rightmove.co.uk/property-epc/abc/123/abc123.jpeg"


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
        raise AssertionError(f"FAIL [{label}]: expected not-None")
    print(f"  PASS [{label}] (not-None)")


def assert_in(label, item, container):
    if item not in container:
        raise AssertionError(f"FAIL [{label}]: {item!r} not in {container!r}")
    print(f"  PASS [{label}]")


def _build_mock_clients(model_response_body: dict, raise_on_get: bool = False):
    """
    Build the httpx.AsyncClient mock pair the extractor uses.

    The extractor makes two calls inside one AsyncClient: GET image, POST chat.
    """
    mock_image_resp = MagicMock()
    if raise_on_get:
        mock_image_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "404", request=MagicMock(), response=MagicMock(status_code=404)
            )
        )
    else:
        mock_image_resp.raise_for_status = MagicMock()
    mock_image_resp.content = b"fake-image-bytes"

    mock_chat_resp = MagicMock()
    mock_chat_resp.raise_for_status = MagicMock()
    mock_chat_resp.json = MagicMock(
        return_value={"message": {"content": json.dumps(model_response_body)}}
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_image_resp)
    mock_client.post = AsyncMock(return_value=mock_chat_resp)
    return mock_client


# ---------------------------------------------------------------------------
# Test 1: consistent read -> sap_derived
# ---------------------------------------------------------------------------

async def test_consistent_read_returns_sap_derived():
    """
    Model returns current=68 D, potential=80 C. SAP 68 maps to D, both
    numbers are internally consistent, the read passes.
    """
    print("Test 1: consistent SAP read returns rating D with confidence sap_derived")
    body = {
        "current_rating": "D",
        "current_sap": 68,
        "potential_rating": "C",
        "potential_sap": 80,
    }
    mock_client = _build_mock_clients(body)
    with patch("homehunt.epc_vision.httpx.AsyncClient", return_value=mock_client):
        result = await extract_epc_from_image(_TEST_URL)

    assert_eq("epc_rating", result["epc_rating"], "D")
    assert_eq("confidence", result["confidence"], "sap_derived")
    assert_eq("sap_score_current", result["sap_score_current"], 68)
    assert_eq("sap_score_potential", result["sap_score_potential"], 80)
    assert_none("no error", result["error"])


# ---------------------------------------------------------------------------
# Test 2: potential below current -> rejected
# ---------------------------------------------------------------------------

async def test_potential_below_current_rejected():
    """
    Model returns current=92 with potential=1. SAP read is internally
    inconsistent because potential cannot be lower than current. Rejected.
    """
    print("Test 2: potential < current is rejected")
    body = {
        "current_rating": "A",
        "current_sap": 92,
        "potential_rating": "G",
        "potential_sap": 1,
    }
    mock_client = _build_mock_clients(body)
    with patch("homehunt.epc_vision.httpx.AsyncClient", return_value=mock_client):
        result = await extract_epc_from_image(_TEST_URL)

    assert_eq("confidence", result["confidence"], "rejected")
    assert_none("epc_rating none", result["epc_rating"])
    assert_eq("sap_score_current", result["sap_score_current"], 92)
    assert_eq("sap_score_potential", result["sap_score_potential"], 1)
    assert_in("error captures reason", "potential_sap_below_current", result["error"])


# ---------------------------------------------------------------------------
# Test 3: SAP-derived letter overrides model letter
# ---------------------------------------------------------------------------

async def test_sap_derived_letter_overrides_model_letter():
    """
    Model says current_rating=A but current_sap=67. SAP 67 falls in the D
    bucket (55-68). The reconciler must trust the SAP and return D.
    """
    print("Test 3: SAP-derived letter overrides the model's letter")
    body = {
        "current_rating": "A",
        "current_sap": 67,
        "potential_rating": "A",
        "potential_sap": 90,
    }
    mock_client = _build_mock_clients(body)
    with patch("homehunt.epc_vision.httpx.AsyncClient", return_value=mock_client):
        result = await extract_epc_from_image(_TEST_URL)

    assert_eq("epc_rating", result["epc_rating"], "D")
    assert_eq("confidence", result["confidence"], "sap_derived")
    assert_eq("sap_score_current", result["sap_score_current"], 67)


# ---------------------------------------------------------------------------
# Test 4: missing SAP -> rejected
# ---------------------------------------------------------------------------

async def test_missing_sap_rejected():
    """
    Model returns rating letters but no SAP scores. The reconciler refuses
    because it cannot verify the rating without a number.
    """
    print("Test 4: missing current SAP is rejected")
    body = {
        "current_rating": "C",
        "current_sap": None,
        "potential_rating": "B",
        "potential_sap": None,
    }
    mock_client = _build_mock_clients(body)
    with patch("homehunt.epc_vision.httpx.AsyncClient", return_value=mock_client):
        result = await extract_epc_from_image(_TEST_URL)

    assert_eq("confidence", result["confidence"], "rejected")
    assert_none("epc_rating none", result["epc_rating"])
    assert_in("error captures reason", "missing_current_sap", result["error"])


# ---------------------------------------------------------------------------
# Test 5: image fetch error -> error
# ---------------------------------------------------------------------------

async def test_image_fetch_http_error():
    """
    Image GET returns 404. The extractor must surface confidence=error and
    populate the error field.
    """
    print("Test 5: image fetch HTTP error returns confidence error")
    mock_client = _build_mock_clients({}, raise_on_get=True)
    with patch("homehunt.epc_vision.httpx.AsyncClient", return_value=mock_client):
        result = await extract_epc_from_image(_TEST_URL)

    assert_eq("confidence", result["confidence"], "error")
    assert_none("epc_rating none", result["epc_rating"])
    assert_not_none("error populated", result["error"])
    assert_in("error mentions http_error", "http_error_404", result["error"])


# ---------------------------------------------------------------------------
# Test 6: reconcile() unit tests (the lifted load-bearing function)
# ---------------------------------------------------------------------------

def test_reconcile_unit():
    """Direct unit tests on the lifted reconcile() / letter_from_sap() helpers."""
    print("Test 6: reconcile and letter_from_sap unit tests")
    assert_eq("letter 92 -> A", letter_from_sap(92), "A")
    assert_eq("letter 80 -> C", letter_from_sap(80), "C")
    assert_eq("letter 68 -> D", letter_from_sap(68), "D")
    assert_eq("letter 55 -> D", letter_from_sap(55), "D")
    assert_eq("letter 1 -> G", letter_from_sap(1), "G")
    assert_none("letter None", letter_from_sap(None))

    rec = reconcile("D", 68, "C", 80)
    assert_eq("reconcile consistent rating", rec["reconciled_rating"], "D")
    assert_eq("reconcile consistent confidence", rec["confidence"], "sap_derived")

    rec = reconcile("A", 92, "G", 1)
    assert_eq("reconcile potential<current confidence", rec["confidence"], "miss")
    assert_eq("reconcile reason", rec["reason"], "potential_sap_below_current")

    rec = reconcile("A", 67, "A", 90)
    assert_eq("reconcile SAP override rating", rec["reconciled_rating"], "D")
    assert_eq("reconcile SAP override confidence", rec["confidence"], "sap_derived")

    rec = reconcile("C", None, "B", None)
    assert_eq("reconcile missing SAP confidence", rec["confidence"], "miss")
    assert_eq("reconcile missing SAP reason", rec["reason"], "missing_current_sap")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

async def main():
    sync_tests = [test_reconcile_unit]
    async_tests = [
        test_consistent_read_returns_sap_derived,
        test_potential_below_current_rejected,
        test_sap_derived_letter_overrides_model_letter,
        test_missing_sap_rejected,
        test_image_fetch_http_error,
    ]

    passed = 0
    failed = 0
    for t in sync_tests:
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

    for t in async_tests:
        try:
            await t()
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
    asyncio.run(main())
