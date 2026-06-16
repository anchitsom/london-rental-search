#!/usr/bin/env python
"""Phase A calibration backfill.

Populates four new columns on every active listing:
    photo_count            from len(parsed images JSON)
    floor_level_int        from parsing floor_level string
    size_sqft_credibility  from comparing size_sqft (scraped) against floorplan_data
    is_let_agreed          from title NLP grep

No model calls. Pure derivation. Idempotent.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "homehunt.db"

LET_AGREED_PATTERNS = [
    r"\blet\s*agreed\b",
    r"\bunder\s*offer\b",
    r"\blet\s*by\b",
    r"\bnow\s*let\b",
    r"\btenanted\b",
]
LET_AGREED_RE = re.compile("|".join(LET_AGREED_PATTERNS), re.IGNORECASE)


def parse_photo_count(images_raw: str | None) -> int | None:
    if not images_raw:
        return 0
    try:
        parsed = json.loads(images_raw)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict) and "urls" in parsed:
        return len(parsed["urls"])
    return None


def parse_floor_level(raw: str | None) -> int | None:
    """Map the messy EPC-derived floor_level string to a normalised integer.

    Examples seen in production:
        "00", "01", "02", "1st", "2nd", "Ground", "ground floor", "mid floor", "17"

    Returns None for anything we cannot resolve. Ground floor maps to 0.
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if s in {"ground", "ground floor", "gr", "g"}:
        return 0
    # Numeric prefix (e.g. "01", "17")
    m = re.match(r"^(-?\d+)", s)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    # Ordinal forms (e.g. "1st", "2nd", "21st")
    m = re.match(r"^(\d+)(st|nd|rd|th)\b", s)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    # Words we can't quantify ("mid floor", "top floor"): leave null.
    return None


def derive_credibility(size_sqft: int | None, floorplan_raw: str | None) -> str | None:
    """Compare scraped size_sqft against floorplan-derived total area.

    Returns:
        "trusted"  if both present and within 10%
        "conflict" if both present but disagree by more than 10%
        "missing"  if either is missing
    """
    floorplan_sqft: float | None = None
    if floorplan_raw:
        try:
            d = json.loads(floorplan_raw)
            if isinstance(d, dict):
                for k in ("total_size_sqft", "total_area_sqft", "total_sqft", "size_sqft"):
                    v = d.get(k)
                    if isinstance(v, (int, float)) and v > 0:
                        floorplan_sqft = float(v)
                        break
                if floorplan_sqft is None:
                    rooms = d.get("rooms") or []
                    if isinstance(rooms, list):
                        s = sum(
                            float(r.get("size_sqft") or 0)
                            for r in rooms
                            if isinstance(r, dict) and isinstance(r.get("size_sqft"), (int, float))
                        )
                        if s > 0:
                            floorplan_sqft = s
        except json.JSONDecodeError:
            pass

    if size_sqft is None or floorplan_sqft is None:
        return "missing"
    if size_sqft <= 0 or floorplan_sqft <= 0:
        return "missing"
    diff = abs(size_sqft - floorplan_sqft) / max(size_sqft, floorplan_sqft)
    return "trusted" if diff <= 0.10 else "conflict"


def detect_let_agreed(title: str | None) -> bool:
    if not title:
        return False
    return bool(LET_AGREED_RE.search(title))


def main() -> None:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT uid, images, floor_level, size_sqft, floorplan_data, title FROM listing"
    ).fetchall()
    print(f"Backfilling {len(rows)} listings ...")

    photo_counts = floor_levels = creds = let_agreeds = 0
    updates = []
    for r in rows:
        photo_count = parse_photo_count(r["images"])
        floor_level_int = parse_floor_level(r["floor_level"])
        credibility = derive_credibility(r["size_sqft"], r["floorplan_data"])
        is_let_agreed = detect_let_agreed(r["title"])

        if photo_count is not None:
            photo_counts += 1
        if floor_level_int is not None:
            floor_levels += 1
        if credibility is not None:
            creds += 1
        if is_let_agreed:
            let_agreeds += 1

        updates.append((
            photo_count, floor_level_int, credibility, int(is_let_agreed), r["uid"]
        ))

    con.executemany(
        """
        UPDATE listing
        SET photo_count = ?,
            floor_level_int = ?,
            size_sqft_credibility = ?,
            is_let_agreed = ?
        WHERE uid = ?
        """,
        updates,
    )
    con.commit()
    con.close()

    print(f"  photo_count populated:           {photo_counts}/{len(rows)}")
    print(f"  floor_level_int populated:       {floor_levels}/{len(rows)}")
    print(f"  size_sqft_credibility populated: {creds}/{len(rows)}")
    print(f"  is_let_agreed = True:            {let_agreeds}")


if __name__ == "__main__":
    main()
