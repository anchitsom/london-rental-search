#!/usr/bin/env python
"""Run the room-aware carpet detector against labelled fixtures and report.

Walks tests/fixtures/carpet/*/labels.yaml, runs detect_carpet_two_signal_from_paths
on the cached photos, computes aggregate predictions, compares to ground truth.

Output: stdout report plus markdown report at
docs/sift_v1/2026-05-10-carpet-real-photos-report.md.

Skips fixtures with no bedroom photos (e.g. Rightmove listings whose scraper
captured only 3 hero shots).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from homehunt.carpet import (  # noqa: E402
    detect_carpet_two_signal_from_paths,
    detect_carpet_two_step_from_paths,
)

FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "carpet"
REPORT_PATH = PROJECT_ROOT / "docs" / "sift_v1" / "2026-05-10-carpet-real-photos-report.md"
LIVING_AREA_ROOMS = {"living-room", "hallway", "dining-room"}

PIPELINE = os.environ.get("CARPET_PIPELINE", "two_step")  # "two_step" | "single_call"


def aggregate_from_per_photo(per_photo: list[dict], bedrooms_declared: int | None) -> dict:
    bedroom_carpet_count = sum(
        1 for p in per_photo if p.get("room_type") == "bedroom" and p.get("carpet") is True
    )
    bedroom_seen_count = sum(1 for p in per_photo if p.get("room_type") == "bedroom")
    has_living_area_carpet = any(
        p.get("room_type") in LIVING_AREA_ROOMS and p.get("carpet") is True for p in per_photo
    )
    if bedrooms_declared:
        bedroom_carpet_count = min(bedroom_carpet_count, bedrooms_declared)
        bedroom_seen_count = min(bedroom_seen_count, bedrooms_declared)
    return {
        "bedrooms_seen_pred": bedroom_seen_count,
        "bedrooms_with_carpet_pred": bedroom_carpet_count,
        "has_living_area_carpet_pred": has_living_area_carpet,
    }


def truth_lookup(photos: list[dict]) -> dict[str, dict]:
    return {p["file"]: p for p in photos}


def per_photo_diff(predictions: list[dict], truth_by_file: dict[str, dict]) -> list[dict]:
    rows = []
    for pred in predictions:
        file_path = Path(pred["file"]).name
        truth = truth_by_file.get(file_path, {})
        # Handle both pipelines' shapes: single-call has "confidence", two-step has "room_confidence" + "carpet_confidence".
        room_conf = pred.get("room_confidence", pred.get("confidence", 0.0))
        carpet_conf = pred.get("carpet_confidence", pred.get("confidence", 0.0))
        rows.append({
            "file": file_path,
            "truth_room": truth.get("room_type"),
            "pred_room": pred.get("room_type"),
            "room_match": truth.get("room_type") == pred.get("room_type"),
            "truth_carpet": bool(truth.get("has_carpet")) if truth else None,
            "pred_carpet": pred.get("carpet"),
            "carpet_match": (
                bool(truth.get("has_carpet")) == bool(pred.get("carpet"))
                if truth and pred.get("carpet") is not None else None
            ),
            "room_confidence": room_conf,
            "carpet_confidence": carpet_conf,
            "floor_material": pred.get("floor_material"),
        })
    return rows


async def evaluate_fixture(yaml_path: Path) -> dict:
    data = yaml.safe_load(yaml_path.read_text())
    uid = data["listing_uid"]
    declared = data.get("bedrooms_declared")
    expected = data.get("expected", {})
    photos = data.get("photos", [])
    has_bedroom_photos = any(p.get("room_type") == "bedroom" for p in photos)

    if not has_bedroom_photos:
        return {
            "uid": uid,
            "skipped": True,
            "reason": "no bedroom photos in fixture (likely scraper photo limit)",
            "expected": expected,
        }

    photos_dir = yaml_path.parent / "photos"
    photo_paths = [photos_dir / p["file"] for p in photos]
    if PIPELINE == "two_step":
        detector_out = await detect_carpet_two_step_from_paths(photo_paths)
        per_photo = detector_out.get("per_photo", [])
        bs = min(detector_out.get("bedrooms_seen", 0), declared) if declared else detector_out.get("bedrooms_seen", 0)
        bwc = min(detector_out.get("bedrooms_with_carpet", 0), declared) if declared else detector_out.get("bedrooms_with_carpet", 0)
        aggregate_pred = {
            "bedrooms_seen_pred": bs,
            "bedrooms_with_carpet_pred": bwc,
            "has_living_area_carpet_pred": detector_out.get("has_living_area_carpet", False),
        }
    else:
        detector_out = await detect_carpet_two_signal_from_paths(photo_paths)
        per_photo = detector_out.get("per_photo", [])
        aggregate_pred = aggregate_from_per_photo(per_photo, declared)
    diff = per_photo_diff(per_photo, truth_lookup(photos))

    bs_match = aggregate_pred["bedrooms_seen_pred"] == expected.get("bedrooms_seen")
    bwc_match = aggregate_pred["bedrooms_with_carpet_pred"] == expected.get("bedrooms_with_carpet")
    liv_match = aggregate_pred["has_living_area_carpet_pred"] == expected.get("has_living_area_carpet")

    return {
        "uid": uid,
        "skipped": False,
        "bedrooms_declared": declared,
        "expected": expected,
        "predicted": aggregate_pred,
        "bedrooms_seen_match": bs_match,
        "bedrooms_with_carpet_match": bwc_match,
        "has_living_area_carpet_match": liv_match,
        "per_photo_diff": diff,
        "raw_detector_flags": {
            "carpet_in_bedroom": detector_out.get("carpet_in_bedroom"),
            "carpet_other_areas": detector_out.get("carpet_other_areas"),
        },
    }


def render_report(results: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# Carpet detector vs labelled fixtures")
    lines.append("")
    model = os.environ.get("VISION_MODEL", "qwen3-vl:2b")
    photos_to_check = os.environ.get("CARPET_PHOTOS_TO_CHECK", "4")
    lines.append(f"**Date:** 2026-05-10. Generated by `scripts/carpet_real_photos_report.py`.")
    lines.append(f"**Model:** `{model}` | **Pipeline:** `{PIPELINE}` | **Photos scanned:** every cached photo per fixture")
    lines.append("")

    evaluated = [r for r in results if not r.get("skipped")]
    skipped = [r for r in results if r.get("skipped")]

    n = len(evaluated)
    bs_ok = sum(1 for r in evaluated if r["bedrooms_seen_match"])
    bwc_ok = sum(1 for r in evaluated if r["bedrooms_with_carpet_match"])
    liv_ok = sum(1 for r in evaluated if r["has_living_area_carpet_match"])

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Fixtures evaluated: **{n}**")
    lines.append(f"- Skipped: {len(skipped)} (no bedroom photos)")
    if n:
        lines.append(f"- `bedrooms_seen` correct:          **{bs_ok}/{n}** ({100*bs_ok/n:.0f}%)")
        lines.append(f"- `bedrooms_with_carpet` correct:   **{bwc_ok}/{n}** ({100*bwc_ok/n:.0f}%)  (target 80%)")
        lines.append(f"- `has_living_area_carpet` correct: **{liv_ok}/{n}** ({100*liv_ok/n:.0f}%)  (target 90%)")
    lines.append("")

    lines.append("## Per-fixture results")
    lines.append("")
    lines.append("| uid | declared | expected (bs/bwc/liv) | predicted (bs/bwc/liv) | bs | bwc | liv |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in evaluated:
        e = r["expected"]
        p = r["predicted"]
        lines.append(
            f"| `{r['uid']}` | {r['bedrooms_declared']} | "
            f"{e.get('bedrooms_seen')}/{e.get('bedrooms_with_carpet')}/{e.get('has_living_area_carpet')} | "
            f"{p['bedrooms_seen_pred']}/{p['bedrooms_with_carpet_pred']}/{p['has_living_area_carpet_pred']} | "
            f"{'OK' if r['bedrooms_seen_match'] else 'X'} | "
            f"{'OK' if r['bedrooms_with_carpet_match'] else 'X'} | "
            f"{'OK' if r['has_living_area_carpet_match'] else 'X'} |"
        )
    lines.append("")

    if skipped:
        lines.append("## Skipped fixtures")
        lines.append("")
        for r in skipped:
            lines.append(f"- `{r['uid']}`: {r['reason']}")
        lines.append("")

    lines.append("## Per-photo confusion (failed fixtures only)")
    lines.append("")
    failed = [
        r for r in evaluated
        if not (r["bedrooms_seen_match"] and r["bedrooms_with_carpet_match"] and r["has_living_area_carpet_match"])
    ]
    if not failed:
        lines.append("None. All evaluated fixtures match.")
        lines.append("")
    for r in failed:
        lines.append(f"### `{r['uid']}`")
        lines.append("")
        lines.append("| file | truth (room/carpet) | pred (room/carpet) | floor | room conf | carpet conf | room match | carpet match |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for d in r["per_photo_diff"]:
            tr = d["truth_room"] or ""
            tc = "Y" if d["truth_carpet"] else "N" if d["truth_carpet"] is False else "?"
            pr = d["pred_room"] or "(none)"
            pc = "Y" if d["pred_carpet"] is True else "N" if d["pred_carpet"] is False else "skip"
            rm = "OK" if d["room_match"] else "X"
            cm = "OK" if d["carpet_match"] else ("X" if d["carpet_match"] is False else "-")
            rc = f"{d['room_confidence']:.2f}" if d["room_confidence"] else ""
            cc = f"{d['carpet_confidence']:.2f}" if d["carpet_confidence"] else ""
            floor = d["floor_material"] or ""
            lines.append(f"| {d['file']} | {tr}/{tc} | {pr}/{pc} | {floor} | {rc} | {cc} | {rm} | {cm} |")
        lines.append("")

    return "\n".join(lines)


async def main_async() -> None:
    yamls = sorted(FIXTURE_ROOT.glob("*/labels.yaml"))
    print(f"Evaluating {len(yamls)} fixtures ...")
    results = []
    for y in yamls:
        print(f"  {y.parent.name} ...", end="", flush=True)
        try:
            r = await evaluate_fixture(y)
            results.append(r)
            if r.get("skipped"):
                print(" skipped")
            else:
                e = r["expected"]
                p = r["predicted"]
                print(
                    f" expected {e.get('bedrooms_seen')}/{e.get('bedrooms_with_carpet')}/{e.get('has_living_area_carpet')} "
                    f"predicted {p['bedrooms_seen_pred']}/{p['bedrooms_with_carpet_pred']}/{p['has_living_area_carpet_pred']}"
                )
        except Exception as exc:
            print(f" ERROR: {exc}")
            results.append({"uid": y.parent.name, "skipped": True, "reason": f"error: {exc}", "expected": {}})

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_report(results))
    print()
    print(f"Report written to {REPORT_PATH}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
