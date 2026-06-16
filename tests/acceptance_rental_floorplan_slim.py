"""
Acceptance test for rental-floorplan-slim.

Run:
  .venv/bin/python tests/acceptance_rental_floorplan_slim.py
"""

import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import run
from homehunt import floorplan_vision


CAPTURED_MODEL_JSON = json.dumps(
    {
        "total_size_sqft": 775,
        "total_size_sqm": 72.0,
        "rooms": [
            {
                "name": "BEDROOM",
                "size_sqft": 140,
                "size_sqm": 13.0,
                "dimensions": "12ft 8in x 11ft 0in",
            },
            {
                "name": "BEDROOM",
                "size_sqft": 120,
                "size_sqm": 11.1,
                "dimensions": "11ft 2in x 10ft 9in",
            },
            {
                "name": "RECEPTION ROOM",
                "size_sqft": 260,
                "size_sqm": 24.2,
                "dimensions": "17ft 0in x 15ft 4in",
            },
            {
                "name": "KITCHEN",
                "size_sqft": 95,
                "size_sqm": 8.8,
                "dimensions": "10ft 6in x 9ft 0in",
            },
        ],
        "has_balcony": True,
        "has_garden": False,
        "compass": "NW",
        "notes": "Two-bedroom flat with reception room and separate kitchen.",
    }
)

RIGHTMOVE_URL = "https://media.rightmove.co.uk/property-floorplan/abc123/88024626/abc123def456.jpeg"


async def _capture_payload():
    mock_image_resp = MagicMock()
    mock_image_resp.raise_for_status = MagicMock()
    mock_image_resp.content = b"fake-image"

    mock_ollama_resp = MagicMock()
    mock_ollama_resp.raise_for_status = MagicMock()
    mock_ollama_resp.json = MagicMock(return_value={"message": {"content": CAPTURED_MODEL_JSON}})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_image_resp)
    mock_client.post = AsyncMock(return_value=mock_ollama_resp)

    with patch("homehunt.floorplan_vision.httpx.AsyncClient", return_value=mock_client):
        await floorplan_vision.extract_floorplan_data(RIGHTMOVE_URL)
    return mock_client.post.call_args.kwargs["json"]


def _record(results, number, label, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    suffix = f" - {detail}" if detail else ""
    print(f"{status} criterion {number}: {label}{suffix}")
    results.append(passed)


def main():
    results = []
    parsed = floorplan_vision._parse_model_response(CAPTURED_MODEL_JSON)

    _record(
        results,
        1,
        "top-level total sizes are preserved",
        parsed["total_size_sqft"] == 775 and parsed["total_size_sqm"] == 72.0,
    )

    forbidden_room_keys = {"size_sqft", "size_sqm", "dimensions"}
    rooms_have_names_only = all(
        isinstance(room, dict)
        and room.get("name")
        and not (forbidden_room_keys & set(room))
        for room in parsed["rooms"]
    )
    _record(results, 2, "rooms keep names and omit per-room size fields", rooms_have_names_only)

    _record(
        results,
        3,
        "room_count is derived from room names",
        parsed["room_count"].get("bedroom") == 2
        and parsed["room_count"].get("reception") == 1
        and parsed["room_count"].get("kitchen") == 1,
    )

    _record(
        results,
        4,
        "balcony garden compass and raw_notes are preserved",
        parsed["has_balcony"] is True
        and parsed["has_garden"] is False
        and parsed["compass"] == "NW"
        and parsed["raw_notes"] == "Two-bedroom flat with reception room and separate kitchen.",
    )

    with contextlib.redirect_stdout(io.StringIO()):
        payload = asyncio.run(_capture_payload())
    _record(
        results,
        5,
        "Ollama options use num_predict 160 and num_ctx 4096",
        payload["options"].get("num_predict") == 160 and payload["options"].get("num_ctx") == 4096,
    )

    room_schema = floorplan_vision._USER_PROMPT.split('"rooms"', 1)[1].split('"has_balcony"', 1)[0]
    _record(
        results,
        6,
        "prompt room schema omits per-room size fields",
        all(field not in room_schema for field in ("size_sqft", "size_sqm", "dimensions")),
    )

    floorplan_without_sizes = {
        "total_size_sqft": None,
        "rooms": [{"name": "BEDROOM"}, {"name": "KITCHEN"}],
    }
    try:
        reconciled = run._reconcile_size(floorplan_without_sizes)
        reconcile_ok = reconciled is None
    except Exception:
        reconcile_ok = False
    _record(results, 7, "_reconcile_size returns None without room sizes", reconcile_ok)

    env = dict(os.environ)
    env.pop("FLOORPLAN_LIVE_TEST", None)
    existing = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "tests" / "test_floorplan_vision.py")],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    _record(
        results,
        8,
        "pre-existing non-live floorplan tests pass",
        existing.returncode == 0,
        "" if existing.returncode == 0 else existing.stdout.strip().splitlines()[-1],
    )

    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
