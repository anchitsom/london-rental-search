"""
Tests for the slim floorplan extractor contract.

Run:
  .venv/bin/python tests/test_floorplan_slim.py
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run
from homehunt.floorplan_vision import (
    _USER_PROMPT,
    _parse_model_response,
    extract_floorplan_data,
)


_RIGHTMOVE_URL = "https://media.rightmove.co.uk/property-floorplan/abc123/88024626/abc123def456.jpeg"

_CAPTURED_MODEL_BODY = {
    "total_size_sqft": 690,
    "total_size_sqm": 64.1,
    "rooms": [
        {
            "name": "BEDROOM",
            "size_sqft": 130,
            "size_sqm": 12.1,
            "dimensions": "12ft 6in x 10ft 4in",
        },
        {
            "name": "BEDROOM",
            "size_sqft": 112,
            "size_sqm": 10.4,
            "dimensions": "11ft 2in x 10ft 0in",
        },
        {
            "name": "KITCHEN",
            "size_sqft": 90,
            "size_sqm": 8.4,
            "dimensions": "10ft 0in x 9ft 0in",
        },
    ],
    "has_balcony": True,
    "has_garden": False,
    "compass": "SE",
    "notes": "Two-bedroom flat with separate kitchen.",
}


def assert_eq(label: str, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


def assert_not_in(label: str, item, container):
    if item in container:
        raise AssertionError(f"FAIL [{label}]: {item!r} unexpectedly present in {container!r}")
    print(f"  PASS [{label}]")


def test_parser_drops_per_room_sizes():
    print("Test 1: parser drops per-room sizes but keeps names and totals")
    result = _parse_model_response(json.dumps(_CAPTURED_MODEL_BODY))

    assert_eq("total_size_sqft", result["total_size_sqft"], 690)
    assert_eq("total_size_sqm", result["total_size_sqm"], 64.1)
    assert_eq("rooms len", len(result["rooms"]), 3)
    assert_eq("first room name", result["rooms"][0]["name"], "BEDROOM")
    for idx, room in enumerate(result["rooms"]):
        assert_not_in(f"room {idx} size_sqft removed", "size_sqft", room)
        assert_not_in(f"room {idx} size_sqm removed", "size_sqm", room)
        assert_not_in(f"room {idx} dimensions removed", "dimensions", room)
    assert_eq("bedroom count", result["room_count"]["bedroom"], 2)
    assert_eq("kitchen count", result["room_count"]["kitchen"], 1)
    assert_eq("has_balcony", result["has_balcony"], True)
    assert_eq("has_garden", result["has_garden"], False)
    assert_eq("compass", result["compass"], "SE")
    assert_eq("raw_notes", result["raw_notes"], "Two-bedroom flat with separate kitchen.")


def test_prompt_schema_is_slim():
    print("Test 2: prompt room schema does not request per-room sizes")
    room_schema = _USER_PROMPT.split('"rooms"', 1)[1].split('"has_balcony"', 1)[0]
    assert_not_in("prompt size_sqft", "size_sqft", room_schema)
    assert_not_in("prompt size_sqm", "size_sqm", room_schema)
    assert_not_in("prompt dimensions", "dimensions", room_schema)


async def test_ollama_num_predict_is_slim():
    print("Test 3: Ollama payload uses slim num_predict")
    mock_image_resp = MagicMock()
    mock_image_resp.raise_for_status = MagicMock()
    mock_image_resp.content = b"fake-image"

    mock_ollama_resp = MagicMock()
    mock_ollama_resp.raise_for_status = MagicMock()
    mock_ollama_resp.json = MagicMock(return_value={"message": {"content": json.dumps(_CAPTURED_MODEL_BODY)}})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_image_resp)
    mock_client.post = AsyncMock(return_value=mock_ollama_resp)

    with patch("homehunt.floorplan_vision.httpx.AsyncClient", return_value=mock_client):
        await extract_floorplan_data(_RIGHTMOVE_URL)

    payload = mock_client.post.call_args.kwargs["json"]
    assert_eq("num_predict", payload["options"]["num_predict"], 160)
    assert_eq("num_ctx", payload["options"]["num_ctx"], 4096)


def test_reconcile_size_handles_rooms_without_sizes():
    print("Test 4: _reconcile_size handles rooms without sizes")
    floorplan = {
        "total_size_sqft": None,
        "rooms": [{"name": "BEDROOM"}, {"name": "KITCHEN"}],
    }
    assert_eq("reconciled size", run._reconcile_size(floorplan), None)


async def main():
    sync_tests = [
        test_parser_drops_per_room_sizes,
        test_prompt_schema_is_slim,
        test_reconcile_size_handles_rooms_without_sizes,
    ]
    async_tests = [test_ollama_num_predict_is_slim]
    passed = 0
    failed = 0
    for test in sync_tests:
        try:
            test()
            passed += 1
        except Exception as exc:
            print(f"  FAIL [{test.__name__}]: {exc}")
            failed += 1
    for test in async_tests:
        try:
            await test()
            passed += 1
        except Exception as exc:
            print(f"  FAIL [{test.__name__}]: {exc}")
            failed += 1

    print(f"\nSummary: {passed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
