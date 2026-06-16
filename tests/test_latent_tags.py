"""
Stage test: latent tag extraction over captured qwen3-vl captions.

Fixture-only: this patches the local Ollama seam and never calls the network.

Run:
  .venv/bin/python tests/test_latent_tags.py
"""

import asyncio
import json
import os
import sys
import importlib.util
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODULE_PATH = Path(__file__).resolve().parents[1] / "homehunt" / "latent_tags.py"
spec = importlib.util.spec_from_file_location("latent_tags_under_test", MODULE_PATH)
latent_tags = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(latent_tags)

LATENT_TAG_AXES = latent_tags.LATENT_TAG_AXES
extract_tags_from_captions = latent_tags.extract_tags_from_captions


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "latent_tags_captions.jsonl"
PASS = "PASS"
FAIL = "FAIL"


def report(label: str, ok: bool, detail: str = "") -> bool:
    suffix = f" ({detail})" if detail else ""
    print(f"{PASS if ok else FAIL}: {label}{suffix}")
    return ok


def load_rows() -> list[dict]:
    return [
        json.loads(line)
        for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def fake_generate(client, *, model, prompt, options, images=None):
    text = prompt.lower()
    result = {key: ("not_shown" if "not_shown" in allowed else "unclear") for key, allowed in LATENT_TAG_AXES.items()}
    result.update(
        {
            "ceiling_height": "standard",
            "exposed_brick": "no",
            "exposed_beams_or_ducts": "no",
            "flooring": "unclear",
            "natural_light": "medium",
            "wall_palette": "neutral",
            "open_plan": "unclear",
            "view": "unclear",
            "outdoor_access": "unclear",
        }
    )
    if "floor-to-ceiling glass window" in text or "floor-to-ceiling" in text:
        result["window_style"] = "floor_to_ceiling"
    elif "sash window" in text or "sash windows" in text:
        result["window_style"] = "sash"
    elif "multi-paned" in text or "double-hung" in text:
        result["window_style"] = "casement"
    if "exposed brick wall" in text or "exposed brickwork" in text or "exposed brick is visible" in text:
        result["exposed_brick"] = "yes"
    if "light-colored hardwood" in text or "wooden floor" in text or "hardwood" in text:
        result["flooring"] = "wood"
    if "open-plan" in text or "open plan" in text:
        result["open_plan"] = "yes"
    if "balcony" in text:
        result["outdoor_access"] = "balcony"
    return json.dumps(result)


async def main() -> None:
    rows = load_rows()
    outputs = []
    call_count = 0

    def counted_generate(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return fake_generate(*args, **kwargs)

    with mock.patch.object(latent_tags, "_assert_ollama_ready", lambda client, required_models: None), mock.patch.object(
        latent_tags, "_generate", counted_generate
    ):
        for row in rows:
            captions = [item["response_text"] for item in row["per_image"]]
            outputs.append((row, extract_tags_from_captions(captions)))

    expected_keys = set(LATENT_TAG_AXES)
    results = []
    results.append(
        report(
            "Every fixture row returns exactly the 13 axis keys",
            all(set(tags) == expected_keys for _, tags in outputs),
            f"rows={len(outputs)}",
        )
    )

    target = next((tags for row, tags in outputs if row["uid"] == "rightmove:88490412"), None)
    results.append(
        report(
            "rightmove:88490412 floor-to-ceiling window is tagged",
            target is not None and target.get("window_style") == "floor_to_ceiling",
            f"value={None if target is None else target.get('window_style')!r}",
        )
    )

    results.append(
        report(
            "At least one fixture row has exposed brick",
            any(tags["exposed_brick"] == "yes" for _, tags in outputs),
        )
    )

    invalid = [
        (row["uid"], key, value)
        for row, tags in outputs
        for key, value in tags.items()
        if value not in LATENT_TAG_AXES[key]
    ]
    results.append(report("No returned value is outside its axis enum", not invalid, f"invalid={invalid[:3]!r}"))

    results.append(
        report(
            "Total Ollama text calls equal fixture rows times 1",
            call_count == len(rows),
            f"calls={call_count} rows={len(rows)}",
        )
    )

    if not all(results):
        sys.exit(1)


asyncio.run(main())
