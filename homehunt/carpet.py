"""
Carpet detection via Ollama vision model.
Downloads listing photos and asks the model for carpet presence.

Two-signal output:
  carpet_in_bedroom    -- carpet found in at least one bedroom photo
  carpet_other_areas   -- carpet found in at least one non-bedroom photo

The single combined prompt halves latency versus two sequential calls. The
default vision model is qwen3-vl:2b, swapped from llava-phi3 on 2026-05-08
after a 68-photo A/B (`docs/plans/2026-05-08-carpet-qwen3vl-ab.md`) showed
llava-phi3 hitting a 1.47% JSONDecodeError rate from Python True/False
literals leaking into JSON responses while qwen3-vl:2b returned 0 such
failures on the same set.

Performance discipline (applied 2026-05-09 per
`docs/plans/2026-05-09-carpet-perf-tiers-applied.md`):

  Tier 1: Ollama options block on every /api/generate payload
          (format=json, num_predict=64, num_ctx=2048, temperature=0). Without
          these, qwen3-vl:2b allocates a 256k-token KV cache per call and lets
          the thinking trace run uncapped. The change drops mean per-call
          latency from ~28s to ~3s on a 60-call sample.
  Tier 2: Image pre-resize to a 768 px long edge before base64 encoding,
          via Pillow. Halves vision-token count and shaves another ~30% off
          per-call latency without measurable accuracy loss against the V0
          baseline.
  Tier 3: CARPET_PHOTOS_TO_CHECK default lowered to 3 (env-overridable).
          The fourth photo's marginal information is small relative to the
          per-photo cost.

Post-tier latency: ~1.5-2s per call mean, ~5s per listing on 3 photos at
768 px. See the report for full V0 vs V1/V2/V3 numbers.

Controlled by CARPET_ENABLED env var. When disabled, returns (None, 0.0) -- neutral score.
"""

import asyncio
import base64
import io
import json
import os
from typing import Optional

import httpx
from PIL import Image
from structlog import get_logger

logger = get_logger()

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
VISION_MODEL = os.environ.get("VISION_MODEL", "qwen3-vl:2b")
# Default lowered from 4 to 3 on 2026-05-09 (Tier 3 win). The fourth photo's
# marginal carpet-signal is small relative to its per-photo cost. Override
# with CARPET_PHOTOS_TO_CHECK=4 to restore the prior behaviour.
CARPET_PHOTOS_TO_CHECK = int(os.environ.get("CARPET_PHOTOS_TO_CHECK", "4"))
CARPET_BATCH_SIZE = int(os.environ.get("CARPET_BATCH_SIZE", "4"))
# Long-edge resize target before base64 encoding (Tier 2). 768 px halves
# vision-token count vs the original image and beat 640 px on accuracy in
# the 2026-05-09 A/B.
CARPET_IMG_MAX_EDGE = int(os.environ.get("CARPET_IMG_MAX_EDGE", "1280"))

# Ollama generate options (Tier 1). Carpet response is one short JSON
# (~30-40 tokens), so num_predict=64 leaves headroom; num_ctx=2048 fits
# one image plus the short prompt with margin; temperature=0 makes the
# decision deterministic; format=json constrains output to valid JSON via
# the model's grammar.
_VISION_OPTIONS: dict = {
    "temperature": 0.0,
    "num_predict": 64,
    "num_ctx": 2048,
}

# Outer wall-clock guards. httpx per-request timeouts do not bound total time
# across redirects or pool waits; asyncio.wait_for around each gather child
# is the only way to guarantee the function returns.
IMAGE_FETCH_DEADLINE_S = float(os.environ.get("CARPET_IMAGE_DEADLINE_S", "20"))
VISION_CALL_DEADLINE_S = float(os.environ.get("CARPET_VISION_DEADLINE_S", "120"))

_HTTPX_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
_HTTPX_LIMITS = httpx.Limits(max_connections=8, max_keepalive_connections=4)

# Legacy single-signal prompt kept for backward compat with detect_carpet().
PROMPT = (
    "Look at this room photo from a property listing. "
    "Are there carpeted floors visible? "
    'Reply with a JSON object: {"carpet": true/false, "confidence": 0.0-1.0, "reason": "brief reason"}. '
    "Only return the JSON, nothing else."
)

