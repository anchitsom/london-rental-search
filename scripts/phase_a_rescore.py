#!/usr/bin/env python
"""Insert a fresh scoring_weights row from yaml and rescore every active listing.

Run after Phase A yaml weight rebalance. Reads the current
filter-scoring-config.yaml, inserts a new scoring_weights row, then loops
over every active listing in production and computes a new score using the
in-process scorer (so the new fields like bedrooms_with_carpet feed in).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from homehunt.scorer import compute_score_from_db  # noqa: E402

DB_PATH = PROJECT_ROOT / "data" / "homehunt.db"
CONFIG_PATH = PROJECT_ROOT / "filter-scoring-config.yaml"


def insert_weights_row() -> None:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    weights = {k: v.get("weight", 0.0) for k, v in cfg["scoring"]["components"].items()}
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO scoring_weights (source, weights_json, created_at) VALUES (?, ?, ?)",
        (
            "phase-a-2026-05-10-calibration",
            json.dumps(weights),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    con.commit()
    con.close()
    print("Inserted scoring_weights row source=phase-a-2026-05-10-calibration")
    print(f"  weights sum: {sum(weights.values())}")


def rescore_active() -> None:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT uid, price_numeric, bedrooms, size_sqft, carpet_in_bedroom,
               carpet_other_areas, bedrooms_with_carpet, has_living_area_carpet,
               epc_rating, commute_canary_wharf, commute_whitechapel, tfl_zone,
               garden, balcony, region
        FROM listing
        WHERE is_active = 1
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
    print(f"Updated {len(updates)} listings")


def main() -> None:
    insert_weights_row()
    rescore_active()


if __name__ == "__main__":
    main()
