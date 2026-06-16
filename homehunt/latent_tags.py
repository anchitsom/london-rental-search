"""Latent visual tag extraction for rental listing photos.

Two-stage pipeline:
1. qwen3-vl captions up to three listing photos.
2. qwen2.5 extracts one conservative enum value per latent axis.

This module intentionally has no fallback tag synthesis. Ollama availability,
required model installation, short captions, HTTP failures, and malformed JSON
all raise so callers can retry or audit the failure honestly.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
from PIL import Image


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
LATENT_IMG_MAX_EDGE = int(os.environ.get("LATENT_IMG_MAX_EDGE", "768"))

LATENT_TAG_AXES: dict[str, list[str]] = {
    "window_style": ["floor_to_ceiling", "sash", "casement", "picture", "skylight", "mixed", "unclear"],
    "ceiling_height": ["standard", "tall", "vaulted", "low", "unclear"],
    "exposed_brick": ["yes", "no", "unclear"],
    "exposed_beams_or_ducts": ["yes", "no", "unclear"],
    "building_era": ["period", "warehouse_conversion", "mid_century", "new_build", "ex_local", "unclear"],
    "kitchen_finish": ["basic", "mid", "premium", "not_shown"],
    "bathroom_finish": ["basic", "mid", "premium", "not_shown"],
    "flooring": ["wood", "laminate", "tile", "carpet", "mixed", "unclear"],
    "natural_light": ["low", "medium", "high", "unclear"],
    "wall_palette": ["white", "neutral", "dark", "colour_heavy", "unclear"],
    "open_plan": ["yes", "no", "unclear"],
    "view": ["street", "garden", "skyline", "courtyard", "blocked", "unclear"],
    "outdoor_access": ["garden", "balcony", "roof_terrace", "shared_garden", "none", "unclear"],
}

VISION_PROMPT = (
    "Characterise this rental listing photo in one short paragraph. Cover window "
    "style, ceiling height impression, whether exposed brick or beams are visible, "
    "building era feel, finish quality, natural light, and any other distinctive "
    "feature you can see. Be concrete and do not use bullet points."
)

PROMPT_TEMPLATE = """You are a strict structured-tag extractor. Below are free-form paragraphs describing photos of one rental listing. Read them together and emit ONE JSON object that captures what is true of this listing.

OUTPUT RULES:
- Output ONLY valid JSON. No prose, no markdown, no code fences.
- Use exactly these keys: {keys}
- Each value must be one of the allowed enum values for that key (see schema below).
- If the photos genuinely do not show evidence either way, use "unclear" (or "not_shown" for kitchen_finish/bathroom_finish).
- Do not invent features the photos do not describe.

SCHEMA (key -> allowed values):
{schema}

PHOTOS:
{captions}