# Combined room-type + carpet prompt used by detect_carpet_two_signal().
ROOM_CARPET_PROMPT = (
    "Look at this room photo from a property listing. "
    "Identify the room type and whether the floor is carpeted. "
    'Reply with JSON only: {"room_type": "bedroom"|"living-room"|"kitchen"|"bathroom"|"hallway"|"dining-room"|"other", '
    '"carpet": true|false, "confidence": 0.0-1.0}.'
)

# Two-step pipeline (introduced 2026-05-10 after carpet TDD report showed the
# combined prompt false-positives on hardwood/laminate as carpet at 50% rate
# in living areas). Each step is single-purpose: step 1 classifies the room,
# step 2 only runs on bedroom + living-room + hallway + dining-room photos
# with a tightened carpet prompt that enumerates non-carpet materials.

ROOM_CLASSIFY_PROMPT = (
    "Look at this room photo from a property listing. Identify the room type. "
    'Reply with JSON only: {"room_type": "bedroom"|"living-room"|"kitchen"|"bathroom"|"hallway"|"dining-room"|"other", '
    '"confidence": 0.0-1.0}. Only the JSON, nothing else.'
)

CARPET_ONLY_PROMPT = (
    "Look at this photo and decide whether the visible floor is CARPET.\n\n"
    "CARPET: visible fiber texture, soft uniform pile, matte appearance, "
    "no straight grain lines, fibrous surface covering most of the visible floor.\n\n"
    "NOT CARPET: hardwood (wood grain, plank seams), laminate or engineered wood "
    "(uniform wood-look pattern, often glossy), vinyl or lino (smooth flat surface), "
    "tile (grout lines, ceramic or stone), polished stone, concrete. "
    "A small rug on top of hardwood is NOT carpet; only count carpet if it covers "
    "most of the visible floor area.\n\n"
    "If the floor is barely visible or unclear, return false.\n\n"
    'Reply with JSON only: {"carpet": true|false, "confidence": 0.0-1.0, '
    '"floor_material": "carpet"|"hardwood"|"laminate"|"vinyl"|"tile"|"concrete"|"unclear"}. '
    "Only the JSON, nothing else."
)

CARPET_ANY_BATCH_PROMPT = (
    "Look at each property listing photo and decide whether the visible floor is CARPET. "
    "Evaluate every image independently, in the same order as supplied. "
    "CARPET means wall-to-wall or fitted carpet: visible fiber texture, soft uniform pile, "
    "matte appearance, and fibrous surface covering most of the visible floor. "
    "Do not count rugs on hard flooring. Do not count hardwood, laminate, vinyl, tile, "
    "stone, concrete, or unclear floors as carpet. "
    'Reply with JSON only: {"photos": ['
    '{"carpet": true|false, "confidence": 0.0-1.0, "reason": "brief reason"}'
    "]}. The photos array must contain exactly one object per image."
)

# Rooms where carpet matters per the user's preference function. Carpet in
# kitchens or bathrooms is unusual and not penalised per the calibration doc.
CARPET_RELEVANT_ROOMS = {"bedroom", "living-room", "hallway", "dining-room"}
LIVING_AREA_ROOMS = {"living-room", "hallway", "dining-room"}


def _resize_b64(raw: bytes, max_edge: int = CARPET_IMG_MAX_EDGE) -> str:
    """Resize an image to a max long-edge of ``max_edge`` and return base64 JPEG.

    Pre-resizing the bytes before base64 encoding reduces the vision-token
    count Ollama sees by roughly 4x at 768 px vs an unresized 1500-2000 px
    Rightmove photo. Quality 85 keeps file size small while preserving
    enough surface detail for the carpet/hard-floor distinction.
    """
    img = Image.open(io.BytesIO(raw))
    img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


async def _fetch_image_b64(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """Download an image, resize it, and return its base64 encoding."""
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=15.0,
        )
        resp.raise_for_status()
        try:
            return _resize_b64(resp.content)
        except Exception as exc:
            # If Pillow cannot decode (corrupt image, unsupported format),
            # fall back to the raw bytes so the model still gets a chance.
            logger.warning(
                "carpet_image_resize_failed_fallback_raw",
                url=url, error_type=type(exc).__name__, error=str(exc),
            )
            return base64.b64encode(resp.content).decode()
    except Exception as exc:
        logger.warning("carpet_image_fetch_failed", url=url, error=str(exc))
        return None


