"""
Freshness gate (Wave 2.5).

Verifies:
  1. _extract_uid_from_url returns the canonical rightmove:UID string.
  2. _load_fresh_uids returns the subset of UIDs whose last_scraped is
     within the horizon, and respects horizon_hours <= 0 as "no UIDs are fresh".
  3. _touch_existing bumps last_scraped, increments scrape_count, sets
     is_active and status without changing other columns.
  4. End-to-end: run_pipeline with a fresh UID skips _enrich_stage1 entirely
     and increments the touched counter; a stale UID goes through the full
     stage 1 + filter + stage 2 + filter + upsert flow.
  5. force_rescrape_all=True overrides the gate even when UIDs are within
     the horizon.

Run:
  .venv/bin/python tests/test_freshness_gate.py

Project convention: plain async script. No pytest. Mocks vision and scraper
modules to keep the test deterministic and offline.
"""

import asyncio
import os
import sys
import tempfile
import unittest.mock as mock
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_temp_db_env() -> str:
    """Point HOMEHUNT_DB at a fresh temp file, return the path."""
    fd, path = tempfile.mkstemp(prefix="homehunt-test-", suffix=".db")
    os.close(fd)
    os.remove(path)  # let create_tables build it
    os.environ["HOMEHUNT_DB"] = path
    return path


# Set the env var BEFORE importing run, because run.py snaps HOMEHUNT_DB at
# import time and run.py imports the Database class which uses that env var.
_DB_PATH = _make_temp_db_env()

import run as _run
from homehunt.core.db import Database, Listing
from homehunt.core.models import ExtractionMethod, Portal, PropertyType
from sqlmodel import Session, select


def _seed_listing(
    db: Database,
    uid: str,
    last_scraped: datetime,
    region: str = "Highbury, North London",
    is_active: bool = True,
    score: int = 75,
) -> None:
    """Insert a minimal Listing row at a chosen last_scraped timestamp."""
    listing = Listing(
        uid=uid,
        portal=Portal.RIGHTMOVE,
        property_id=uid.split(":")[-1],
        url=f"https://www.rightmove.co.uk/properties/{uid.split(':')[-1]}",
        address="Seed Address",
        postcode="N5 1AA",
        price="£2200 pcm",
        price_numeric=220000,
        bedrooms=2,
        size_sqft=600,
        property_type=PropertyType.FLAT,
        title="Seed",
        extraction_method=ExtractionMethod.DIRECT_HTTP,
        region=region,
        epc_rating="C",
        tfl_zone=2,
        score=score,
        first_seen=last_scraped,
        last_scraped=last_scraped,
        scrape_count=1,
        is_active=is_active,
        status="active" if is_active else "inactive",
    )
    with Session(db.engine) as s:
        s.add(listing)
        s.commit()


async def test_extract_uid_from_url():
    assert _run._extract_uid_from_url(
        "https://www.rightmove.co.uk/properties/170985353"
    ) == "rightmove:170985353"
    assert _run._extract_uid_from_url(
        "https://www.rightmove.co.uk/properties/88142079#/?channel=RES_LET"
    ) == "rightmove:88142079"
    assert _run._extract_uid_from_url("https://example.com/no-id") is None
    print("PASS: _extract_uid_from_url normalises rightmove URLs")


async def test_load_fresh_uids():
    """Two seeded rows: one fresh, one stale. Lookup with a 24h horizon."""
    db = Database()
    db.create_tables()

    now = datetime.utcnow()
    fresh_ts = now - timedelta(hours=2)
    stale_ts = now - timedelta(hours=48)

    _seed_listing(db, "rightmove:fresh1", last_scraped=fresh_ts)
    _seed_listing(db, "rightmove:stale1", last_scraped=stale_ts)
    _seed_listing(db, "rightmove:fresh2", last_scraped=fresh_ts)

    candidates = ["rightmove:fresh1", "rightmove:stale1", "rightmove:fresh2", "rightmove:absent"]
    fresh = _run._load_fresh_uids(db, candidates, horizon_hours=24)
    assert fresh == {"rightmove:fresh1", "rightmove:fresh2"}, fresh

    # horizon_hours = 0 means no UID is treated as fresh.
    none_fresh = _run._load_fresh_uids(db, candidates, horizon_hours=0)
    assert none_fresh == set()
    print("PASS: _load_fresh_uids returns only within-horizon UIDs")


