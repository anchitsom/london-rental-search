"""
Acceptance test for rental-carpet-binary.

Stdlib-only harness; imports real project modules from this checkout.
Run:
  ./.venv/bin/python tests/acceptance_rental_carpet_binary.py
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


RESULTS = []


def record(num, label, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    suffix = f" - {detail}" if detail else ""
    print(f"{status} criterion {num}: {label}{suffix}")
    RESULTS.append(ok)


async def detector_fixture(responses_by_call, urls, batch_size=4):
    import homehunt.carpet as carpet

    old_batch_size = carpet.CARPET_BATCH_SIZE
    old_fetch = carpet._fetch_image_b64
    old_query = carpet._query_carpet_batch
    calls = []

    async def fake_fetch(client, url):
        return f"b64:{url}"

    async def fake_query(client, images):
        calls.append(list(images))
        idx = len(calls) - 1
        return responses_by_call[idx]

    try:
        carpet.CARPET_BATCH_SIZE = batch_size
        carpet._fetch_image_b64 = fake_fetch
        carpet._query_carpet_batch = fake_query
        result = await carpet.detect_carpet_any(urls)
    finally:
        carpet.CARPET_BATCH_SIZE = old_batch_size
        carpet._fetch_image_b64 = old_fetch
        carpet._query_carpet_batch = old_query

    return result, calls


async def run_detector_criteria():
    import homehunt.carpet as carpet

    urls = [f"photo-{i}" for i in range(9)]
    responses = [
        [{"carpet": False, "confidence": 0.6} for _ in range(4)],
        [{"carpet": False, "confidence": 0.6} for _ in range(4)],
        [{"carpet": True, "confidence": 0.9}],
    ]
    result, calls = await detector_fixture(responses, urls, batch_size=4)
    record(
        1,
        "detect_carpet_any examines all photos in CARPET_BATCH_SIZE chunks",
        len(result.get("per_photo", [])) == 9 and len(calls) == 3,
        f"per_photo={len(result.get('per_photo', []))}, calls={len(calls)}",
    )
    record(
        2,
        "any carpet=True photo makes carpet_detected True",
        result.get("carpet_detected") is True,
        f"carpet_detected={result.get('carpet_detected')!r}",
    )

    false_responses = [
        [{"carpet": False, "confidence": 0.7} for _ in range(4)],
        [{"carpet": False, "confidence": 0.7} for _ in range(2)],
    ]
    result_false, _ = await detector_fixture(false_responses, [f"clean-{i}" for i in range(6)])
    record(
        3,
        "all carpet=False photos make carpet_detected False",
        result_false.get("carpet_detected") is False,
        f"carpet_detected={result_false.get('carpet_detected')!r}",
    )

    result_empty = await carpet.detect_carpet_any([])
    record(
        4,
        "empty photo_urls returns carpet_detected None",
        result_empty.get("carpet_detected") is None,
        f"carpet_detected={result_empty.get('carpet_detected')!r}",
    )

    mismatch_result, _ = await detector_fixture(
        [[{"carpet": True, "confidence": 0.99}]],
        ["a", "b", "c"],
    )
    record(
        5,
        "mismatched batch response is unknown, not carpet=True",
        mismatch_result.get("carpet_detected") is not True,
        f"carpet_detected={mismatch_result.get('carpet_detected')!r}",
    )

    room_fields_null = all(
        result.get(k) is None
        for k in (
            "carpet_in_bedroom",
            "carpet_other_areas",
            "bedrooms_with_carpet",
            "has_living_area_carpet",
        )
    )
    required_keys = all(
        k in result
        for k in ("carpet_detected", "carpet_confidence", "carpet_photo_count")
    )
    record(
        6,
        "result nulls room-area fields and includes binary fields",
        room_fields_null and required_keys,
    )

    record(
        7,
        "CARPET_IMG_MAX_EDGE default is 1280",
        carpet.CARPET_IMG_MAX_EDGE == 1280,
        f"CARPET_IMG_MAX_EDGE={carpet.CARPET_IMG_MAX_EDGE}",
    )


def run_scorer_criteria():
    from homehunt import scorer

    base = dict(
        price_pcm=2500,
        size_sqft=700,
        bedrooms=2,
        carpet_confidence=0.9,
        carpet_in_bedroom=None,
        carpet_other_areas=None,
        bedrooms_with_carpet=None,
        has_living_area_carpet=None,
        epc_rating="C",
        commute_mins=20,
        tfl_zone=2,
        garden=True,
        balcony=False,
    )
    total_true, breakdown_true = scorer.compute_score(carpet_detected=True, **base)
    total_false, breakdown_false = scorer.compute_score(carpet_detected=False, **base)
    _, breakdown_none = scorer.compute_score(carpet_detected=None, **base)

    record(8, "carpet_detected=True scores detected_score 20", breakdown_true.get("carpet") == 20)
    record(9, "carpet_detected=False scores 100", breakdown_false.get("carpet") == 100)
    record(10, "carpet_detected=None scores null_score 50", breakdown_none.get("carpet") == 50)

    cfg = scorer._load_config(str(ROOT / "filter-scoring-config.yaml"))
    carpet_block = cfg["scoring"]["components"]["carpet"]
    record(
        11,
        "carpet weight remains 0.25 and lowers weighted total when detected",
        carpet_block.get("weight") == 0.25 and total_true < total_false,
        f"true={total_true}, false={total_false}, weight={carpet_block.get('weight')}",
    )
    record(
        12,
        "active carpet config has binary keys and no bedrooms_with_carpet_score",
        carpet_block.get("detected_score") == 20
        and carpet_block.get("not_detected_score") == 100
        and carpet_block.get("null_score") == 50
        and "bedrooms_with_carpet_score" not in carpet_block,
        json.dumps(carpet_block, sort_keys=True),
    )


async def run_wiring_criteria():
    import run
    from homehunt.core.db import Database, Listing
    from homehunt.core.models import ExtractionMethod, Portal, PropertyType

    source = (ROOT / "run.py").read_text()
    old_detect = run.detect_carpet_any
    guarded_seen = {}

    async def fake_detect(photo_urls):
        guarded_seen["photo_urls"] = list(photo_urls)
        return {
            "carpet_detected": True,
            "carpet_confidence": 0.87,
            "carpet_in_bedroom": None,
            "carpet_other_areas": None,
            "bedrooms_with_carpet": None,
            "has_living_area_carpet": None,
            "per_photo": [],
            "carpet_photo_count": len(photo_urls),
        }

    try:
        run.detect_carpet_any = fake_detect
        await run._detect_carpet_guarded([f"photo-{i}" for i in range(6)])
    finally:
        run.detect_carpet_any = old_detect

    static_ok = (
        "from homehunt.carpet import detect_carpet_any" in source
        and "detect_carpet_two_step" not in source
        and "_detect_carpet_guarded(images)" in source
        and "_detect_carpet_guarded(photos)" in source
        and "[:CARPET_PHOTOS_TO_CHECK]" not in source
    )
    runtime_ok = guarded_seen.get("photo_urls") == [f"photo-{i}" for i in range(6)]
    record(
        13,
        "run.py calls detect_carpet_any through guarded full-photo paths",
        static_ok and runtime_ok,
    )

    tmp = tempfile.NamedTemporaryFile(prefix="rental-carpet-binary-", suffix=".db", delete=False)
    tmp.close()
    try:
        db = Database(f"sqlite:///{tmp.name}")
        db.create_tables()
        partial = Listing(
            uid="rightmove:acceptance-carpet",
            portal=Portal.RIGHTMOVE,
            property_id="acceptance-carpet",
            url="https://example.test/listing",
            address="1 Test Street",
            latitude=51.5,
            longitude=-0.1,
            price="£2,500 pcm",
            price_numeric=250000,
            bedrooms=2,
            bathrooms=1,
            size_sqft=700,
            property_type=PropertyType.FLAT,
            title="Acceptance listing",
            images=json.dumps(["p1", "p2"]),
            extraction_method=ExtractionMethod.DIRECT_HTTP,
            carpet_detected=None,
            carpet_confidence=None,
            carpet_in_bedroom=None,
            carpet_other_areas=None,
            bedrooms_with_carpet=None,
            has_living_area_carpet=None,
            epc_rating="C",
            tfl_zone=2,
            commute_canary_wharf=20,
            commute_whitechapel=25,
            garden=False,
            balcony=False,
            region="Highbury",
            first_seen=datetime.utcnow(),
            last_scraped=datetime.utcnow(),
            scrape_count=1,
            is_active=True,
            status="active",
        )
        enriched = {
            "size_sqft": 700,
            "size_sqft_source": "structured",
            "size_sqft_confidence": 1.0,
            "epc": "C",
            "epc_rating_source": "fixture",
            "epc_rating_confidence": "fixture",
            "carpet": await fake_detect(["p1", "p2"]),
            "floorplan": {},
            "raw": {"images": ["p1", "p2"]},
            "tfl": {
                "commute_to": {"canary_wharf": 20, "whitechapel": 25},
                "tfl_zone": 2,
            },
        }
        final = run._finalise_listing(partial, enriched)
        run._upsert(db, final)
        db.engine.dispose()
        asyncio.get_event_loop()

        conn = sqlite3.connect(tmp.name)
        try:
            row = conn.execute(
                """
                SELECT carpet_detected, carpet_confidence, carpet_in_bedroom,
                       carpet_other_areas, bedrooms_with_carpet,
                       has_living_area_carpet, score
                FROM listing WHERE uid = ?
                """,
                ("rightmove:acceptance-carpet",),
            ).fetchone()
            table_info = conn.execute("PRAGMA table_info(listing)").fetchall()
            db_row = conn.execute(
                "SELECT * FROM listing WHERE uid = ?",
                ("rightmove:acceptance-carpet",),
            ).fetchone()
        finally:
            conn.close()

        notnull_indexes = [col[0] for col in table_info if col[3] and not col[5]]
        notnull_ok = all(db_row[i] is not None for i in notnull_indexes)
        row_ok = (
            row is not None
            and row[0] == 1
            and abs(float(row[1]) - 0.87) < 0.0001
            and row[2] is None
            and row[3] is None
            and row[4] is None
            and row[5] is None
            and row[6] is not None
            and notnull_ok
        )
        record(
            14,
            "stubbed enrichment saves binary carpet row with room fields NULL and score",
            row_ok,
            f"row={row!r}, notnull_ok={notnull_ok}",
        )
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def run_regression_criterion():
    changed = (ROOT / "tests" / "test_carpet_other_areas_scoring.py").read_text()
    table = (ROOT / "tests" / "test_table_driven_scorer.py").read_text()
    acceptance_exists = (ROOT / "tests" / "acceptance_rental_carpet_binary.py").exists()
    record(
        15,
        "old carpet split/count tests updated and existing scorer regression aligned",
        "carpet_detected=True" in changed
        and "carpet_other_areas=True" not in changed
        and '"carpet": 0.25' in table
        and acceptance_exists,
    )


async def main():
    await run_detector_criteria()
    run_scorer_criteria()
    await run_wiring_criteria()
    run_regression_criterion()
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