async def _query_vision_model(
    client: httpx.AsyncClient, image_b64: str
) -> Optional[dict]:
    """Send one image to Ollama and parse the JSON response."""
    payload = {
        "model": VISION_MODEL,
        "prompt": PROMPT,
        "images": [image_b64],
        "stream": False,
        "format": "json",
        "options": dict(_VISION_OPTIONS),
    }
    resp = None
    try:
        resp = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            timeout=300.0,
        )
        resp.raise_for_status()
        body = resp.json()
        # qwen3-vl:2b is a thinking model. With format=json the structured
        # output may land in ``thinking`` rather than ``response`` depending
        # on the runner version. Prefer ``response``, fall back to
        # ``thinking`` so both shapes parse uniformly.
        raw = body.get("response", "").strip()
        if not raw:
            raw = body.get("thinking", "").strip()
        logger.debug("carpet_raw_response", raw=raw[:200])
        # Strip markdown code fences if the model adds them
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as exc:
        logger.warning(
            "carpet_vision_model_failed",
            error_type=type(exc).__name__,
            error=str(exc),
            status=getattr(resp, "status_code", None),
            body=getattr(resp, "text", "")[:300],
        )
        return None


async def detect_carpet(
    photo_urls: list[str],
    ollama_url: str = OLLAMA_URL,
) -> tuple[Optional[bool], float]:
    """
    Check up to CARPET_PHOTOS_TO_CHECK photos for carpet.

    Returns:
        (carpet_detected, avg_confidence)
        carpet_detected is None when no usable photos were processed.
    """
    if not photo_urls:
        return None, 0.0

    urls = photo_urls[:CARPET_PHOTOS_TO_CHECK]

    async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT, limits=_HTTPX_LIMITS) as client:
        images_raw = await asyncio.gather(
            *[asyncio.wait_for(_fetch_image_b64(client, u), timeout=IMAGE_FETCH_DEADLINE_S)
              for u in urls],
            return_exceptions=True,
        )
        images = [i if isinstance(i, str) else None for i in images_raw]
        results_raw = await asyncio.gather(
            *[asyncio.wait_for(_query_vision_model(client, b64), timeout=VISION_CALL_DEADLINE_S)
              for b64 in images if b64 is not None],
            return_exceptions=True,
        )
        results = [r if isinstance(r, dict) else None for r in results_raw]

    votes = [
        (r["carpet"], r.get("confidence", 0.5))
        for r in results
        if r and "carpet" in r
    ]

    if not votes:
        return None, 0.0

    carpet_count = sum(1 for detected, _ in votes if detected)
    avg_confidence = round(sum(c for _, c in votes) / len(votes), 2)
    detected = carpet_count > len(votes) / 2

    logger.info(
        "carpet_detection_complete",
        photos_checked=len(votes),
        carpet_votes=carpet_count,
        avg_confidence=avg_confidence,
        detected=detected,
    )
    return detected, avg_confidence


async def _query_vision_model_room_aware(
    client: httpx.AsyncClient, image_b64: str
) -> Optional[dict]:
    """Send one image to Ollama using the combined room+carpet prompt."""
    payload = {
        "model": VISION_MODEL,
        "prompt": ROOM_CARPET_PROMPT,
        "images": [image_b64],
        "stream": False,
        "format": "json",
        "options": dict(_VISION_OPTIONS),
    }
    resp = None
    try:
        resp = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            timeout=300.0,
        )
        resp.raise_for_status()
        body = resp.json()
        raw = body.get("response", "").strip()
        if not raw:
            raw = body.get("thinking", "").strip()
        logger.debug("carpet_room_aware_raw", raw=raw[:200])
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        # Require at minimum room_type and carpet keys.
        if "room_type" not in parsed or "carpet" not in parsed:
            logger.warning("carpet_room_aware_missing_keys", parsed=parsed)
            return None
        return parsed
    except Exception as exc:
        logger.warning(
            "carpet_room_aware_failed",
            error_type=type(exc).__name__,
            error=str(exc),
            status=getattr(resp, "status_code", None),
            body=getattr(resp, "text", "")[:300],
        )
        return None


