"""
Smoke test: carpet detection against qwen3-vl:2b returns the expected JSON
contract.

Asserts:
  - The carpet module's default VISION_MODEL is qwen3-vl:2b after the
    pre-Wave-3 swap.
  - One labelled fixture URL goes through _query_vision_model_room_aware
    and returns a dict with all three required keys (room_type, carpet,
    confidence) and the right Python types.
  - JSON parsing succeeds (no JSONDecodeError leaking out).

Skip path: if Ollama is not reachable on 127.0.0.1:11434, the test prints a
SKIP message and exits 0. This keeps the test usable in environments without
the local model server (CI, container without GPU passthrough).

Run with:
    .venv/bin/python tests/test_carpet_qwen3vl_smoke.py
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from homehunt.carpet import (  # noqa: E402
    OLLAMA_URL,
    VISION_MODEL,
    _query_vision_model_room_aware,
)

# A real listing image URL pulled from the production data.homehunt.db
# at A/B time. Bedroom carpet=False per the existing llava-phi3 prediction;
# the smoke only checks contract conformance, not carpet correctness.
SMOKE_IMAGE_URL = (
    "https://media.rightmove.co.uk/property-photo/491fde32b/88159476/"
    "491fde32bd3ab97917fd56793a969df6.jpeg"
)


async def _run() -> int:
    # 1. Constant assertion: post-swap default must be qwen3-vl:2b.
    assert VISION_MODEL == "qwen3-vl:2b", (
        f"VISION_MODEL is {VISION_MODEL!r}, expected qwen3-vl:2b after the "
        f"pre-Wave-3 swap. Set the default in homehunt/carpet.py back to "
        f"qwen3-vl:2b or override the VISION_MODEL env var."
    )

    # 2. Reachability: skip cleanly if Ollama is not running.
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            r = await client.get(f"{OLLAMA_URL}/api/version")
            r.raise_for_status()
        except Exception as exc:
            print(f"SKIP: Ollama unreachable at {OLLAMA_URL}: {exc}")
            return 0

        # 3. Confirm the model is loaded; if not, skip rather than fail.
        try:
            tags = await client.get(f"{OLLAMA_URL}/api/tags")
            tags.raise_for_status()
            names = [m.get("name", "") for m in tags.json().get("models", [])]
            if not any(VISION_MODEL in n for n in names):
                print(
                    f"SKIP: model {VISION_MODEL} not found in Ollama. "
                    f"Run `ollama pull {VISION_MODEL}` first."
                )
                return 0
        except Exception as exc:
            print(f"SKIP: cannot list Ollama models: {exc}")
            return 0

        # 4. Fetch a real listing photo.
        try:
            img_resp = await client.get(
                SMOKE_IMAGE_URL,
                headers={"User-Agent": "Mozilla/5.0"},
                follow_redirects=True,
                timeout=20.0,
            )
            img_resp.raise_for_status()
            img_b64 = base64.b64encode(img_resp.content).decode()
        except Exception as exc:
            print(f"SKIP: cannot fetch smoke image: {exc}")
            return 0

        # 5. Run the production detector path against one photo. This goes
        # through json.loads internally; if the model returned a Python
        # True/False literal or a malformed body, the helper returns None.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        ) as inner:
            parsed = await _query_vision_model_room_aware(inner, img_b64)

    # 6. Contract assertions: must return a dict with the three required keys
    # and the right types.
    assert parsed is not None, (
        "carpet detector returned None. The model either failed JSON parsing "
        "or omitted required keys. Check logs for carpet_room_aware_failed "
        "or carpet_room_aware_missing_keys."
    )
    assert isinstance(parsed, dict), f"expected dict, got {type(parsed)}"
    for k in ("room_type", "carpet", "confidence"):
        assert k in parsed, f"missing required key {k!r}"
    assert isinstance(parsed["room_type"], str), (
        f"room_type must be str, got {type(parsed['room_type'])}"
    )
    assert isinstance(parsed["carpet"], bool), (
        f"carpet must be bool, got {type(parsed['carpet'])}: {parsed['carpet']!r}"
    )
    assert isinstance(parsed["confidence"], (int, float)), (
        f"confidence must be numeric, got {type(parsed['confidence'])}"
    )
    print(
        f"PASS: model={VISION_MODEL} parsed cleanly. "
        f"room_type={parsed['room_type']!r} "
        f"carpet={parsed['carpet']} "
        f"confidence={parsed['confidence']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
