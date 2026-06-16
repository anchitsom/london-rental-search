"""
Tests for homehunt/floorplan_vision.py

Run:
  .venv/bin/python tests/test_floorplan_vision.py

Unit tests mock the Ollama call and run without network.
A live integration test is gated behind FLOORPLAN_LIVE_TEST=1.

Test groups:
  1. Foxtons URL filter -- skipped, no Ollama call made.
  2. Non-Rightmove URL filter -- skipped, no Ollama call made.
  3. Rightmove URL with mocked Ollama response -- full parse.
  4. Mocked response with dimension strings -- per-room sizes are discarded.
  5. Mocked response with empty rooms list -- handled gracefully.
  6. Malformed JSON response from Ollama -- error field set, no exception.
  7. room_count categorisation -- correct bucket assignment.
  8. [LIVE] Real Rightmove floorplan via live Ollama (FLOORPLAN_LIVE_TEST=1).
"""

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homehunt.floorplan_vision import (
    _FLOORPLAN_SEM,
    _categorise_rooms,
    _empty_result,
    _parse_model_response,
    extract_floorplan_data,
    filter_floorplan_urls,
    is_foxtons_url,
    is_supported_floorplan_host,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def assert_eq(label: str, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


def assert_not_none(label: str, got):
    if got is None:
        raise AssertionError(f"FAIL [{label}]: expected not-None")
    print(f"  PASS [{label}]")


def assert_none(label: str, got):
    if got is not None:
        raise AssertionError(f"FAIL [{label}]: expected None, got {got!r}")
    print(f"  PASS [{label}]")


def assert_in(label: str, item, container):
    if item not in container:
        raise AssertionError(f"FAIL [{label}]: {item!r} not in {container!r}")
    print(f"  PASS [{label}]")


_FOXTONS_URL = "https://www.foxtons.co.uk/properties-to-rent/n1/chpk1234567/large_floorplan"
_RIGHTMOVE_URL = "https://media.rightmove.co.uk/property-floorplan/abc123/88024626/abc123def456.jpeg"
_ZOOPLA_URL = "https://lc.zoocdn.com/c913d367a9fa1cf81caae06bc1fca1e816cfa5b5.jpg"
_OTHER_URL = "https://cdn.someagent.co.uk/floorplans/abc.png"

_MOCK_RESPONSE_BODY = {
    "total_size_sqft": 550,
    "total_size_sqm": 51.1,
    "rooms": [
        {
            "name": "RECEPTION ROOM",
            "size_sqft": 259,
            "size_sqm": 24.1,
            "dimensions": "16ft 8in x 15ft 6in",
        },
        {
            "name": "BEDROOM",
            "size_sqft": 139,
            "size_sqm": 12.9,
            "dimensions": "12ft 8in x 10ft 11in",
        },
        {
            "name": "KITCHEN",
            "size_sqft": 78,
            "size_sqm": 7.3,
            "dimensions": "10ft 11in x 7ft 2in",
        },
        {
            "name": "BATHROOM",
            "size_sqft": 43,
            "size_sqm": 4.0,
            "dimensions": "7ft 2in x 6ft",
        },
    ],
    "has_balcony": False,
    "has_garden": False,
    "compass": "N",
    "notes": "One-bed flat with reception, kitchen, bedroom, and bathroom.",
}

_MOCK_OLLAMA_RESPONSE = {
    "message": {
        "content": json.dumps(_MOCK_RESPONSE_BODY),
    }
}


# ---------------------------------------------------------------------------
# Test 1: Foxtons URL is filtered
# ---------------------------------------------------------------------------

async def test_foxtons_url_skipped():
    print("Test 1: Foxtons URL skipped")
    result = await extract_floorplan_data(_FOXTONS_URL)
    assert_eq("error field set", result["error"], "foxtons_url_skipped")
    assert_none("total_size_sqft", result["total_size_sqft"])
    assert_eq("rooms empty", result["rooms"], [])


# ---------------------------------------------------------------------------
# Test 2: Non-Rightmove URL is filtered
# ---------------------------------------------------------------------------

async def test_non_rightmove_url_skipped():
    print("Test 2: Non-Rightmove URL skipped")
    result = await extract_floorplan_data(_OTHER_URL)
    assert_eq("error field set", result["error"], "unsupported_host_skipped")
    assert_none("total_size_sqft", result["total_size_sqft"])


# ---------------------------------------------------------------------------
# Test 3: Rightmove URL with mocked Ollama -- full parse
# ---------------------------------------------------------------------------

async def test_rightmove_url_mocked_ollama():
    print("Test 3: Rightmove URL with mocked Ollama response")

    import base64 as _b64

    # Two HTTP calls: GET image, then POST to Ollama.
    mock_image_resp = MagicMock()
    mock_image_resp.raise_for_status = MagicMock()
    mock_image_resp.content = b"fake-image-bytes"

    mock_ollama_resp = MagicMock()
    mock_ollama_resp.raise_for_status = MagicMock()
    mock_ollama_resp.json = MagicMock(return_value=_MOCK_OLLAMA_RESPONSE)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_image_resp)
    mock_client.post = AsyncMock(return_value=mock_ollama_resp)

    with patch("homehunt.floorplan_vision.httpx.AsyncClient", return_value=mock_client):
        result = await extract_floorplan_data(_RIGHTMOVE_URL)

    assert_none("no error", result["error"])
    assert_eq("total_size_sqft", result["total_size_sqft"], 550)
    assert_eq("total_size_sqm", result["total_size_sqm"], 51.1)
    assert_eq("room count bedrooms", result["room_count"]["bedroom"], 1)
    assert_eq("room count kitchen", result["room_count"]["kitchen"], 1)
    assert_eq("room count bathroom", result["room_count"]["bathroom"], 1)
    assert_eq("room count reception", result["room_count"]["reception"], 1)
    assert_eq("has_balcony", result["has_balcony"], False)
    assert_eq("has_garden", result["has_garden"], False)
    assert_eq("compass", result["compass"], "N")
    assert_eq("rooms len", len(result["rooms"]), 4)

    # Tier 1 perf: assert the Ollama payload carries the latency-bounding
    # options. Captured from the mocked POST call.
    posted = mock_client.post.call_args
    payload = posted.kwargs["json"]
    options = payload["options"]
    assert_eq("temperature", options.get("temperature"), 0.0)
    assert_eq("num_predict cap", options.get("num_predict"), 160)
    assert_eq("num_ctx", options.get("num_ctx"), 4096)
    assert_eq("format json", payload.get("format"), "json")


# ---------------------------------------------------------------------------
# Test 4: Per-room dimension strings in response are discarded
# ---------------------------------------------------------------------------

async def test_room_dimensions_discarded():
    print("Test 4: per-room dimensions are discarded")
    body = dict(_MOCK_RESPONSE_BODY)
    body["rooms"] = [
        {
            "name": "RECEPTION ROOM",
            "size_sqft": None,
            "size_sqm": None,
            "dimensions": "16ft 8in x 15ft 6in",
        }
    ]
    mock_image_resp = MagicMock()
    mock_image_resp.raise_for_status = MagicMock()
    mock_image_resp.content = b"fake-image"

    mock_ollama_resp = MagicMock()
    mock_ollama_resp.raise_for_status = MagicMock()
    mock_ollama_resp.json = MagicMock(return_value={"message": {"content": json.dumps(body)}})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_image_resp)
    mock_client.post = AsyncMock(return_value=mock_ollama_resp)

    with patch("homehunt.floorplan_vision.httpx.AsyncClient", return_value=mock_client):
        result = await extract_floorplan_data(_RIGHTMOVE_URL)

    assert_none("no error on extra room fields", result["error"])
    assert_eq("rooms len", len(result["rooms"]), 1)
    assert_eq("room name preserved", result["rooms"][0], {"name": "RECEPTION ROOM"})


