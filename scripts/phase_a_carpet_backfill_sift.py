#!/usr/bin/env python
"""Run the new two-step carpet detector against every sift-labelled listing.

Reads sift labels from /tmp/sift_v1_active.db, fetches each listing's
images JSON from production, runs detect_carpet_two_step, and writes
bedrooms_with_carpet + has_living_area_carpet (plus the legacy booleans)
back to the production DB. Then rescores those listings.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Honour the env vars production now uses (q8 model, 1280px image, 8 photos).
os.environ.setdefault("VISION_MODEL", "qwen3-vl:2b-instruct-q8_0")
os.environ.setdefault("CARPET_PHOTOS_TO_CHECK", "8")
os.environ.setdefault("CARPET_IMG_MAX_EDGE", "1280")

from homehunt.carpet import detect_carpet_two_step  # noqa: E402
from homehunt.scorer import compute_score_from_db  # noqa: E402

SIFT_DB = "/tmp/sift_v1_active.db"
PROD_DB = PROJECT_ROOT / "data" / "homehunt.db"


def load_sift_uids() -> list[str]:
    con = sqlite3.connect(SIFT_DB)
    rows = con.execute(
        "SELECT DISTINCT uid FROM sifting_label_v2 WHERE session_id = (SELECT MAX(id) FROM sifting_session_v2)"
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def load_listing(uid: str) -> dict:
    con = sqlite3.connect(PROD_DB)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT uid, images, bedrooms FROM listing WHERE uid = ?",
        (uid,),
    ).fetchone()
    con.close()
    if row is None:
        return {}
    return {
        "uid": row["uid"],
        "images": json.loads(row["images"]) if row["images"] else [],
        "bedrooms": row["bedrooms"],
    }


def write_carpet_result(uid: str, result: dict) -> None:
    con = sqlite3.connect(PROD_DB)
    bwc = result.get("bedrooms_with_carpet")
    if bwc is None:
        bwc = 0
    con.execute(
        """
        UPDATE listing
        SET bedrooms_with_carpet = ?,
            has_living_area_carpet = ?,
            carpet_in_bedroom = ?,
            carpet_other_areas = ?,
            carpet_detected = ?,
            carpet_confidence = ?
        WHERE uid = ?
        """,
        (
            bwc,
            int(bool(result.get("has_living_area_carpet"))),
            int(bool(result.get("carpet_in_bedroom"))) if result.get("carpet_in_bedroom") is not None else None,
            int(bool(result.get("carpet_other_areas"))) if result.get("carpet_other_areas") is not None else None,
            int(bool(result.get("carpet_detected"))) if result.get("carpet_detected") is not None else None,
            result.get("carpet_confidence"),
            uid,
        ),
    )
    con.commit()
    con.close()


def rescore_uid(uid: str) -> int:
    con = sqlite3.connect(PROD_DB)
    con.row_factory = sqlite3.Row
    r = con.execute(
        """
        SELECT uid, price_numeric, bedrooms, size_sqft, carpet_in_bedroom,
               carpet_other_areas, bedrooms_with_carpet, has_living_area_carpet,
               epc_rating, commute_canary_wharf, commute_whitechapel, tfl_zone,
               garden, balcony, region
        FROM listing WHERE uid = ?
        """,
        (uid,),
    ).fetchone()
    if r is None:
        con.close()
        return 0
    commute = r["commute_canary_wharf"] or r["commute_whitechapel"]
    price_pcm = (r["price_numeric"] // 100) if r["price_numeric"] else None
    total, breakdown = compute_score_from_db(
        db_path=str(PROD_DB),
        price_pcm=price_pcm,
        size_sqft=r["size_sqft"],
        bedrooms=r["bedrooms"],
        carpet_in_bedroom=bool(r["carpet_in_bedroom"]) if r["carpet_in_bedroom"] is not None else None,
        carpet_other_areas=bool(r["carpet_other_areas"]) if r["carpet_other_areas"] is not None else None,
        bedrooms_with_carpet=r["bedrooms_with_carpet"],
        has_living_area_carpet=bool(r["has_living_area_carpet"]) if r["has_living_area_carpet"] is not None else None,
        epc_rating=r["epc_rating"],
        commute_mins=commute,
        tfl_zone=r["tfl_zone"],
        garden=bool(r["garden"]) if r["garden"] is not None else None,
        balcony=bool(r["balcony"]) if r["balcony"] is not None else None,
        region=r["region"],
    )
    con.execute(
        "UPDATE listing SET score = ?, score_breakdown = ? WHERE uid = ?",
        (total, json.dumps(breakdown), uid),
    )
    con.commit()
    con.close()
    return total


async def main_async() -> None:
    uids = load_sift_uids()
    print(f"Running carpet detector on {len(uids)} sift-labelled listings ...")
    for i, uid in enumerate(uids, 1):
        listing = load_listing(uid)
        if not listing or not listing["images"]:
            print(f"  [{i}/{len(uids)}] {uid} -- no images, skipping")
            continue
        result = await detect_carpet_two_step(listing["images"])
        bwc = result.get("bedrooms_with_carpet", 0)
        liv = result.get("has_living_area_carpet", False)
        write_carpet_result(uid, result)
        new_score = rescore_uid(uid)
        print(f"  [{i}/{len(uids)}] {uid} bwc={bwc} liv={liv} new_score={new_score}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
