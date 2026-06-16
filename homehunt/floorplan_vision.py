"""
Floorplan vision enrichment via Ollama + qwen2.5vl:3b.

Public function: extract_floorplan_data(image_url: str) -> dict

Calls the local Ollama multimodal API with qwen2.5vl:3b.

URL filtering: Foxtons-hosted floorplans
(www.foxtons.co.uk/.../large_floorplan) return an HTML wrapper, not a raw
image binary.  Those URLs are skipped before any Ollama call.

Returned dict fields:
    total_size_sqft   int | None
    total_size_sqm    float | None
    rooms             list[dict]  each: {name}
    room_count        dict  {bedroom, bathroom, reception, kitchen, other}
    has_balcony       bool | None
    has_garden        bool | None
    compass           str | None  e.g. "N", "S", "E", "W", "NE", etc.
    raw_notes         str | None  free-text notes from the model
    error             str | None  set when the call failed or URL was skipped
"""

import asyncio
import base64
import json
import os
import re
from typing import Optional
from urllib.parse import urlparse

import httpx
from structlog import get_logger

logger = get_logger()

OLLAMA_BASE = "http://127.0.0.1:11434"
FLOORPLAN_MODEL = "qwen2.5vl:3b"

# Bound concurrent floorplan vision calls. A 3B vision model can crash GGML
# under heavy parallel load on a single GPU, mirroring the carpet pattern.
# The default of 2 is a safe ceiling on Apple Silicon Metal; raise via env
# only after confirming the runner is stable.
_FLOORPLAN_SEM = asyncio.Semaphore(int(os.environ.get("FLOORPLAN_MAX_CONCURRENT", "2")))