# ---------------------------------------------------------------------------
# Test 5: Empty rooms list
# ---------------------------------------------------------------------------

async def test_empty_rooms():
    print("Test 5: Empty rooms list handled gracefully")
    body = {
        "total_size_sqft": 420,
        "total_size_sqm": 39.0,
        "rooms": [],
        "has_balcony": None,
        "has_garden": None,
        "compass": None,
        "notes": "No room labels visible.",
    }
    mock_image_resp = MagicMock()
    mock_image_resp.raise_for_status = MagicMock()
    mock_image_resp.content = b"fake-image"

    mock_ollama_resp = MagicMock()
    mock_ollama_resp.raise_for_status = MagicMock()
    mock_ollama_resp.json = MagicMock(return_value={"message": {"content": json.dumps(body)}})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_image_resp)
    mock_client.post = AsyncMock(return_value=mock_ollama_resp)

    with patch("homehunt.floorplan_vision.httpx.AsyncClient", return_value=mock_client):
        result = await extract_floorplan_data(_RIGHTMOVE_URL)

    assert_none("no error", result["error"])
    assert_eq("total_size_sqft", result["total_size_sqft"], 420)
    assert_eq("rooms empty", result["rooms"], [])


# ---------------------------------------------------------------------------
# Test 6: Malformed JSON from Ollama
# ---------------------------------------------------------------------------

