from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from homehunt.latent_tags import LATENT_TAG_AXES


TAG_COLUMNS = [f"tag_{axis}" for axis in LATENT_TAG_AXES]


def _create_db(path: Path) -> None:
    tag_defs = ", ".join(f"{column} TEXT" for column in TAG_COLUMNS)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            f"""
            CREATE TABLE listing (
                uid TEXT PRIMARY KEY,
                portal TEXT,
                url TEXT,
                title TEXT,
                is_active INTEGER,
                current_status TEXT,
                score INTEGER,
                price_numeric INTEGER,
                built_form TEXT,
                {tag_defs},
                tags_extracted_at DATETIME,
                tag_raw_captions TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE listing_lifecycle (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT,
                from_status TEXT,
                to_status TEXT,
                reason TEXT,
                notes TEXT,
                transitioned_at TEXT,
                transitioned_by TEXT
            )
            """
        )
        for index in range(1, 26):
            values = {column: None for column in TAG_COLUMNS}
            values["tag_window_style"] = "floor_to_ceiling" if index % 2 else "sash"
            values["tag_flooring"] = "wood"
            uid = f"tagged-{index:02d}"
            conn.execute(
                f"""
                INSERT INTO listing (
                    uid, portal, url, title, is_active, current_status,
                    score, price_numeric, built_form, {", ".join(TAG_COLUMNS)},
                    tags_extracted_at, tag_raw_captions
                )
                VALUES (
                    ?, 'OPENRENT', ?, ?, 1, 'new', ?, ?, 'Mid-Terrace',
                    {", ".join("?" for _ in TAG_COLUMNS)},
                    '2026-06-01 12:00:00', ?
                )
                """,
                [
                    uid,
                    f"https://example.test/{uid}",
                    f"Tagged listing {uid}",
                    100 - index,
                    200000 + index,
                    *[values[column] for column in TAG_COLUMNS],
                    '["bright room with floor to ceiling or sash windows"]',
                ],
            )
        values = {column: None for column in TAG_COLUMNS}
        conn.execute(
            f"""
            INSERT INTO listing (
                uid, portal, url, title, is_active, current_status,
                score, price_numeric, built_form, {", ".join(TAG_COLUMNS)}
            )
            VALUES (
                'untagged-01', 'OPENRENT', 'https://example.test/untagged-01',
                'Untagged listing', 1, 'new', 1, 250000, 'Mid-Terrace',
                {", ".join("?" for _ in TAG_COLUMNS)}
            )
            """,
            [values[column] for column in TAG_COLUMNS],
        )
        conn.commit()
    finally:
        conn.close()


def _uids_from_cards(html: str) -> set[str]:
    return set(re.findall(r'<li class="card" data-uid="([^"]+)"', html))


def _client_for_temp_db(tmp: str) -> TestClient:
    db_path = Path(tmp) / "viewer-tags.db"
    _create_db(db_path)
    os.environ["HOMEHUNT_DB"] = str(db_path)

    from homehunt.api import app

    return TestClient(app)


def test_viewer_renders_vision_tag_multiselects() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _client_for_temp_db(tmp)
        response = client.get("/viewer")
    assert response.status_code == 200
    assert response.text.count('<select class="field__select field__select--multi"') >= 13
    assert response.text.index('id="built_form"') < response.text.index('<fieldset class="vision-tags">')
    assert response.text.index('<fieldset class="vision-tags">') < response.text.index('<div class="controls__actions">')


def test_repeated_and_comma_tag_filters_match() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _client_for_temp_db(tmp)
        repeated = client.get("/viewer?tag_window_style=sash&tag_window_style=floor_to_ceiling")
        comma = client.get("/viewer?tag_window_style=sash,floor_to_ceiling")

    assert repeated.status_code == 200
    assert comma.status_code == 200
    assert _uids_from_cards(repeated.text) == _uids_from_cards(comma.text)
    assert "tagged-01" in repeated.text
    assert "tagged-02" in repeated.text


def test_pagination_preserves_repeated_tag_filters() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _client_for_temp_db(tmp)
        response = client.get("/viewer?tag_window_style=sash&tag_window_style=floor_to_ceiling")
    assert response.status_code == 200
    assert "page 1 of 2" in response.text
    assert "tag_window_style=sash&amp;tag_window_style=floor_to_ceiling" in response.text


def test_listing_detail_tag_chips_hide_when_untagged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _client_for_temp_db(tmp)
        tagged = client.get("/listing/tagged-01")
        untagged = client.get("/listing/untagged-01")

    assert tagged.status_code == 200
    assert untagged.status_code == 200
    assert tagged.text.count('<li class="tag-chips__chip"') >= 1
    assert untagged.text.count('<li class="tag-chips__chip"') == 0


def main() -> int:
    tests = [
        test_viewer_renders_vision_tag_multiselects,
        test_repeated_and_comma_tag_filters_match,
        test_pagination_preserves_repeated_tag_filters,
        test_listing_detail_tag_chips_hide_when_untagged,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