_FOXTONS_PATTERN = re.compile(
    r"https?://www\.foxtons\.co\.uk/.+/large_floorplan",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = (
    "You are a floorplan data extractor.  Respond ONLY with valid JSON.  "
    "Use ASCII-only strings. Never embed raw quote characters inside a JSON "
    "string value."
)

_USER_PROMPT = """\
Analyse this residential floorplan image and return a single JSON object with
exactly the following keys.  All string values must use ASCII characters only.

{
  "total_size_sqft": <integer or null>,
  "total_size_sqm": <number or null>,
  "rooms": [
    {
      "name": "<room label e.g. BEDROOM, KITCHEN, RECEPTION ROOM, BATHROOM>"
    }
  ],
  "has_balcony": <true | false | null>,
  "has_garden": <true | false | null>,
  "compass": "<one of N NE E SE S SW W NW or null if no compass rose>",
  "notes": "<brief one-sentence description of the layout>"
}

Rules:
- If total size is not annotated, set total_size_sqft and total_size_sqm
  to null.
- Include every labelled room, including hallways, WC, and storage.
"""


def is_foxtons_url(url: str) -> bool:
    """Return True if the URL points to a Foxtons large_floorplan endpoint."""
    return bool(_FOXTONS_PATTERN.match(url))


def is_supported_floorplan_host(url: str) -> bool:
    """Return True if the URL host is one we know returns a usable image binary.

    Rightmove media CDNs and the Zoopla imgproxy CDN both serve PNG/JPEG bytes
    that the vision model can ingest. Foxtons is excluded by a separate gate
    because foxtons.co.uk returns HTML wrappers, not image bytes.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host in ("media.rightmove.co.uk", "media2.rightmove.co.uk"):
        return True
    return host == "lc.zoocdn.com" or host.endswith(".zoocdn.com")


def _categorise_rooms(rooms: list[dict]) -> dict[str, int]:
    """Count rooms by broad category from a list of room dicts."""
    counts: dict[str, int] = {
        "bedroom": 0,
        "bathroom": 0,
        "reception": 0,
        "kitchen": 0,
        "other": 0,
    }
    for room in rooms:
        name = (room.get("name") or "").lower()
        if "bedroom" in name or "master" in name:
            counts["bedroom"] += 1
        elif "bathroom" in name or "shower" in name or "wc" in name or "toilet" in name:
            counts["bathroom"] += 1
        elif "reception" in name or "living" in name or "lounge" in name or "dining" in name:
            counts["reception"] += 1
        elif "kitchen" in name:
            counts["kitchen"] += 1
        else:
            counts["other"] += 1
    return counts


def _empty_result(error: str) -> dict:
    return {
        "total_size_sqft": None,
        "total_size_sqm": None,
        "rooms": [],
        "room_count": {"bedroom": 0, "bathroom": 0, "reception": 0, "kitchen": 0, "other": 0},
        "has_balcony": None,
        "has_garden": None,
        "compass": None,
        "raw_notes": None,
        "error": error,
    }


def _parse_model_response(raw_text: str) -> dict:
    """
    Parse the model's JSON response.  Returns a normalised dict or raises
    ValueError on parse failure.
    """
    # Strip markdown code fences if present
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove opening fence (possibly ```json)
        lines = lines[1:]
        # Remove closing fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    data = json.loads(text)

    rooms = [
        {"name": room.get("name")}
        for room in (data.get("rooms") or [])
        if isinstance(room, dict) and room.get("name") is not None
    ]
    room_count = _categorise_rooms(rooms)

    return {
        "total_size_sqft": data.get("total_size_sqft"),
        "total_size_sqm": data.get("total_size_sqm"),
        "rooms": rooms,
        "room_count": room_count,
        "has_balcony": data.get("has_balcony"),
        "has_garden": data.get("has_garden"),
        "compass": data.get("compass"),
        "raw_notes": data.get("notes"),
        "error": None,
    }


async def _fetch_image_b64(image_url: str, client: httpx.AsyncClient) -> Optional[str]:
    """Fetch an image URL and return its base64-encoded content."""
    resp = await client.get(image_url)
    resp.raise_for_status()
    return base64.b64encode(resp.content).decode("ascii")


async def extract_floorplan_data(image_url: str, timeout: float = 90.0) -> dict:
    """
    Extract floorplan data from a single image URL using Ollama qwen2.5vl:3b.

    Foxtons URLs are skipped (they return HTML, not image binary).
    Non-Rightmove-hosted URLs are skipped.

    The image is fetched and base64-encoded before being sent to Ollama;
    Ollama's /api/chat endpoint does not accept URLs directly.

    Returns a dict with keys: total_size_sqft, total_size_sqm, rooms,
    room_count, has_balcony, has_garden, compass, raw_notes, error.
    """
    if is_foxtons_url(image_url):
        logger.info("floorplan_url_skipped_foxtons", url=image_url)
        return _empty_result("foxtons_url_skipped")

    if not is_supported_floorplan_host(image_url):
        logger.info("floorplan_url_skipped_unsupported_host", url=image_url)
        return _empty_result("unsupported_host_skipped")

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Fetch the image binary and base64-encode it for Ollama.
            image_b64 = await _fetch_image_b64(image_url, client)

            payload = {
                "model": FLOORPLAN_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": _SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": _USER_PROMPT,
                        "images": [image_b64],
                    },
                ],
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": 0.0,
                    "num_predict": 160,
                    "num_ctx": 4096,
                },
            }

            async with _FLOORPLAN_SEM:
                resp = await client.post(
                    f"{OLLAMA_BASE}/api/chat",
                    json=payload,
                )
                resp.raise_for_status()
                body = resp.json()

        raw_text = body.get("message", {}).get("content", "")
        if not raw_text:
            logger.warning("floorplan_empty_response", url=image_url)
            return _empty_result("empty_response")

        result = _parse_model_response(raw_text)
        logger.info(
            "floorplan_extracted",
            url=image_url,
            total_sqft=result["total_size_sqft"],
            room_count=result["room_count"],
        )
        return result

    except json.JSONDecodeError as exc:
        logger.warning("floorplan_json_parse_error", url=image_url, error=str(exc))
        return _empty_result(f"json_parse_error: {exc}")
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "floorplan_http_error",
            url=image_url,
            status=exc.response.status_code,
        )
        return _empty_result(f"http_error_{exc.response.status_code}")
    except Exception as exc:
        logger.warning("floorplan_call_failed", url=image_url, error=str(exc))
        return _empty_result(f"call_failed: {exc}")


def filter_floorplan_urls(urls: list[str]) -> list[str]:
    """
    Given a list of floorplan URLs, return only those hosted on a CDN we
    know returns a usable image binary (Rightmove or Zoopla, not Foxtons).
    """
    return [u for u in urls if is_supported_floorplan_host(u) and not is_foxtons_url(u)]