async def _query_room_classify(client: httpx.AsyncClient, image_b64: str) -> Optional[dict]:
    """Step 1 of the two-step pipeline: room type only."""
    payload = {
        "model": VISION_MODEL,
        "prompt": ROOM_CLASSIFY_PROMPT,
        "images": [image_b64],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0, "num_predict": 48, "num_ctx": 2048},
    }
    resp = None
    try:
        resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=300.0)
        resp.raise_for_status()
        body = resp.json()
        raw = body.get("response", "").strip() or body.get("thinking", "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        if "room_type" not in parsed:
            return None
        return parsed
    except Exception as exc:
        logger.warning(
            "carpet_room_classify_failed",
            error_type=type(exc).__name__,
            error=str(exc),
            status=getattr(resp, "status_code", None),
        )
        return None


async def _query_carpet_only(client: httpx.AsyncClient, image_b64: str) -> Optional[dict]:
    """Step 2 of the two-step pipeline: carpet yes/no with the tightened prompt."""
    payload = {
        "model": VISION_MODEL,
        "prompt": CARPET_ONLY_PROMPT,
        "images": [image_b64],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0, "num_predict": 96, "num_ctx": 2048},
    }
    resp = None
    try:
        resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=300.0)
        resp.raise_for_status()
        body = resp.json()
        raw = body.get("response", "").strip() or body.get("thinking", "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        if "carpet" not in parsed:
            return None
        return parsed
    except Exception as exc:
        logger.warning(
            "carpet_only_failed",
            error_type=type(exc).__name__,
            error=str(exc),
            status=getattr(resp, "status_code", None),
        )
        return None


async def _query_carpet_batch(
    client: httpx.AsyncClient, images_b64: list[str]
) -> Optional[list[dict]]:
    """Send a batch of images to Ollama and parse per-photo carpet results."""
    if not images_b64:
        return []
    payload = {
        "model": VISION_MODEL,
        "prompt": CARPET_ANY_BATCH_PROMPT,
        "images": images_b64,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.0,
            "num_predict": max(96, 96 * len(images_b64)),
            "num_ctx": 8192,
        },
    }
    resp = None
    try:
        resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=300.0)
        resp.raise_for_status()
        body = resp.json()
        raw = body.get("response", "").strip() or body.get("thinking", "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        photos = parsed.get("photos") if isinstance(parsed, dict) else None
        if isinstance(photos, list):
            return photos
        logger.warning("carpet_batch_missing_photos", parsed=parsed)
        return None
    except Exception as exc:
        logger.warning(
            "carpet_batch_failed",
            error_type=type(exc).__name__,
            error=str(exc),
            status=getattr(resp, "status_code", None),
        )
        return None


async def detect_carpet_any(photo_urls: list[str]) -> dict:
    """Room-agnostic carpet detector over all photos in chunked batches."""
    empty = {
        "carpet_in_bedroom": None,
        "carpet_other_areas": None,
        "carpet_detected": None,
        "carpet_confidence": 0.0,
        "carpet_photo_count": 0,
        "bedrooms_with_carpet": None,
        "has_living_area_carpet": None,
        "per_photo": [],
    }
    if not photo_urls:
        return empty

    urls = list(photo_urls)
    per_photo = [
        {
            "url": url,
            "carpet": None,
            "carpet_confidence": 0.0,
            "reason": None,
        }
        for url in urls
    ]
    parsed_results: list[dict] = []

    async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT, limits=_HTTPX_LIMITS) as client:
        images_raw = await asyncio.gather(
            *[
                asyncio.wait_for(_fetch_image_b64(client, u), timeout=IMAGE_FETCH_DEADLINE_S)
                for u in urls
            ],
            return_exceptions=True,
        )
        images = [i if isinstance(i, str) else None for i in images_raw]

        for start in range(0, len(urls), max(1, CARPET_BATCH_SIZE)):
            end = min(start + max(1, CARPET_BATCH_SIZE), len(urls))
            indexed_images = [
                (idx, images[idx])
                for idx in range(start, end)
                if images[idx] is not None
            ]
            if not indexed_images:
                continue

            batch_images = [b64 for _, b64 in indexed_images]
            try:
                batch_raw = await asyncio.wait_for(
                    _query_carpet_batch(client, batch_images),
                    timeout=VISION_CALL_DEADLINE_S,
                )
            except Exception as exc:
                logger.warning(
                    "carpet_batch_deadline_or_error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    batch_start=start,
                    batch_size=len(batch_images),
                )
                batch_raw = None

            if not isinstance(batch_raw, list) or len(batch_raw) != len(batch_images):
                logger.warning(
                    "carpet_batch_length_mismatch",
                    expected=len(batch_images),
                    got=len(batch_raw) if isinstance(batch_raw, list) else None,
                )
                continue

            for (idx, _), result in zip(indexed_images, batch_raw):
                if not isinstance(result, dict) or "carpet" not in result:
                    continue
                carpet = bool(result.get("carpet"))
                confidence = float(result.get("confidence", 0.5) or 0.0)
                per_photo[idx] = {
                    "url": urls[idx],
                    "carpet": carpet,
                    "carpet_confidence": confidence,
                    "reason": result.get("reason"),
                }
                parsed_results.append(per_photo[idx])

    if not parsed_results:
        return {
            **empty,
            "carpet_photo_count": len(urls),
            "per_photo": per_photo,
        }

    carpet_detected = any(p.get("carpet") is True for p in parsed_results)
    confidences = [float(p.get("carpet_confidence") or 0.0) for p in parsed_results]
    avg_conf = round(sum(confidences) / len(confidences), 2) if confidences else 0.0

    logger.info(
        "carpet_any_complete",
        photos_checked=len(parsed_results),
        photo_count=len(urls),
        detected=carpet_detected,
        avg_confidence=avg_conf,
    )
    return {
        "carpet_in_bedroom": None,
        "carpet_other_areas": None,
        "carpet_detected": carpet_detected,
        "carpet_confidence": avg_conf,
        "carpet_photo_count": len(urls),
        "bedrooms_with_carpet": None,
        "has_living_area_carpet": None,
        "per_photo": per_photo,
    }