async def test_touch_existing_bumps_fields():
    """Touch updates last_scraped, scrape_count, is_active without other columns."""
    db = Database()
    db.create_tables()

    initial_ts = datetime.utcnow() - timedelta(hours=10)
    _seed_listing(
        db, "rightmove:touch1",
        last_scraped=initial_ts,
        is_active=False,
        score=42,
    )

    ok = _run._touch_existing(db, "rightmove:touch1")
    assert ok is True

    with Session(db.engine) as s:
        row = s.exec(select(Listing).where(Listing.uid == "rightmove:touch1")).first()
    assert row is not None
    assert row.last_scraped > initial_ts, "last_scraped should advance"
    assert row.scrape_count == 2, f"expected scrape_count=2, got {row.scrape_count}"
    assert row.is_active is True, "is_active should be re-asserted on touch"
    assert row.status == "active"
    assert row.score == 42, "score should be untouched"

    # Touch on a missing UID returns False without error.
    assert _run._touch_existing(db, "rightmove:absent") is False
    print("PASS: _touch_existing bumps the right fields and is no-op for missing UIDs")


async def test_run_pipeline_skips_stage1_for_fresh_uids():
    """End-to-end: a fresh UID is touched only; a stale URL goes through stage 1."""
    db = Database()
    db.create_tables()

    # Seed a fresh row whose UID will appear in the discovered URLs.
    fresh_ts = datetime.utcnow() - timedelta(hours=2)
    _seed_listing(db, "rightmove:11111111", last_scraped=fresh_ts)

    stage1_calls: list[str] = []
    stage2_calls: list[str] = []
    upsert_calls: list[str] = []

    async def _fake_stage1(scraper, url):
        stage1_calls.append(url)
        # Build a usable enriched dict for the stale URL.
        return {
            "url": url,
            "property_id": "22222222",
            "uid": "rightmove:22222222",
            "raw": {
                "address": "Stale Address",
                "postcode": "N5 1AB",
                "price": "£2200 pcm",
                "bedrooms": 2,
                "title": "Stale",
                "size_sqft": 600,
                "images": [],
                "floorplan_urls": [],
                "raw_content": "",
                "latitude": 51.5485,
                "longitude": -0.1037,
                "garden": False,
                "balcony": False,
                "furnished": None,
                "bills_included": None,
                "council_tax_band": None,
                "let_available_date": None,
                "area": None,
                "agent_phone": None,
            },
            "tfl": {"tfl_zone": 2, "commute_to": {}, "tube_distance": None,
                    "cycle_canary_wharf": None, "commute_pass_40min": None,
                    "nearest_station": None},
            "epc": "C",
            "epc_multifield": {},
            "epc_graph_url": None,
            "epc_rating_source": "epc_api",
            "epc_rating_confidence": "epc_api_match",
            "nlp_size": None,
            "nlp_epc": None,
            "lat": 51.5485,
            "lng": -0.1037,
            "region": "Highbury, North London",
            "size_sqft": 600,
            "size_sqft_source": "structured",
            "size_sqft_confidence": 0.95,
            "carpet": None,
            "floorplan": None,
        }

    async def _fake_stage2(enriched):
        stage2_calls.append(enriched["uid"])
        enriched["carpet"] = {}
        enriched["floorplan"] = {}
        return enriched

    class _FakeScraper:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    async def _fake_discover(**kwargs):
        return [
            "https://www.rightmove.co.uk/properties/11111111",
            "https://www.rightmove.co.uk/properties/22222222",
        ]

    def _wrap_upsert(orig):
        def _inner(db, listing):
            upsert_calls.append(listing.uid)
            return orig(db, listing)
        return _inner

    profiles = [{
        "location": "Highbury",
        "region_id": "REGION^1",
        "min_price": 1600,
        "max_price": 2800,
        "min_bedrooms": 1,
        "max_bedrooms": 2,
        "radius": 0.5,
        "max_results": 2,
        "page_delay": 0.0,
        "scrape_delay": 0.0,
    }]

    with mock.patch("run._enrich_stage1", _fake_stage1), \
         mock.patch("run._enrich_stage2", _fake_stage2), \
         mock.patch("run.DirectHTTPScraper", _FakeScraper), \
         mock.patch("run.discover_properties", _fake_discover), \
         mock.patch("run._upsert", _wrap_upsert(_run._upsert)):
        result = await _run.run_pipeline(
            profiles_override=profiles,
            max_age_hours=24,
        )

    # The fresh URL must NOT trigger stage 1.
    assert len(stage1_calls) == 1, f"stage 1 should run only for the stale URL, got {stage1_calls}"
    assert stage1_calls[0].endswith("/22222222")
    # Stage 2 only on the stale survivor.
    assert len(stage2_calls) == 1
    assert stage2_calls[0] == "rightmove:22222222"
    # Upsert only the stale row.
    assert len(upsert_calls) == 1
    assert upsert_calls[0] == "rightmove:22222222"
    # Touched counter for the fresh URL.
    assert result["touched"] == 1, result
    assert result["saved"] == 1, result
    assert result["discovered"] == 2, result

    # Verify the fresh row's scrape_count was bumped.
    with Session(db.engine) as s:
        row = s.exec(select(Listing).where(Listing.uid == "rightmove:11111111")).first()
    assert row.scrape_count == 2, f"fresh row should be touched once, got scrape_count={row.scrape_count}"
    print("PASS: fresh UIDs touched only, stale URLs go through full pipeline")


