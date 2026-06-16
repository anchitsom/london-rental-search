"""
Let-agreed re-check detector tests.

Project convention: plain async-style script, NOT pytest. Each test asserts
and prints PASS / FAIL; run via `.venv/bin/python tests/test_let_agreed_detection.py`.

Covers:
  1. Rightmove detector: legacy hydrated + dehydrated PAGE_MODEL fixtures.
  2. Zoopla detector: gallery-badge HTML fixtures.
  3. Defensive: empty / None / malformed input returns False, no exception.
  4. The target-set SQL query used by scripts/recheck_let_agreed.py against a
     fresh fixture DB built from the canonical Listing schema.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "let_agreed")
TARGET_FIXTURE_DB = os.path.join(
    os.path.dirname(__file__), "fixtures", "let_agreed_target.db"
)


def _load_json(name: str) -> dict:
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return json.load(f)


def _load_html(name: str) -> str:
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return f.read()


def _assert(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL [{label}]: {detail}")
    print(f"  PASS [{label}]")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_rightmove_detector():
    print("Test 1: Rightmove PAGE_MODEL detector (hydrated and dehydrated)")
    from homehunt.scrapers.rightmove_api import detect_let_agreed_from_page_model

    hyd_let = _load_json("rightmove_let_agreed.json")
    hyd_live = _load_json("rightmove_live.json")
    deh_let = _load_json("rightmove_dehydrated_let_agreed.json")
    deh_live = _load_json("rightmove_dehydrated_live.json")

    _assert(
        "hydrated_let_agreed_true",
        detect_let_agreed_from_page_model(hyd_let) is True,
        "hydrated let-agreed fixture did not detect",
    )
    _assert(
        "hydrated_live_false",
        detect_let_agreed_from_page_model(hyd_live) is False,
        "hydrated live fixture false-positive",
    )
    _assert(
        "dehydrated_let_agreed_true",
        detect_let_agreed_from_page_model(deh_let) is True,
        "dehydrated let-agreed fixture did not detect",
    )
    _assert(
        "dehydrated_live_false",
        detect_let_agreed_from_page_model(deh_live) is False,
        "dehydrated live fixture false-positive",
    )


def test_zoopla_detector():
    print("Test 2: Zoopla HTML detector")
    from homehunt.scrapers.zoopla_http import detect_let_agreed_from_html

    let_html = _load_html("zoopla_let_agreed.html")
    live_html = _load_html("zoopla_live.html")

    _assert(
        "zoopla_let_agreed_true",
        detect_let_agreed_from_html(let_html) is True,
        "let-agreed badge missed",
    )
    _assert(
        "zoopla_live_false",
        detect_let_agreed_from_html(live_html) is False,
        "live badge false-positive",
    )


def test_detector_defensive():
    print("Test 3: detectors return False on empty / None / malformed input")
    from homehunt.scrapers.rightmove_api import detect_let_agreed_from_page_model
    from homehunt.scrapers.zoopla_http import detect_let_agreed_from_html

    # Rightmove: None, empty dict, junk dict, malformed dehydrated.
    _assert("rm_none", detect_let_agreed_from_page_model(None) is False)
    _assert("rm_empty_dict", detect_let_agreed_from_page_model({}) is False)
    _assert(
        "rm_junk_dict",
        detect_let_agreed_from_page_model({"propertyData": "not-a-dict"}) is False,
    )
    _assert(
        "rm_malformed_dehydrated",
        detect_let_agreed_from_page_model(
            {"data": "not-json", "encoding": "x"}
        ) is False,
    )
    _assert(
        "rm_empty_array_dehydrated",
        detect_let_agreed_from_page_model({"data": "[]", "encoding": "x"}) is False,
    )

    # Zoopla: None, empty, junk.
    _assert("zp_none", detect_let_agreed_from_html(None) is False)
    _assert("zp_empty", detect_let_agreed_from_html("") is False)
    _assert(
        "zp_junk",
        detect_let_agreed_from_html("<html><body>nothing here</body></html>") is False,
    )


def _build_target_fixture_db(path: str) -> None:
    """Build a fresh DB with a mix of states/scores for target-set query tests."""
    if os.path.exists(path):
        os.remove(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    from sqlalchemy import create_engine
    from homehunt.core.db import Listing  # noqa: F401
    from sqlmodel import SQLModel
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()

    conn = sqlite3.connect(path)
    cur = conn.cursor()

    base_ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()

    # Seven rows hitting every relevant case.
    cases = [
        # (uid, current_status, score, is_active, should_be_in_target_set)
        ("rightmove:contact_queued_low",   "contact_queued",   60, 1, True),   # contact_queued included regardless of score
        ("rightmove:contacted_low",        "contacted",        50, 1, True),   # contacted included regardless of score
        ("rightmove:new_score_90",         "new",              90, 1, True),   # new with score >= 85 included
        ("rightmove:null_status_score_95", None,               95, 1, True),   # NULL status with score >= 85 included
        ("rightmove:new_score_80",         "new",              80, 1, False),  # new with score < 85 excluded
        ("rightmove:null_status_score_70", None,               70, 1, False),  # NULL status with score < 85 excluded
        ("rightmove:shortlisted_high",     "shortlisted",      99, 1, False),  # shortlisted not in target set
        ("rightmove:inactive_queued",      "contact_queued",   90, 0, False),  # inactive excluded
    ]

    rows = []
    for i, (uid, status, score, is_active, _expected) in enumerate(cases):
        rows.append(
            (
                uid,
                "RIGHTMOVE",
                str(1000 + i),
                f"https://www.rightmove.co.uk/properties/{1000 + i}",
                f"row {i}",                     # title
                "£2000",                         # price
                200000,                          # price_numeric
                2,                               # bedrooms
                "Test addr, London",             # address
                "N5 1AA",                        # postcode
                "Highbury",                      # area
                "direct_http",                   # extraction_method
                base_ts,                         # first_seen
                base_ts,                         # last_scraped
                1,                               # scrape_count
                bool(is_active),                 # is_active
                "active" if is_active else "inactive",
                score,                           # score
                status,                          # current_status
            )
        )

    cur.executemany(
        """
        INSERT INTO listing (
            uid, portal, property_id, url, title, price, price_numeric,
            bedrooms, address, postcode, area,
            extraction_method, first_seen, last_scraped,
            scrape_count, is_active, status,
            score, current_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def test_target_set_query():
    print("Test 4: target-set SQL query returns exactly the expected uids")
    _build_target_fixture_db(TARGET_FIXTURE_DB)

    expected_uids = {
        "rightmove:contact_queued_low",
        "rightmove:contacted_low",
        "rightmove:new_score_90",
        "rightmove:null_status_score_95",
    }

    con = sqlite3.connect(TARGET_FIXTURE_DB)
    try:
        rows = con.execute(
            """
            SELECT uid, portal, url, current_status, score
            FROM listing
            WHERE is_active = 1
              AND (
                current_status IN ('contact_queued', 'contacted')
                OR (
                  (current_status IS NULL OR current_status = 'new')
                  AND score >= 85
                )
              )
            ORDER BY portal, score DESC NULLS LAST, uid
            """
        ).fetchall()
    finally:
        con.close()

    got_uids = {r[0] for r in rows}
    _assert(
        "target_set_matches_expected",
        got_uids == expected_uids,
        f"expected {sorted(expected_uids)}, got {sorted(got_uids)}",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        ("rightmove_detector", test_rightmove_detector),
        ("zoopla_detector", test_zoopla_detector),
        ("detector_defensive", test_detector_defensive),
        ("target_set_query", test_target_set_query),
    ]

    failures = []
    for name, fn in tests:
        print(f"\n=== {name} ===")
        try:
            fn()
        except AssertionError as exc:
            print(f"  {exc}")
            failures.append(name)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            failures.append(name)

    print(f"\n{'=' * 60}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print(f"ALL {len(tests)} test groups passed.")