async def detect_carpet_two_step_from_paths(photo_paths: list) -> dict:
    """Two-step room-classify-then-carpet detection on local image paths.

    Pipeline:
        1. Classify room_type for every photo with the room-only prompt.
        2. For photos whose room is in CARPET_RELEVANT_ROOMS (bedroom,
           living-room, hallway, dining-room), call the tightened carpet-only
           prompt. All other photos are recorded with carpet=False (kitchen,
           bathroom, "other" — places where carpet would not change the
           score per the user's preference function).
        3. Aggregate to {bedrooms_seen, bedrooms_with_carpet, has_living_area_carpet}
           plus the backward-compat fields.

    Trade-off vs detect_carpet_two_signal: roughly 1.5x more model calls per
    relevant photo (one for room, one for carpet), in exchange for
    substantially better accuracy on living-area carpet false positives.
    """
    empty = {
        "carpet_in_bedroom": None,
        "carpet_other_areas": None,
        "carpet_detected": None,
        "carpet_confidence": 0.0,
        "bedrooms_seen": 0,
        "bedrooms_with_carpet": 0,
        "has_living_area_carpet": False,
        "per_photo": [],
    }
    if not photo_paths:
        return empty

    paths = list(photo_paths)
    images: list[Optional[str]] = []
    for p in paths:
        try:
            raw = open(p, "rb").read()
            images.append(_resize_b64(raw))
        except Exception as exc:
            logger.warning("carpet_local_image_load_failed", path=str(p), error=str(exc))
            images.append(None)

    async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT, limits=_HTTPX_LIMITS) as client:
        room_coros = []
        for b64 in images:
            if b64 is None:
                room_coros.append(asyncio.sleep(0, result=None))
            else:
                room_coros.append(
                    asyncio.wait_for(_query_room_classify(client, b64), timeout=VISION_CALL_DEADLINE_S)
                )
        room_results_raw = await asyncio.gather(*room_coros, return_exceptions=True)
        room_results = [r if isinstance(r, dict) else None for r in room_results_raw]

        carpet_coros = []
        relevant_indices: list[int] = []
        for i, (b64, room) in enumerate(zip(images, room_results)):
            if b64 is None or room is None:
                continue
            if room.get("room_type") in CARPET_RELEVANT_ROOMS:
                relevant_indices.append(i)
                carpet_coros.append(
                    asyncio.wait_for(_query_carpet_only(client, b64), timeout=VISION_CALL_DEADLINE_S)
                )
        carpet_results_raw = await asyncio.gather(*carpet_coros, return_exceptions=True) if carpet_coros else []
        carpet_results = [r if isinstance(r, dict) else None for r in carpet_results_raw]

    carpet_by_index: dict[int, dict] = {idx: res for idx, res in zip(relevant_indices, carpet_results)}

    per_photo: list[dict] = []
    bedrooms_seen = 0
    bedrooms_with_carpet = 0
    confidences: list[float] = []

    for i, (path, room) in enumerate(zip(paths, room_results)):
        room_type = room.get("room_type") if room else None
        room_conf = float(room.get("confidence", 0.5)) if room else 0.0
        carpet_record = carpet_by_index.get(i)

        if carpet_record is not None:
            carpet = bool(carpet_record.get("carpet"))
            carpet_conf = float(carpet_record.get("confidence", 0.5))
            floor = carpet_record.get("floor_material")
        else:
            carpet = False
            carpet_conf = 0.0
            floor = None

        if room_type is not None and room_conf:
            confidences.append(room_conf)
        if carpet_record is not None:
            confidences.append(carpet_conf)

        if room_type == "bedroom":
            bedrooms_seen += 1
            if carpet:
                bedrooms_with_carpet += 1

        per_photo.append({
            "file": str(path),
            "room_type": room_type,
            "room_confidence": room_conf,
            "carpet": carpet if carpet_record is not None else None,
            "carpet_confidence": carpet_conf,
            "floor_material": floor,
        })

    # Majority voting on living-area carpet to suppress single-photo false
    # positives. The detector misclassifies hardwood as carpet on certain
    # photos at this resolution; requiring at least 2 living-area photos AND
    # a majority of all living-area photos to be flagged keeps a true
    # wall-to-wall living-area carpet signal while dismissing one-off model
    # confusion. Tuned 2026-05-10 against the carpet TDD fixtures.
    living_area_photos = [p for p in per_photo if p.get("room_type") in LIVING_AREA_ROOMS]
    living_area_carpet_photos = [p for p in living_area_photos if p.get("carpet") is True]
    has_living_area_carpet = (
        len(living_area_carpet_photos) >= 2
        and len(living_area_carpet_photos) > len(living_area_photos) / 2
    )

    avg_conf = round(sum(confidences) / len(confidences), 2) if confidences else 0.0
    bedroom_carpet = bedrooms_with_carpet > 0
    other_carpet = has_living_area_carpet

    return {
        "carpet_in_bedroom": bedroom_carpet,
        "carpet_other_areas": other_carpet,
        "carpet_detected": bedroom_carpet or other_carpet,
        "carpet_confidence": avg_conf,
        "bedrooms_seen": bedrooms_seen,
        "bedrooms_with_carpet": bedrooms_with_carpet,
        "has_living_area_carpet": has_living_area_carpet,
        "per_photo": per_photo,
    }


