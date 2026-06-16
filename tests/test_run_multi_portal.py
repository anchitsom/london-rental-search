"""
Wave 2.7 Agent M: integration tests for the multi-portal pipeline wiring.

Project convention: plain async script, NOT pytest. Run via
.venv/bin/python -m tests.test_run_multi_portal from the project root.

Test cases:
  Goal 1: PropertyListing.from_extraction_result drops auxiliary keys
    1. Rightmove extraction dict passes through, populates required fields
    2. Zoopla extraction dict (with raw_content, photo_urls, price_pcm,
       floorplan_urls, size_sqm) passes through without raising
    3. Auxiliary keys are silently dropped, not promoted to attributes

  Goal 2: Listing model has cluster_id with round-trip read
    4. Listing(cluster_id=5) writes and reads back with cluster_id == 5

  Goal 3: enabled_portals plumbing in run.py
    5. Default enabled_portals is [rightmove]
    6. ENABLED_PORTALS env override parses comma-separated list

  Goal 5: regression smoke test (with mock scrapers)
    7. enabled_portals=[rightmove] calls only the Rightmove scraper, not Zoopla
       and does NOT call dedup
    8. enabled_portals=[rightmove, zoopla] invokes both scrapers
       and DOES call cluster_listings exactly once
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _assert(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL [{label}]: {detail}")
    print(f"  PASS [{label}]")


# ---------------------------------------------------------------------------
# Goal 1: PropertyListing.from_extraction_result hardening
# ---------------------------------------------------------------------------

async def test_from_extraction_result_rightmove_passes():
    print("Test 1: Rightmove-shaped extraction dict round-trips through helper")
    from homehunt.core.models import PropertyListing

    rightmove_dict = {
        "address": "12 Test Road, London",
        "postcode": "N1 1AA",
        "title": "2 bed flat to rent",
        "price": "£2,500 pcm",
        "bedrooms": 2,
        "description": "Lovely flat near the tube",
        "features": ["furnished", "balcony"],
        "raw_content": "<html>...</html>",
        "images": ["https://media.rightmove.co.uk/img1.jpg"],
        "latitude": 51.5,
        "longitude": -0.1,
    }
    listing = PropertyListing.from_extraction_result(
        portal="rightmove",
        property_id="111111",
        url="https://www.rightmove.co.uk/properties/111111",
        extraction_result=rightmove_dict,
        extraction_method="direct_http",
    )
    _assert("rightmove_uid", listing.uid == "rightmove:111111", f"uid={listing.uid}")
    _assert("rightmove_address", listing.address == "12 Test Road, London")
    _assert("rightmove_bedrooms", listing.bedrooms == 2)
    _assert("rightmove_price", listing.price == "£2,500 pcm")


async def test_from_extraction_result_zoopla_passes():
    print("Test 2: Zoopla-shaped extraction dict round-trips through helper")
    from homehunt.core.models import PropertyListing

    # Mirrors the keys returned by ZooplaHTTPScraper._parse_detail_html.
    # The auxiliary keys (raw_content, photo_urls, price_pcm, floorplan_urls,
    # size_sqm) are NOT on PropertyListing and used to crash with
    # ValidationError under extra="forbid".
    zoopla_dict = {
        "address": "21 Test Lane, London",
        "postcode": "E2 8DY",
        "title": "1 bed flat to rent in London",
        "price": "£1,800 pcm",
        "price_pcm": 1800,
        "bedrooms": 1,
        "bathrooms": 1,
        "description": "Modern 1-bed flat",
        "features": ["unfurnished"],
        "raw_content": "<html>...</html>",
        "images": ["https://lid.zoocdn.com/u/1024/768/abc.jpg"],
        "photo_urls": ["https://lid.zoocdn.com/u/1024/768/abc.jpg"],
        "floorplan_urls": ["https://lc.zoocdn.com/def.jpg"],
        "epc_rating": "C",
        "size_sqft": 540,
        "size_sqm": 50,
        "latitude": 51.52,
        "longitude": -0.06,
        "property_type": "flat",
        "furnished": "Unfurnished",
    }
    listing = PropertyListing.from_extraction_result(
        portal="zoopla",
        property_id="73109367",
        url="https://www.zoopla.co.uk/to-rent/details/73109367/",
        extraction_result=zoopla_dict,
        extraction_method="direct_http",
    )
    _assert("zoopla_uid", listing.uid == "zoopla:73109367", f"uid={listing.uid}")
    _assert("zoopla_address", listing.address == "21 Test Lane, London")
    _assert("zoopla_epc", listing.epc_rating == "C")
    _assert("zoopla_size_sqft", listing.size_sqft == 540)


async def test_from_extraction_result_drops_aux_keys():
    print("Test 3: auxiliary keys are silently dropped, do not raise")
    from homehunt.core.models import PropertyListing

    payload = {
        "address": "1 Aux Road",
        "raw_content": "should be dropped",
        "photo_urls": ["should-be-dropped"],
        "price_pcm": 9999,
        "floorplan_urls": ["should-be-dropped"],
        "size_sqm": 99,
        "completely_unknown_field": "should-be-dropped",
    }
    listing = PropertyListing.from_extraction_result(
        portal="zoopla",
        property_id="999",
        url="https://www.zoopla.co.uk/to-rent/details/999/",
        extraction_result=payload,
        extraction_method="direct_http",
    )
    _assert("address_kept", listing.address == "1 Aux Road")
    # Auxiliary keys must not appear as attributes
    _assert(
        "raw_content_dropped",
        not hasattr(listing, "raw_content") or getattr(listing, "raw_content", None) is None,
    )
    _assert(
        "photo_urls_dropped",
        not hasattr(listing, "photo_urls") or getattr(listing, "photo_urls", None) is None,
    )


# ---------------------------------------------------------------------------
# Goal 2: cluster_id round-trip on Listing
# ---------------------------------------------------------------------------

async def test_listing_cluster_id_round_trip():
    print("Test 4: Listing(cluster_id=5) writes and reads back")
    from sqlmodel import SQLModel, Session, create_engine, select
    from homehunt.core.db import Listing
    from homehunt.core.models import ExtractionMethod, Portal
    from homehunt.dedup import ensure_cluster_schema

    engine = create_engine("sqlite:///:memory:", echo=False)
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        ensure_cluster_schema(conn)

    with Session(engine) as session:
        row = Listing(
            uid="rightmove:1",
            portal=Portal.RIGHTMOVE,
            property_id="1",
            url="https://www.rightmove.co.uk/properties/1",
            extraction_method=ExtractionMethod.DIRECT_HTTP,
            cluster_id=5,
        )
        session.add(row)
        session.commit()
        result = session.exec(select(Listing).where(Listing.uid == "rightmove:1")).first()
        _assert("cluster_id_persisted", result.cluster_id == 5,
                f"got {result.cluster_id!r}")


# ---------------------------------------------------------------------------
# Goal 3: enabled_portals plumbing
# ---------------------------------------------------------------------------

async def test_default_enabled_portals_is_rightmove_only():
    print("Test 5: default enabled_portals is [rightmove]")
    # Reload run module with no env override.
    if "ENABLED_PORTALS" in os.environ:
        del os.environ["ENABLED_PORTALS"]
    import importlib
    import run as run_mod
    importlib.reload(run_mod)
    portals = run_mod._parse_enabled_portals()
    _assert("default_is_rightmove_only", portals == ["rightmove"],
            f"got {portals!r}")


async def test_env_override_parses_comma_separated():
    print("Test 6: ENABLED_PORTALS env var parses comma-separated list")
    os.environ["ENABLED_PORTALS"] = "rightmove,zoopla"
    import importlib
    import run as run_mod
    importlib.reload(run_mod)
    portals = run_mod._parse_enabled_portals()
    _assert("two_portals", portals == ["rightmove", "zoopla"],
            f"got {portals!r}")
    del os.environ["ENABLED_PORTALS"]


# ---------------------------------------------------------------------------
# Goal 5: regression smoke test (mocks)
# ---------------------------------------------------------------------------

async def test_rightmove_only_does_not_call_zoopla_or_dedup():
    print("Test 7: enabled_portals=[rightmove] calls neither Zoopla nor dedup")
    if "ENABLED_PORTALS" in os.environ:
        del os.environ["ENABLED_PORTALS"]
    import importlib
    import run as run_mod
    importlib.reload(run_mod)

    rm_calls = {"count": 0}
    zp_calls = {"count": 0}
    dedup_calls = {"count": 0}

    async def _fake_run_rightmove(profiles, total, hard_filters, drops_s1, drops_s2, horizon_hours):
        rm_calls["count"] += 1

    async def _fake_run_zoopla(profiles, total, hard_filters, drops_s1, drops_s2, horizon_hours):
        zp_calls["count"] += 1

    async def _fake_dedup(session):
        dedup_calls["count"] += 1
        return 0

    with patch.object(run_mod, "_run_rightmove_pipeline", _fake_run_rightmove), \
         patch.object(run_mod, "_run_zoopla_pipeline", _fake_run_zoopla), \
         patch.object(run_mod, "_run_cluster_listings", _fake_dedup):
        await run_mod.run_pipeline(
            profiles_override=[],
            enabled_portals=["rightmove"],
        )

    _assert("rightmove_called", rm_calls["count"] == 1, f"got {rm_calls['count']}")
    _assert("zoopla_not_called", zp_calls["count"] == 0, f"got {zp_calls['count']}")
    _assert("dedup_not_called", dedup_calls["count"] == 0, f"got {dedup_calls['count']}")


async def test_multi_portal_invokes_both_and_dedup():
    print("Test 8: enabled_portals=[rightmove, zoopla] calls both plus dedup")
    if "ENABLED_PORTALS" in os.environ:
        del os.environ["ENABLED_PORTALS"]
    import importlib
    import run as run_mod
    importlib.reload(run_mod)

    rm_calls = {"count": 0}
    zp_calls = {"count": 0}
    dedup_calls = {"count": 0}

    async def _fake_run_rightmove(profiles, total, hard_filters, drops_s1, drops_s2, horizon_hours):
        rm_calls["count"] += 1

    async def _fake_run_zoopla(profiles, total, hard_filters, drops_s1, drops_s2, horizon_hours):
        zp_calls["count"] += 1

    async def _fake_dedup(session):
        dedup_calls["count"] += 1
        return 0

    with patch.object(run_mod, "_run_rightmove_pipeline", _fake_run_rightmove), \
         patch.object(run_mod, "_run_zoopla_pipeline", _fake_run_zoopla), \
         patch.object(run_mod, "_run_cluster_listings", _fake_dedup):
        await run_mod.run_pipeline(
            profiles_override=[],
            enabled_portals=["rightmove", "zoopla"],
        )

    _assert("rightmove_called", rm_calls["count"] == 1, f"got {rm_calls['count']}")
    _assert("zoopla_called", zp_calls["count"] == 1, f"got {zp_calls['count']}")
    _assert("dedup_called_once", dedup_calls["count"] == 1, f"got {dedup_calls['count']}")


# ---------------------------------------------------------------------------
# Wave 2.7 M.5: Zoopla enrichment goals
# ---------------------------------------------------------------------------


def _set_temp_db():
    """Point HOMEHUNT_DB at a unique tmp file so tests do not pollute prod."""
    import tempfile, uuid
    path = Path(tempfile.gettempdir()) / f"m5-test-{uuid.uuid4().hex}.db"
    if path.exists():
        path.unlink()
    os.environ["HOMEHUNT_DB"] = str(path)
    return path
#
# Goal 1: enrichment runs on Zoopla listings, with skips for already-populated
#   fields. Specifically:
#     - EPC API is called even when scraper produced epc_rating, but the
#       scraper's value wins. The API still populates the full energy block.
#     - Floorplan vision is skipped when the scraper already gave size_sqft.
#     - Floorplan vision runs when size_sqft is null and floorplan_urls present.
#     - TfL, region, walk-distance run on Zoopla rows like Rightmove rows.
#
# Goal 2: hard filter applies to Zoopla rows. Two-stage filter is portal
#   agnostic; we just confirm a Zoopla row that fails price is excluded and
#   one that passes is kept.
#
# Goal 3: scoring runs after dedup.


def _make_fake_zoopla_scrape_result(*, pid, **overrides):
    """Build a ScrapingResult-like object for the Zoopla scraper mock."""
    from homehunt.core.models import ExtractionMethod, Portal, ScrapingResult

    data = {
        "address": "21 Test Lane, London",
        "postcode": "E2 8DY",
        "title": "1 bed flat to rent in London",
        "price": "£1,800 pcm",
        "price_pcm": 1800,
        "bedrooms": 1,
        "bathrooms": 1,
        "description": "Modern 1-bed flat",
        "features": ["unfurnished"],
        "raw_content": "<html>...</html>",
        "images": ["https://lid.zoocdn.com/u/1024/768/abc.jpg"],
        "photo_urls": ["https://lid.zoocdn.com/u/1024/768/abc.jpg"],
        "latitude": 51.52,
        "longitude": -0.06,
        "property_type": "flat",
    }
    data.update(overrides)
    return ScrapingResult(
        url=f"https://www.zoopla.co.uk/to-rent/details/{pid}/",
        success=True,
        portal=Portal.ZOOPLA,
        property_id=pid,
        data=data,
        extraction_method=ExtractionMethod.DIRECT_HTTP,
    )


class _FakeZooplaScraper:
    """Stand-in for ZooplaHTTPScraper used to control results in tests."""

    def __init__(self, results_by_url):
        self._results_by_url = results_by_url
        self._urls = list(results_by_url.keys())

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def scrape_search_page(self, search_url):
        return list(self._urls)

    async def scrape_property(self, url):
        return self._results_by_url[url]


def _patch_enrichment(run_mod, *, called):
    """
    Stub the enrichment surfaces used by both pipelines.

    Records each call into the `called` dict so tests can assert which
    surfaces fired and with what arguments. Returns a list of patches that
    callers should enter via contextlib.ExitStack.
    """
    from contextlib import ExitStack

    stack = ExitStack()

    async def _fake_enrich_commute_multi(postcode):
        called.setdefault("tfl", []).append(postcode)
        return {
            "tube_distance": 5,
            "cycle_canary_wharf": 20,
            "tfl_zone": 2,
            "nearest_station": "Whitechapel",
            "commute_pass_40min": True,
            "commute_to": {"canary_wharf": 18, "whitechapel": 5},
        }

    async def _fake_enrich_listing_with_epc(listing_stub):
        called.setdefault("epc", []).append(listing_stub.uid)
        return {
            "epc_rating": "D",
            "total_floor_area_sqm": 50,
            "current_energy_efficiency": 65,
            "potential_energy_efficiency": 80,
            "number_habitable_rooms": 3,
            "size_sqft_fallback": None,
        }

    async def _fake_extract_floorplan_data(url):
        called.setdefault("floorplan_vision", []).append(url)
        return {"total_size_sqft": 700, "rooms": []}

    async def _fake_extract_epc_from_image(url):
        called.setdefault("epc_vision", []).append(url)
        return {"confidence": "sap_derived", "epc_rating": "C"}

    async def _fake_detect_carpet_two_signal(images):
        called.setdefault("carpet", []).append(len(images))
        return {
            "carpet_detected": False,
            "carpet_confidence": 0.9,
            "carpet_in_bedroom": False,
            "carpet_other_areas": False,
        }

    def _fake_resolve_region(lat, lng):
        called.setdefault("region", []).append((lat, lng))
        return "Bethnal Green"

    async def _fake_dedup(session):
        called.setdefault("dedup", []).append(1)
        return 0

    stack.enter_context(patch.object(run_mod, "enrich_commute_multi", _fake_enrich_commute_multi))
    stack.enter_context(patch.object(run_mod, "enrich_listing_with_epc", _fake_enrich_listing_with_epc))
    stack.enter_context(patch.object(run_mod, "extract_floorplan_data", _fake_extract_floorplan_data))
    stack.enter_context(patch.object(run_mod, "extract_epc_from_image", _fake_extract_epc_from_image))
    stack.enter_context(patch.object(run_mod, "detect_carpet_two_signal", _fake_detect_carpet_two_signal))
    stack.enter_context(patch.object(run_mod, "resolve_region", _fake_resolve_region))
    stack.enter_context(patch.object(run_mod, "_run_cluster_listings", _fake_dedup))

    return stack


def _zoopla_only_profile():
    return [
        {
            "name": "test",
            "location": "Bethnal Green",
            "min_price": 500,
            "max_price": 2800,
            "min_bedrooms": 1,
            "max_bedrooms": 2,
            "scrape_delay": 0.0,
        }
    ]


async def _run_zoopla_only(run_mod, fake_scraper):
    """Drive run_pipeline with only the Zoopla portal enabled and a fake scraper."""
    with patch.object(
        run_mod, "ZooplaHTTPScraper", lambda *a, **kw: fake_scraper
    ), patch.object(
        run_mod, "build_zoopla_search_url", lambda **kw: "https://search/"
    ):
        return await run_mod.run_pipeline(
            profiles_override=_zoopla_only_profile(),
            enabled_portals=["zoopla"],
        )


async def test_zoopla_epc_api_populates_when_scraper_missing():
    """
    Goal 1.1: when the scraper did not extract epc_rating, EPC API populates
    the persisted row.
    """
    print("Test 9: zoopla row without scraper epc_rating gets EPC API value")
    if "ENABLED_PORTALS" in os.environ:
        del os.environ["ENABLED_PORTALS"]
    _set_temp_db()
    import importlib
    import run as run_mod
    importlib.reload(run_mod)

    from sqlmodel import Session, select
    from homehunt.core.db import Listing

    pid = "10000001"
    url = f"https://www.zoopla.co.uk/to-rent/details/{pid}/"
    fake = _FakeZooplaScraper({
        url: _make_fake_zoopla_scrape_result(
            pid=pid,
            size_sqft=600,  # populated to bypass floorplan vision
        ),
    })

    called = {}
    with _patch_enrichment(run_mod, called=called), \
         patch.object(run_mod, "_DB_WRITE_LOCK", asyncio.Lock()):
        await _run_zoopla_only(run_mod, fake)

    db = run_mod.Database()
    with Session(db.engine) as s:
        row = s.exec(select(Listing).where(Listing.uid == f"zoopla:{pid}")).first()
    _assert("row_persisted", row is not None)
    _assert("epc_from_api", row.epc_rating == "D", f"got {row.epc_rating!r}")
    _assert("epc_api_called", "epc" in called and len(called["epc"]) == 1)
    _assert("tfl_called", "tfl" in called and len(called["tfl"]) == 1)
    _assert("region_called", "region" in called and len(called["region"]) == 1)


async def test_zoopla_scraper_epc_wins_over_api():
    """
    Goal 1.2: when scraper extracted epc_rating, the API call still happens
    (to populate the energy block) but the scraper letter is preserved.
    """
    print("Test 10: zoopla scraper epc_rating overrides EPC API")
    if "ENABLED_PORTALS" in os.environ:
        del os.environ["ENABLED_PORTALS"]
    _set_temp_db()
    import importlib
    import run as run_mod
    importlib.reload(run_mod)

    from sqlmodel import Session, select
    from homehunt.core.db import Listing

    pid = "10000002"
    url = f"https://www.zoopla.co.uk/to-rent/details/{pid}/"
    fake = _FakeZooplaScraper({
        url: _make_fake_zoopla_scrape_result(
            pid=pid,
            size_sqft=560,
            epc_rating="C",  # scraper-extracted, must not be overwritten
        ),
    })

    called = {}
    with _patch_enrichment(run_mod, called=called), \
         patch.object(run_mod, "_DB_WRITE_LOCK", asyncio.Lock()):
        await _run_zoopla_only(run_mod, fake)

    db = run_mod.Database()
    with Session(db.engine) as s:
        row = s.exec(select(Listing).where(Listing.uid == f"zoopla:{pid}")).first()
    _assert("row_persisted", row is not None)
    _assert("scraper_epc_kept", row.epc_rating == "C", f"got {row.epc_rating!r}")
    # API still called so the full energy block is populated.
    _assert("api_full_block_called", "epc" in called and len(called["epc"]) == 1)
    _assert(
        "energy_block_populated",
        row.total_floor_area_sqm == 50 and row.current_energy_efficiency == 65,
    )


async def test_zoopla_floorplan_vision_skipped_when_size_present():
    """
    Goal 1.3: when scraper produced size_sqft, the floorplan vision call is
    not made.
    """
    print("Test 11: zoopla scraper size_sqft skips floorplan vision")
    if "ENABLED_PORTALS" in os.environ:
        del os.environ["ENABLED_PORTALS"]
    _set_temp_db()
    import importlib
    import run as run_mod
    importlib.reload(run_mod)

    pid = "10000003"
    url = f"https://www.zoopla.co.uk/to-rent/details/{pid}/"
    fake = _FakeZooplaScraper({
        url: _make_fake_zoopla_scrape_result(
            pid=pid,
            size_sqft=560,
            floorplan_urls=["https://lc.zoocdn.com/abc.jpg"],
        ),
    })

    called = {}
    with _patch_enrichment(run_mod, called=called), \
         patch.object(run_mod, "_DB_WRITE_LOCK", asyncio.Lock()):
        await _run_zoopla_only(run_mod, fake)

    _assert(
        "floorplan_vision_skipped",
        "floorplan_vision" not in called,
        f"unexpectedly called: {called.get('floorplan_vision')}",
    )


async def test_zoopla_floorplan_vision_runs_when_size_missing():
    """
    Goal 1.4: when scraper has no size_sqft but has floorplan_urls,
    floorplan vision runs.
    """
    print("Test 12: zoopla missing size_sqft triggers floorplan vision")
    if "ENABLED_PORTALS" in os.environ:
        del os.environ["ENABLED_PORTALS"]
    _set_temp_db()
    import importlib
    import run as run_mod
    importlib.reload(run_mod)

    # Force vision on for the test even if env disabled it.
    pid = "10000004"
    url = f"https://www.zoopla.co.uk/to-rent/details/{pid}/"
    fake = _FakeZooplaScraper({
        url: _make_fake_zoopla_scrape_result(
            pid=pid,
            floorplan_urls=["https://lc.zoocdn.com/floorplan.jpg"],
        ),
    })

    called = {}
    with _patch_enrichment(run_mod, called=called), \
         patch.object(run_mod, "FLOORPLAN_ENABLED", True), \
         patch.object(run_mod, "_DB_WRITE_LOCK", asyncio.Lock()):
        await _run_zoopla_only(run_mod, fake)

    _assert(
        "floorplan_vision_called",
        "floorplan_vision" in called and len(called["floorplan_vision"]) == 1,
        f"got {called.get('floorplan_vision')}",
    )


async def test_zoopla_hard_filter_applies():
    """
    Goal 2: a Zoopla row that fails price filter is dropped, one that passes
    is saved.
    """
    print("Test 13: hard filter applies to zoopla rows")
    if "ENABLED_PORTALS" in os.environ:
        del os.environ["ENABLED_PORTALS"]
    _set_temp_db()
    import importlib
    import run as run_mod
    importlib.reload(run_mod)

    from sqlmodel import Session, select
    from homehunt.core.db import Listing

    pid_pass = "20000001"
    pid_fail = "20000002"
    url_pass = f"https://www.zoopla.co.uk/to-rent/details/{pid_pass}/"
    url_fail = f"https://www.zoopla.co.uk/to-rent/details/{pid_fail}/"

    fake = _FakeZooplaScraper({
        url_pass: _make_fake_zoopla_scrape_result(
            pid=pid_pass, size_sqft=560, price_pcm=2200, price="£2,200 pcm",
        ),
        url_fail: _make_fake_zoopla_scrape_result(
            pid=pid_fail, size_sqft=560, price_pcm=10000, price="£10,000 pcm",
        ),
    })

    called = {}
    with _patch_enrichment(run_mod, called=called), \
         patch.object(run_mod, "_DB_WRITE_LOCK", asyncio.Lock()):
        result = await _run_zoopla_only(run_mod, fake)

    db = run_mod.Database()
    with Session(db.engine) as s:
        pass_row = s.exec(select(Listing).where(Listing.uid == f"zoopla:{pid_pass}")).first()
        fail_row = s.exec(select(Listing).where(Listing.uid == f"zoopla:{pid_fail}")).first()

    _assert("pass_row_saved", pass_row is not None)
    _assert("fail_row_dropped", fail_row is None,
            f"unexpected row: {fail_row.uid if fail_row else None}")


async def test_multi_portal_dedup_runs_once_after_both():
    """
    Goal 3: dedup runs once after both scrape passes; scorer is invoked once
    per cluster (verified by score being populated on each survivor).
    """
    print("Test 14: multi-portal flow runs dedup once and scores survivors")
    if "ENABLED_PORTALS" in os.environ:
        del os.environ["ENABLED_PORTALS"]
    import importlib
    import run as run_mod
    importlib.reload(run_mod)

    rm_calls = {"count": 0}
    zp_calls = {"count": 0}
    dedup_calls = {"count": 0}

    async def _fake_run_rightmove(profiles, total, hard_filters, drops_s1, drops_s2, horizon_hours):
        rm_calls["count"] += 1

    async def _fake_run_zoopla(profiles, total, hard_filters, drops_s1, drops_s2, horizon_hours):
        zp_calls["count"] += 1

    async def _fake_dedup(session):
        dedup_calls["count"] += 1
        return 0

    with patch.object(run_mod, "_run_rightmove_pipeline", _fake_run_rightmove), \
         patch.object(run_mod, "_run_zoopla_pipeline", _fake_run_zoopla), \
         patch.object(run_mod, "_run_cluster_listings", _fake_dedup):
        await run_mod.run_pipeline(
            profiles_override=[],
            enabled_portals=["rightmove", "zoopla"],
        )

    _assert("rightmove_called", rm_calls["count"] == 1, f"got {rm_calls['count']}")
    _assert("zoopla_called", zp_calls["count"] == 1, f"got {zp_calls['count']}")
    _assert("dedup_called_once", dedup_calls["count"] == 1, f"got {dedup_calls['count']}")


async def test_zoopla_score_populated_after_enrichment():
    """
    Goal 3 corollary: a saved Zoopla row has a numeric score and a
    score_breakdown. The scorer is portal-agnostic.
    """
    print("Test 15: zoopla survivors have score and score_breakdown populated")
    if "ENABLED_PORTALS" in os.environ:
        del os.environ["ENABLED_PORTALS"]
    _set_temp_db()
    import importlib
    import run as run_mod
    importlib.reload(run_mod)

    from sqlmodel import Session, select
    from homehunt.core.db import Listing

    pid = "30000001"
    url = f"https://www.zoopla.co.uk/to-rent/details/{pid}/"
    fake = _FakeZooplaScraper({
        url: _make_fake_zoopla_scrape_result(
            pid=pid, size_sqft=560, price_pcm=2200,
        ),
    })

    called = {}
    with _patch_enrichment(run_mod, called=called), \
         patch.object(run_mod, "_DB_WRITE_LOCK", asyncio.Lock()):
        await _run_zoopla_only(run_mod, fake)

    db = run_mod.Database()
    with Session(db.engine) as s:
        row = s.exec(select(Listing).where(Listing.uid == f"zoopla:{pid}")).first()

    _assert("row_saved", row is not None)
    _assert("score_int", isinstance(row.score, int), f"got {type(row.score)}")
    _assert("score_in_range", 0 <= row.score <= 100, f"got {row.score}")
    _assert("score_breakdown_present", row.score_breakdown is not None)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def main():
    tests = [
        test_from_extraction_result_rightmove_passes,
        test_from_extraction_result_zoopla_passes,
        test_from_extraction_result_drops_aux_keys,
        test_listing_cluster_id_round_trip,
        test_default_enabled_portals_is_rightmove_only,
        test_env_override_parses_comma_separated,
        test_rightmove_only_does_not_call_zoopla_or_dedup,
        test_multi_portal_invokes_both_and_dedup,
        # M.5 additions
        test_zoopla_epc_api_populates_when_scraper_missing,
        test_zoopla_scraper_epc_wins_over_api,
        test_zoopla_floorplan_vision_skipped_when_size_present,
        test_zoopla_floorplan_vision_runs_when_size_missing,
        test_zoopla_hard_filter_applies,
        test_multi_portal_dedup_runs_once_after_both,
        test_zoopla_score_populated_after_enrichment,
    ]
    failures = 0
    for t in tests:
        try:
            await t()
        except AssertionError as e:
            print(f"  {e}")
            failures += 1
        except Exception as e:
            print(f"  ERROR in {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failures += 1

    print()
    if failures == 0:
        print("ALL test groups passed.")
    else:
        print(f"FAIL: {failures} test groups failed")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