async def test_force_rescrape_all_overrides_gate():
    """force_rescrape_all=True must run stage 1 on a UID that is within horizon."""
    db = Database()
    db.create_tables()

    fresh_ts = datetime.utcnow() - timedelta(hours=1)
    _seed_listing(db, "rightmove:33333333", last_scraped=fresh_ts)

    stage1_calls: list[str] = []

    async def _fake_stage1(scraper, url):
        stage1_calls.append(url)
        return None  # treat as failed enrichment so the rest of the path skips

    class _FakeScraper:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    async def _fake_discover(**kwargs):
        return ["https://www.rightmove.co.uk/properties/33333333"]

    profiles = [{
        "location": "Highbury", "region_id": "REGION^1",
        "min_price": 1600, "max_price": 2800,
        "min_bedrooms": 1, "max_bedrooms": 2,
        "radius": 0.5, "max_results": 1,
        "page_delay": 0.0, "scrape_delay": 0.0,
    }]

    with mock.patch("run._enrich_stage1", _fake_stage1), \
         mock.patch("run.DirectHTTPScraper", _FakeScraper), \
         mock.patch("run.discover_properties", _fake_discover):
        result = await _run.run_pipeline(
            profiles_override=profiles,
            force_rescrape_all=True,
        )

    assert len(stage1_calls) == 1, "force_rescrape_all should override the gate"
    assert result["touched"] == 0, result
    print("PASS: force_rescrape_all overrides freshness gate")


async def main():
    await test_extract_uid_from_url()
    await test_load_fresh_uids()
    await test_touch_existing_bumps_fields()
    await test_run_pipeline_skips_stage1_for_fresh_uids()
    await test_force_rescrape_all_overrides_gate()
    print("\nAll freshness-gate tests pass.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        # Best-effort cleanup of the temp DB.
        try:
            os.remove(_DB_PATH)
        except OSError:
            pass