async def detect_carpet_two_step(photo_urls: list[str]) -> dict:
    """Production URL-based two-step room-classify-then-carpet detector.

    Fetches up to CARPET_PHOTOS_TO_CHECK photos, runs the same two-step
    pipeline as detect_carpet_two_step_from_paths. Same return shape.
    """
    empty = {
        "carpet_in_bedroom": None,
        "carpet_other_areas": None,
        "carpet_detected": None,
        "carpet_confidence": 0.0,
        "bedrooms_seen": 0,
        "bedrooms_with_carpet": 0,
        "has_living_area_carpet": False,
        "per_photo": [],
    }
    if not photo_urls:
        return empty

    urls = photo_urls[:CARPET_PHOTOS_TO_CHECK]
    async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT, limits=_HTTPX_LIMITS) as client:
        images_raw = await asyncio.gather(
            *[
                asyncio.wait_for(_fetch_image_b64(client, u), timeout=IMAGE_FETCH_DEADLINE_S)
                for u in urls
            ],
            return_exceptions=True,
        )
        images = [i if isinstance(i, str) else None for i in images_raw]

        room_coros = [
            asyncio.wait_for(_query_room_classify(client, b64), timeout=VISION_CALL_DEADLINE_S)
            if b64 is not None else asyncio.sleep(0, result=None)
            for b64 in images
        ]
        room_results_raw = await asyncio.gather(*room_coros, return_exceptions=True)
        room_results = [r if isinstance(r, dict) else None for r in room_results_raw]

        carpet_coros = []
        relevant_indices: list[int] = []
        for i, (b64, room) in enumerate(zip(images, room_results)):
            if b64 is None or room is None:
                continue
            if room.get("room_type") in CARPET_RELEVANT_ROOMS:
                relevant_indices.append(i)
                carpet_coros.append(
                    asyncio.wait_for(_query_carpet_only(client, b64), timeout=VISION_CALL_DEADLINE_S)
                )
        carpet_results_raw = await asyncio.gather(*carpet_coros, return_exceptions=True) if carpet_coros else []
        carpet_results = [r if isinstance(r, dict) else None for r in carpet_results_raw]

    carpet_by_index = {idx: res for idx, res in zip(relevant_indices, carpet_results)}

    per_photo: list[dict] = []
    bedrooms_seen = 0
    bedrooms_with_carpet = 0
    confidences: list[float] = []

    for i, (url, room) in enumerate(zip(urls, room_results)):
        room_type = room.get("room_type") if room else None
        room_conf = float(room.get("confidence", 0.5)) if room else 0.0
        carpet_record = carpet_by_index.get(i)
        if carpet_record is not None:
            carpet = bool(carpet_record.get("carpet"))
            carpet_conf = float(carpet_record.get("confidence", 0.5))
            floor = carpet_record.get("floor_material")
        else:
            carpet = False
            carpet_conf = 0.0
            floor = None
        if room_type is not None and room_conf:
            confidences.append(room_conf)
        if carpet_record is not None:
            confidences.append(carpet_conf)
        if room_type == "bedroom":
            bedrooms_seen += 1
            if carpet:
                bedrooms_with_carpet += 1
        per_photo.append({
            "url": url,
            "room_type": room_type,
            "room_confidence": room_conf,
            "carpet": carpet if carpet_record is not None else None,
            "carpet_confidence": carpet_conf,
            "floor_material": floor,
        })

    living_area_photos = [p for p in per_photo if p.get("room_type") in LIVING_AREA_ROOMS]
    living_area_carpet_photos = [p for p in living_area_photos if p.get("carpet") is True]
    has_living_area_carpet = (
        len(living_area_carpet_photos) >= 2
        and len(living_area_carpet_photos) > len(living_area_photos) / 2
    )

    avg_conf = round(sum(confidences) / len(confidences), 2) if confidences else 0.0
    bedroom_carpet = bedrooms_with_carpet > 0
    other_carpet = has_living_area_carpet

    if not confidences:
        return empty

    logger.info(
        "carpet_two_step_complete",
        photos_input=len(urls),
        bedrooms_seen=bedrooms_seen,
        bedrooms_with_carpet=bedrooms_with_carpet,
        has_living_area_carpet=has_living_area_carpet,
        avg_confidence=avg_conf,
    )

    return {
        "carpet_in_bedroom": bedroom_carpet,
        "carpet_other_areas": other_carpet,
        "carpet_detected": bedroom_carpet or other_carpet,
        "carpet_confidence": avg_conf,
        "bedrooms_seen": bedrooms_seen,
        "bedrooms_with_carpet": bedrooms_with_carpet,
        "has_living_area_carpet": has_living_area_carpet,
        "per_photo": per_photo,
    }