async def test_malformed_json():
    print("Test 6: Malformed JSON response from Ollama")
    mock_image_resp = MagicMock()
    mock_image_resp.raise_for_status = MagicMock()
    mock_image_resp.content = b"fake-image"

    mock_ollama_resp = MagicMock()
    mock_ollama_resp.raise_for_status = MagicMock()
    mock_ollama_resp.json = MagicMock(return_value={"message": {"content": "This is not JSON { unclosed"}})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_image_resp)
    mock_client.post = AsyncMock(return_value=mock_ollama_resp)

    with patch("homehunt.floorplan_vision.httpx.AsyncClient", return_value=mock_client):
        result = await extract_floorplan_data(_RIGHTMOVE_URL)

    assert_not_none("error field set", result["error"])
    assert_in("error type", "json_parse_error", result["error"])
    assert_none("total_size_sqft is none", result["total_size_sqft"])


# ---------------------------------------------------------------------------
# Test 7: room_count categorisation
# ---------------------------------------------------------------------------

def test_room_count_categorisation():
    print("Test 7: room_count categorisation")
    rooms = [
        {"name": "MASTER BEDROOM", "size_sqft": 140, "size_sqm": 13.0},
        {"name": "Bedroom 2", "size_sqft": 90, "size_sqm": 8.4},
        {"name": "KITCHEN", "size_sqft": 70, "size_sqm": 6.5},
        {"name": "RECEPTION ROOM", "size_sqft": 200, "size_sqm": 18.6},
        {"name": "BATHROOM", "size_sqft": 40, "size_sqm": 3.7},
        {"name": "WC", "size_sqft": 20, "size_sqm": 1.9},
        {"name": "HALLWAY", "size_sqft": 30, "size_sqm": 2.8},
    ]
    counts = _categorise_rooms(rooms)
    assert_eq("bedroom count", counts["bedroom"], 2)
    assert_eq("kitchen count", counts["kitchen"], 1)
    assert_eq("reception count", counts["reception"], 1)
    assert_eq("bathroom count", counts["bathroom"], 2)  # BATHROOM + WC
    assert_eq("other count", counts["other"], 1)  # HALLWAY


# ---------------------------------------------------------------------------
# Test 8: filter_floorplan_urls helper
# ---------------------------------------------------------------------------