JSON:"""

_VISION_OPTIONS: dict[str, Any] = {
    "temperature": 0.0,
    "num_predict": 600,
    "num_ctx": 4096,
}

_TEXT_OPTIONS: dict[str, Any] = {
    "temperature": 0.0,
    "num_predict": 512,
    "num_ctx": 4096,
}

_HTTPX_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=20.0, pool=5.0)
_HTTPX_LIMITS = httpx.Limits(max_connections=8, max_keepalive_connections=4)


def _build_prompt(captions: list[str]) -> str:
    schema_lines = "\n".join(f"- {key}: {values}" for key, values in LATENT_TAG_AXES.items())
    caption_block = "\n\n".join(f"PHOTO {index + 1}:\n{caption}" for index, caption in enumerate(captions))
    return PROMPT_TEMPLATE.format(
        keys=", ".join(LATENT_TAG_AXES.keys()),
        schema=schema_lines,
        captions=caption_block,
    )


def _first_json_object(text: str) -> dict[str, Any] | None:
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    parsed = json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    start = -1
                    continue
                return parsed if isinstance(parsed, dict) else None
    return None


def _coerce_tag(value: object, allowed: list[str]) -> str:
    if isinstance(value, str) and value in allowed:
        return value
    if "unclear" in allowed:
        return "unclear"
    return "not_shown"


def _assert_ollama_ready(client: httpx.Client, required_models: set[str]) -> None:
    try:
        response = client.get(f"{OLLAMA_URL}/api/tags", timeout=10.0)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        raise RuntimeError(f"Ollama is not reachable at {OLLAMA_URL}") from exc

    installed = {entry.get("name") for entry in body.get("models", []) if isinstance(entry, dict)}
    missing = sorted(model for model in required_models if model not in installed)
    if missing:
        raise RuntimeError(f"Ollama model(s) not installed: {missing}; installed={sorted(installed)!r}")


def _generate(
    client: httpx.Client,
    *,
    model: str,
    prompt: str,
    options: dict[str, Any],
    images: list[str] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": dict(options),
    }
    if images:
        payload["images"] = images

    try:
        response = client.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=240.0)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        raise RuntimeError(f"Ollama generate failed for model {model!r}") from exc

    text = str(body.get("response") or body.get("thinking") or "").strip()
    if not text:
        raise RuntimeError(f"Ollama returned an empty response for model {model!r}")
    return text


def _resize_b64(raw: bytes, max_edge: int = LATENT_IMG_MAX_EDGE) -> str:
    img = Image.open(io.BytesIO(raw))
    img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _extension_from_url(url: str) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def _cache_path(cache_dir: Path, uid: str, index: int, url: str) -> Path:
    safe_uid = "".join(char if char.isalnum() or char in "-_" else "_" for char in uid)
    return cache_dir / safe_uid / f"{index + 1}{_extension_from_url(url)}"


def _fetch_image(client: httpx.Client, url: str, path: Path | None = None) -> bytes:
    if path is not None and path.exists() and path.stat().st_size > 0:
        return path.read_bytes()

    response = client.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        follow_redirects=True,
        timeout=45.0,
    )
    response.raise_for_status()
    raw = response.content
    if not raw:
        raise RuntimeError(f"downloaded empty image from {url}")
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return raw


def extract_tags_from_captions(captions: list[str], *, text_model: str = "qwen2.5:7b") -> dict[str, str]:
    """Extract the fixed 13-axis latent tag dict from prose image captions."""
    if not captions:
        raise ValueError("captions must contain at least one caption")
    prompt = _build_prompt(captions)
    with httpx.Client(timeout=_HTTPX_TIMEOUT, limits=_HTTPX_LIMITS) as client:
        _assert_ollama_ready(client, {text_model})
        response_text = _generate(
            client,
            model=text_model,
            prompt=prompt,
            options=_TEXT_OPTIONS,
        )

    parsed = _first_json_object(response_text)
    if parsed is None:
        raise RuntimeError("Ollama extraction response did not contain a valid JSON object")

    return {
        key: _coerce_tag(parsed.get(key), allowed)
        for key, allowed in LATENT_TAG_AXES.items()
    }


def tag_listing(
    uid: str,
    image_urls: list[str],
    *,
    vision_model: str = "qwen3-vl:2b-instruct-q8_0",
    text_model: str = "qwen2.5:7b",
    max_images: int = 3,
    image_cache_dir: Path | None = None,
) -> dict:
    """Caption listing images and return 13 latent axes plus raw caption audit rows."""
    urls = [url for url in image_urls[:max_images] if isinstance(url, str) and url.startswith(("http://", "https://"))]
    if not urls:
        raise ValueError(f"listing {uid!r} has no usable image URLs")

    raw_captions: list[dict[str, Any]] = []
    with httpx.Client(timeout=_HTTPX_TIMEOUT, limits=_HTTPX_LIMITS) as client:
        _assert_ollama_ready(client, {vision_model, text_model})
        for index, url in enumerate(urls):
            cache_path = _cache_path(image_cache_dir, uid, index, url) if image_cache_dir is not None else None
            try:
                image_b64 = _resize_b64(_fetch_image(client, url, cache_path))
            except Exception as exc:
                raise RuntimeError(f"failed to fetch or resize image for listing {uid!r}: {url}") from exc

            started = time.perf_counter()
            caption = _generate(
                client,
                model=vision_model,
                prompt=VISION_PROMPT,
                images=[image_b64],
                options=_VISION_OPTIONS,
            )
            latency = time.perf_counter() - started
            if len(caption) < 80:
                raise RuntimeError(f"short vision response ({len(caption)} chars) for listing {uid!r}: {caption!r}")
            raw_captions.append(
                {
                    "image_url": url,
                    "response_text": caption,
                    "latency_seconds": round(latency, 3),
                }
            )

        extraction = _generate(
            client,
            model=text_model,
            prompt=_build_prompt([row["response_text"] for row in raw_captions]),
            options=_TEXT_OPTIONS,
        )

    parsed = _first_json_object(extraction)
    if parsed is None:
        raise RuntimeError(f"Ollama extraction response for listing {uid!r} did not contain valid JSON")

    tags = {
        key: _coerce_tag(parsed.get(key), allowed)
        for key, allowed in LATENT_TAG_AXES.items()
    }
    tags["raw_captions"] = raw_captions
    return tags
