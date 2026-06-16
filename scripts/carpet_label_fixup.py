#!/usr/bin/env python
"""One-shot fixer for sift v1 carpet fixtures.

For each tests/fixtures/carpet/<uid>/labels.yaml:
    - Bedroom-tagged photos with empty bedroom_slug get sequential slugs
      (bedroom_1, bedroom_2, ...).
    - The expected block is recomputed: bedrooms_seen and bedrooms_with_carpet
      are unique-slug counts capped at bedrooms_declared.

The original yaml is backed up to labels.original.yaml before modification.
Idempotent: re-running on already-fixed fixtures only recomputes expected.
"""

from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "carpet"

LIVING_AREA_ROOMS = {"living-room", "hallway", "dining-room"}


def assign_default_slugs(photos: list[dict]) -> list[dict]:
    counter = 0
    for entry in photos:
        if entry.get("room_type") == "bedroom" and not entry.get("bedroom_slug"):
            counter += 1
            entry["bedroom_slug"] = f"bedroom_{counter}"
    return photos


def recompute_expected(photos: list[dict], bedrooms_declared: int | None) -> dict:
    bedrooms_seen: set[str] = set()
    bedrooms_with_carpet: set[str] = set()
    has_living_area_carpet = False
    for entry in photos:
        rt = entry.get("room_type", "other")
        carpet = bool(entry.get("has_carpet"))
        slug = entry.get("bedroom_slug") or None
        if rt == "bedroom" and slug:
            bedrooms_seen.add(slug)
            if carpet:
                bedrooms_with_carpet.add(slug)
        elif rt in LIVING_AREA_ROOMS and carpet:
            has_living_area_carpet = True
    bs = len(bedrooms_seen)
    bwc = len(bedrooms_with_carpet)
    if bedrooms_declared is not None:
        bs = min(bs, bedrooms_declared)
        bwc = min(bwc, bedrooms_declared)
    return {
        "bedrooms_seen": bs,
        "bedrooms_with_carpet": bwc,
        "has_living_area_carpet": has_living_area_carpet,
    }


def fixup_fixture(yaml_path: Path) -> dict:
    data = yaml.safe_load(yaml_path.read_text())
    before = dict(data.get("expected", {}))

    backup = yaml_path.with_suffix(".original.yaml")
    if not backup.exists():
        backup.write_text(yaml_path.read_text())

    photos = data.get("photos", [])
    photos = assign_default_slugs(photos)
    data["photos"] = photos
    declared = data.get("bedrooms_declared")
    data["expected"] = recompute_expected(photos, declared)

    yaml_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return {
        "uid": data["listing_uid"],
        "before": before,
        "after": data["expected"],
        "bedroom_photos": sum(1 for p in photos if p.get("room_type") == "bedroom"),
        "unique_slugs": len({p.get("bedroom_slug") for p in photos if p.get("bedroom_slug")}),
    }


def main() -> None:
    yamls = sorted(FIXTURE_ROOT.glob("*/labels.yaml"))
    print(f"Found {len(yamls)} fixtures")
    print()
    print(f"{'uid':<32} {'bed_photos':>10} {'slugs':>6}  {'bs_after':>8} {'bwc_after':>9} {'liv':>5}")
    print("-" * 80)
    for y in yamls:
        result = fixup_fixture(y)
        a = result["after"]
        print(
            f"{result['uid']:<32} "
            f"{result['bedroom_photos']:>10} "
            f"{result['unique_slugs']:>6}  "
            f"{a['bedrooms_seen']:>8} "
            f"{a['bedrooms_with_carpet']:>9} "
            f"{str(a['has_living_area_carpet']):>5}"
        )


if __name__ == "__main__":
    main()