async def test_floorplan_semaphore_caps_concurrency():
    """
    Tier 2: launch 4 concurrent extract_floorplan_data calls and assert that
    the Ollama POST is gated by _FLOORPLAN_SEM so at most 2 calls are in-flight
    inside the semaphore at any moment.
    """
    print("Test 9: floorplan semaphore caps in-flight POSTs at 2")

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def slow_post(*args, **kwargs):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            if in_flight > max_in_flight:
                max_in_flight = in_flight
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=_MOCK_OLLAMA_RESPONSE)
        return resp

    mock_image_resp = MagicMock()
    mock_image_resp.raise_for_status = MagicMock()
    mock_image_resp.content = b"fake-image"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_image_resp)
    mock_client.post = AsyncMock(side_effect=slow_post)

    with patch("homehunt.floorplan_vision.httpx.AsyncClient", return_value=mock_client):
        await asyncio.gather(*[extract_floorplan_data(_RIGHTMOVE_URL) for _ in range(4)])

    # Default FLOORPLAN_MAX_CONCURRENT is 2.
    assert_eq("max in-flight POSTs", max_in_flight, 2)


def test_filter_floorplan_urls():
    print("Test 8: filter_floorplan_urls")
    urls = [
        _FOXTONS_URL,
        _RIGHTMOVE_URL,
        _ZOOPLA_URL,
        _OTHER_URL,
        "https://media.rightmove.co.uk/abc/87952860/def.png",
    ]
    filtered = filter_floorplan_urls(urls)
    assert_eq("foxtons excluded", _FOXTONS_URL in filtered, False)
    assert_eq("other excluded", _OTHER_URL in filtered, False)
    assert_eq("rightmove kept", _RIGHTMOVE_URL in filtered, True)
    assert_eq("zoopla kept", _ZOOPLA_URL in filtered, True)
    assert_eq("rightmove + zoopla kept", len(filtered), 3)


def test_is_supported_floorplan_host():
    print("Test 8b: is_supported_floorplan_host")
    assert_eq("rightmove host", is_supported_floorplan_host(_RIGHTMOVE_URL), True)
    assert_eq("zoopla host", is_supported_floorplan_host(_ZOOPLA_URL), True)
    assert_eq("foxtons host rejected", is_supported_floorplan_host(_FOXTONS_URL), False)
    assert_eq("other host rejected", is_supported_floorplan_host(_OTHER_URL), False)


# ---------------------------------------------------------------------------
# Live integration test (gated behind env var)
# ---------------------------------------------------------------------------

async def test_live_integration():
    """
    Real Ollama call.  Requires:
      - Ollama running at 127.0.0.1:11434
      - qwen2.5vl:3b pulled
      - FLOORPLAN_LIVE_TEST=1
    """
    url = "https://media.rightmove.co.uk/property-floorplan/51c2054fe/88024626/51c2054feedece5712657fc1c5030e52.jpeg"
    result = await extract_floorplan_data(url)
    print(f"  Live result: total_sqft={result['total_size_sqft']} rooms={len(result['rooms'])} error={result['error']}")
    assert_none("no error in live call", result["error"])
    assert_not_none("got total size", result["total_size_sqft"])


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

async def main():
    tests = [
        test_foxtons_url_skipped,
        test_non_rightmove_url_skipped,
        test_rightmove_url_mocked_ollama,
        test_room_dimensions_discarded,
        test_empty_rooms,
        test_malformed_json,
        test_floorplan_semaphore_caps_concurrency,
    ]
    sync_tests = [
        test_room_count_categorisation,
        test_filter_floorplan_urls,
        test_is_supported_floorplan_host,
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
            print(f"  ERROR [{t.__name__}]: {exc}")
            failed += 1

    for t in tests:
        try:
            await t()
            passed += 1
        except AssertionError as exc:
            print(f"  {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR [{t.__name__}]: {exc}")
            failed += 1

    if os.environ.get("FLOORPLAN_LIVE_TEST") == "1":
        print("Test [LIVE]: real Ollama call")
        try:
            await test_live_integration()
            passed += 1
        except AssertionError as exc:
            print(f"  {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR [test_live_integration]: {exc}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