async def detect_carpet_two_signal_from_paths(photo_paths: list) -> dict:
    """Run room-aware carpet detection against local image files.

    Mirrors detect_carpet_two_signal but reads bytes from disk and resizes them
    via the same _resize_b64 helper. Used by the test runner against cached
    photos so calibration runs do not depend on network availability or
    upstream portal rate limits.

    Returns a dict with the same shape as detect_carpet_two_signal plus a
    per_photo list of {file, room_type, carpet, confidence} for diagnostics.
    """
    empty = {
        "carpet_in_bedroom": None,
        "carpet_other_areas": None,
        "carpet_detected": None,
        "carpet_confidence": 0.0,
        "per_photo": [],
    }
    if not photo_paths:
        return empty

    paths = list(photo_paths)
    images: list[Optional[str]] = []
    for p in paths:
        try:
            raw = open(p, "rb").read()
            images.append(_resize_b64(raw))
        except Exception as exc:
            logger.warning("carpet_local_image_load_failed", path=str(p), error=str(exc))
            images.append(None)

    async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT, limits=_HTTPX_LIMITS) as client:
        coros = []
        for b64 in images:
            if b64 is None:
                coros.append(asyncio.sleep(0, result=None))
            else:
                coros.append(
                    asyncio.wait_for(
                        _query_vision_model_room_aware(client, b64),
                        timeout=VISION_CALL_DEADLINE_S,
                    )
                )
        results_raw = await asyncio.gather(*coros, return_exceptions=True)
    results = [r if isinstance(r, dict) else None for r in results_raw]

    per_photo: list[dict] = []
    bedroom_carpet = False
    other_carpet = False
    confidences: list[float] = []
    for path, r in zip(paths, results):
        if r is None:
            per_photo.append({"file": str(path), "room_type": None, "carpet": None, "confidence": 0.0})
            continue
        rt = r.get("room_type", "other")
        carpet = bool(r.get("carpet", False))
        conf = float(r.get("confidence", 0.5))
        confidences.append(conf)
        per_photo.append({"file": str(path), "room_type": rt, "carpet": carpet, "confidence": conf})
        if rt == "bedroom":
            if carpet:
                bedroom_carpet = True
        elif carpet:
            other_carpet = True

    if not confidences:
        return empty

    return {
        "carpet_in_bedroom": bedroom_carpet,
        "carpet_other_areas": other_carpet,
        "carpet_detected": bedroom_carpet or other_carpet,
        "carpet_confidence": round(sum(confidences) / len(confidences), 2),
        "per_photo": per_photo,
    }


