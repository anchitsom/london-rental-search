"""
Latent tag viewer filter tests.

Run directly:
    .venv/bin/python tests/test_viewer_tag_filters.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlmodel import SQLModel

from homehunt.core.db import Listing  # noqa: F401  (registers the table)
from homehunt.latent_tags import LATENT_TAG_AXES


TAG_COLUMNS = [f"tag_{axis}" for axis in LATENT_TAG_AXES]

ROWS = [
    ("tag-01", "floor_to_ceiling", "period", "wood"),
    ("tag-02", "floor_to_ceiling", "new_build", "laminate"),
    ("tag-03", "sash", "period", "wood"),
    ("tag-04", "sash", "period", "carpet"),
    ("tag-05", "casement", "mid_century", "tile"),
    ("tag-06", "unclear", "period", "mixed"),
    ("tag-07", "picture", "warehouse_conversion", "wood"),
    ("tag-08", "floor_to_ceiling", "period", "tile"),
    ("tag-09", "skylight", "new_build", "laminate"),
    ("tag-10", "mixed", "ex_local", "carpet"),
]


def _create_db(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()

    conn = sqlite3.connect(path)
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(listing)").fetchall()}
        for column in TAG_COLUMNS:
            if column not in existing:
                conn.execute(f"ALTER TABLE listing ADD COLUMN {column} TEXT")
        if "tags_extracted_at" not in existing:
            conn.execute("ALTER TABLE listing ADD COLUMN tags_extracted_at DATETIME")
        if "tag_raw_captions" not in existing:
            conn.execute("ALTER TABLE listing ADD COLUMN tag_raw_captions TEXT")

        for index, (uid, window_style, building_era, flooring) in enumerate(ROWS, start=1):
            conn.execute(
                """
                INSERT INTO listing (
                    uid, portal, property_id, url, extraction_method, status,
                    is_active, current_status, first_seen, last_scraped,
                    scrape_count, title, price_numeric, bedrooms, score,
                    tag_window_style, tag_building_era, tag_flooring
                )
                VALUES (?, 'OPENRENT', ?, ?, 'direct_http', 'active',
                        1, 'new', '2026-06-01 12:00:00.000000',
                        '2026-06-01 12:00:00.000000', 1, ?, ?, 2, ?,
                        ?, ?, ?)
                """,
                (
                    uid,
                    uid,
                    f"https://example.test/{uid}",
                    f"Tagged listing {uid}",
                    200000 + index,
                    100 - index,
                    window_style,
                    building_era,
                    flooring,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _uids(result: dict) -> set[str]:
    return {row["uid"] for row in result["listings"]}


def _report(label: str, ok: bool, detail: str = "") -> bool:
    suffix = f" ({detail})" if detail else ""
    print(f"{'PASS' if ok else 'FAIL'} {label}{suffix}")
    return ok


def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "viewer-tags.db"
        _create_db(db_path)
        os.environ["HOMEHUNT_DB"] = str(db_path)

        from fastapi.testclient import TestClient

        from homehunt.api import app
        from homehunt.viewer_query import list_listings

        cases = [
            (
                "single tag_window_style filters floor_to_ceiling",
                {"tag_window_style": "floor_to_ceiling"},
                {"tag-01", "tag-02", "tag-08"},
            ),
            (
                "comma tag_window_style ORs floor_to_ceiling and sash",
                {"tag_window_style": "floor_to_ceiling,sash"},
                {"tag-01", "tag-02", "tag-03", "tag-04", "tag-08"},
            ),
            (
                "two tag axes AND together",
                {"tag_window_style": "floor_to_ceiling", "tag_building_era": "period"},
                {"tag-01", "tag-08"},
            ),
            (
                "unclear is dropped from mixed comma filter",
                {"tag_window_style": "floor_to_ceiling,unclear"},
                {"tag-01", "tag-02", "tag-08"},
            ),
            (
                "unclear-only tag filter is omitted",
                {"tag_window_style": "unclear"},
                {row[0] for row in ROWS},
            ),
        ]

        for label, filters, expected in cases:
            got = _uids(list_listings(filters=filters, sort="score_desc", page=1, page_size=50))
            if not _report(label, got == expected, f"got={sorted(got)} expected={sorted(expected)}"):
                failures += 1

        client = TestClient(app)
        response = client.get("/viewer?tag_window_style=floor_to_ceiling")
        html = response.text
        if not _report("viewer route accepts floor_to_ceiling tag filter", response.status_code == 200 and "3 listings" in html):
            failures += 1

        response = client.get("/viewer?tag_window_style=floor_to_ceiling,sash&tag_building_era=period")
        html = response.text
        if not _report("viewer route applies OR within and AND across axes", response.status_code == 200 and "4 listings" in html):
            failures += 1

        response = client.get("/api/listings?tag_window_style=sash")
        payload = response.json()
        got = {row["uid"] for row in payload}
        if not _report("api_listings applies tag filters", response.status_code == 200 and got == {"tag-03", "tag-04"}):
            failures += 1

    print("ALL PASS" if failures == 0 else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
