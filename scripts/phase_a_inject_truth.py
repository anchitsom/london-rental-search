#!/usr/bin/env python
"""Inject the labelled-fixture ground truth into production for evaluation.

For each tests/fixtures/carpet/<uid>/labels.yaml, copy the expected block's
bedrooms_with_carpet and has_living_area_carpet into the production listing
row. Used to evaluate the calibration loop with perfect detection on the
fixture set, without re-running the carpet detector.

Then rescore those listings.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from homehunt.scorer import compute_score_from_db  # noqa: E402

FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "carpet"
DB_PATH = PROJECT_ROOT / "data" / "homehunt.db"


def main() -> None:
    yamls = sorted(FIXTURE_ROOT.glob("*/labels.yaml"))
    print(f"Reading {len(yamls)} fixtures ...")

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    updated = 0
    skipped = 0
    for y in yamls:
        data = yaml.safe_load(y.read_text())
        uid = data["listing_uid"]
        expected = data.get("expected") or {}
        bwc = expected.get("bedrooms_with_carpet")
        liv = bool(expected.get("has_living_area_carpet"))
        bedrooms_seen = expected.get("bedrooms_seen", 0)
        # Skip Rightmove rows that had no bedroom photos -- meaningless to inject zeros.
        if bedrooms_seen == 0:
            skipped += 1
            continue
        con.execute(
            """
            UPDATE listing
            SET bedrooms_with_carpet = ?,
                has_living_area_carpet = ?,
                carpet_in_bedroom = ?,
                carpet_other_areas = ?
            WHERE uid = ?
            """,
            (bwc, int(liv), int(bwc > 0) if bwc is not None else None, int(liv), uid),
        )
        if con.execute("SELECT changes()").fetchone()[0]:
            updated += 1
    con.commit()
    print(f"  injected truth into {updated} fixtures, skipped {skipped} without bedroom photos")

    rows = con.execute(
        """
        SELECT l.uid, l.price_numeric, l.bedrooms, l.size_sqft, l.carpet_in_bedroom,
               l.carpet_other_areas, l.bedrooms_with_carpet, l.has_living_area_carpet,
               l.epc_rating, l.commute_canary_wharf, l.commute_whitechapel, l.tfl_zone,
               l.garden, l.balcony, l.region
        FROM listing l
        WHERE l.is_active = 1
        """
    ).fetchall()
    print(f"Rescoring {len(rows)} active listings ...")
    updates = []
    for r in rows:
        commute = r["commute_canary_wharf"] or r["commute_whitechapel"]
        price_pcm = (r["price_numeric"] // 100) if r["price_numeric"] else None
        total, breakdown = compute_score_from_db(
            db_path=str(DB_PATH),
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
        updates.append((total, json.dumps(breakdown), r["uid"]))
    con.executemany(
        "UPDATE listing SET score = ?, score_breakdown = ? WHERE uid = ?",
        updates,
    )
    con.commit()
    con.close()
    print(f"Rescored {len(updates)} listings")


if __name__ == "__main__":
    main()