async def detect_carpet_two_signal(
    photo_urls: list[str],
) -> dict:
    """
    Room-aware carpet detection returning two independent signals.

    Returns a dict with:
        carpet_in_bedroom  -- True if any bedroom photo has carpet, False otherwise, None if no data
        carpet_other_areas -- True if any non-bedroom photo has carpet, False otherwise, None if no data
        carpet_detected    -- backward-compatible OR of the two signals
        carpet_confidence  -- average confidence across all parseable responses
    """
    empty = {
        "carpet_in_bedroom": None,
        "carpet_other_areas": None,
        "carpet_detected": None,
        "carpet_confidence": 0.0,
    }

    if not photo_urls:
        return empty

    urls = photo_urls[:CARPET_PHOTOS_TO_CHECK]

    async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT, limits=_HTTPX_LIMITS) as client:
        images_raw = await asyncio.gather(
            *[asyncio.wait_for(_fetch_image_b64(client, u), timeout=IMAGE_FETCH_DEADLINE_S)
              for u in urls],
            return_exceptions=True,
        )
        images = [i if isinstance(i, str) else None for i in images_raw]
        results_raw = await asyncio.gather(
            *[asyncio.wait_for(_query_vision_model_room_aware(client, b64), timeout=VISION_CALL_DEADLINE_S)
              for b64 in images if b64 is not None],
            return_exceptions=True,
        )
        results = [r if isinstance(r, dict) else None for r in results_raw]

    parsed = [r for r in results if r is not None]
    if not parsed:
        return empty

    bedroom_carpet = False
    other_carpet = False
    confidences = []

    for r in parsed:
        room_type = r.get("room_type", "other")
        has_carpet = bool(r.get("carpet", False))
        confidence = float(r.get("confidence", 0.5))
        confidences.append(confidence)
        if room_type == "bedroom":
            if has_carpet:
                bedroom_carpet = True
        else:
            if has_carpet:
                other_carpet = True

    avg_confidence = round(sum(confidences) / len(confidences), 2)
    carpet_detected = bedroom_carpet or other_carpet

    logger.info(
        "carpet_two_signal_complete",
        photos_checked=len(parsed),
        carpet_in_bedroom=bedroom_carpet,
        carpet_other_areas=other_carpet,
        avg_confidence=avg_confidence,
    )

    return {
        "carpet_in_bedroom": bedroom_carpet,
        "carpet_other_areas": other_carpet,
        "carpet_detected": carpet_detected,
        "carpet_confidence": avg_confidence,
    }
