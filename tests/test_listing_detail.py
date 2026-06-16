"""
Wave 2.6: per-listing detail page tests.

Project convention: plain script, NOT pytest. Each test asserts and prints
PASS / FAIL; run via .venv/bin/python tests/test_listing_detail.py.

Tests:
  1. GET /listing/{uid} returns 200 for an existing uid.
  2. GET /listing/{uid} returns 404 for an unknown uid.
  3. Detail page shows the score breakdown section when score_breakdown populated.
  4. Detail page shows the floorplan section when floorplan_data populated, with
     room rows visible.
  5. Detail page shows the EPC certificate section when EPC fields populated.
  6. Detail page shows the 'View on Rightmove' CTA at the bottom.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_DB = os.path.join(FIXTURES_DIR, "listing_detail.db")


def _build_fixture_db(path: str) -> None:
    """Build a tiny fixture DB with one fully-decorated listing."""
    if os.path.exists(path):
        os.remove(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    from sqlalchemy import create_engine
    from homehunt.core.db import Listing  # noqa: F401
    from sqlmodel import Session, SQLModel
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)

    # Add Wave 1.7 columns the canonical schema may be missing.
    import sqlite3
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(listing)")
    cols = {r[1] for r in cur.fetchall()}
    for col, ddl in [
        ("epc_graph_url", "VARCHAR"),
        ("epc_rating_source", "VARCHAR"),
        ("epc_rating_confidence", "VARCHAR"),
    ]:
        if col not in cols:
            cur.execute(f"ALTER TABLE listing ADD COLUMN {col} {ddl}")
    conn.commit()
    conn.close()

    floorplan_data = json.dumps({
        "total_size_sqft": 720,
        "total_size_sqm": 66.9,
        "rooms": [
            {"name": "BEDROOM", "size_sqft": 130, "dimensions": "12ft 6in x 10ft 4in"},
            {"name": "RECEPTION ROOM", "size_sqft": 220, "dimensions": "16ft 8in x 13ft 2in"},
            {"name": "KITCHEN", "size_sqft": 90, "dimensions": "10ft 0in x 9ft 0in"},
            {"name": "BATHROOM", "size_sqft": 50, "dimensions": "8ft 4in x 6ft 0in"},
        ],
        "has_balcony": True,
        "has_garden": False,
        "compass": "SE",
        "notes": "Flat with bright SE-facing reception.",
    })

    score_breakdown = json.dumps({
        "price": 100,
        "size": 80,
        "bedrooms": 100,
        "epc": 100,
        "transport": 75,
        "outside": 60,
        "carpet": 100,
    })

    from homehunt.core.models import ExtractionMethod, Portal, PropertyType
    from sqlmodel import Session
    listing = Listing(
        uid="rightmove:99999001",
        portal=Portal.RIGHTMOVE,
        property_id="99999001",
        url="https://www.rightmove.co.uk/properties/99999001",
        address="1 Test Street, London, N5 1AB",
        postcode="N5 1AB",
        title="Charming two-bed flat",
        price="£2,200 pcm",
        price_numeric=220000,
        bedrooms=2,
        size_sqft=720,
        property_type=PropertyType.FLAT,
        extraction_method=ExtractionMethod.DIRECT_HTTP,
        region="Highbury, North London",
        epc_rating="C",
        tfl_zone=2,
        commute_canary_wharf=27,
        commute_whitechapel=18,
        nearest_station="Highbury and Islington",
        tube_distance=4,
        score=82,
        score_breakdown=score_breakdown,
        floorplan_data=floorplan_data,
        carpet_in_bedroom=False,
        carpet_other_areas=False,
        garden=False,
        balcony=True,
        total_floor_area_sqm=66.9,
        current_energy_efficiency=72,
        potential_energy_efficiency=84,
        built_form="Mid-Terrace",
        construction_age_band="2003-2006",
        windows_description="Fully double glazed",
        windows_energy_eff="Good",
        walls_description="Cavity wall, as built, partial insulation (assumed)",
        walls_energy_eff="Average",
        first_seen=datetime.utcnow(),
        last_scraped=datetime.utcnow(),
        scrape_count=1,
        is_active=True,
        status="active",
    )
    with Session(engine) as s:
        s.add(listing)
        s.commit()
    engine.dispose()


_FIXTURE_BUILT = False


def _setup():
    """Point homehunt at the fixture DB and return a fresh TestClient."""
    global _FIXTURE_BUILT
    os.environ["HOMEHUNT_DB"] = FIXTURE_DB
    if not _FIXTURE_BUILT:
        _build_fixture_db(FIXTURE_DB)
        _FIXTURE_BUILT = True
    from fastapi.testclient import TestClient
    from homehunt import api as api_module
    return TestClient(api_module.app)


def _assert(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL [{label}]: {detail}")
    print(f"  PASS [{label}]")


def test_existing_uid_returns_200():
    print("Test 1: GET /listing/{uid} returns 200 for known uid")
    client = _setup()
    r = client.get("/listing/rightmove:99999001")
    _assert("status_200", r.status_code == 200, f"got {r.status_code}")


def test_unknown_uid_returns_404():
    print("Test 2: GET /listing/{uid} returns 404 for unknown uid")
    client = _setup()
    r = client.get("/listing/rightmove:absent")
    _assert("status_404", r.status_code == 404, f"got {r.status_code}")


def test_score_breakdown_section_renders():
    print("Test 3: score breakdown section renders with bars")
    client = _setup()
    r = client.get("/listing/rightmove:99999001")
    body = r.text
    _assert("section_present", "score breakdown" in body)
    _assert("bars_present", "score-bar__label" in body)
    _assert("price_label", ">price<" in body)
    _assert("transport_label", ">transport<" in body)


def test_floorplan_section_renders_with_rooms():
    print("Test 4: floorplan section renders rooms")
    client = _setup()
    r = client.get("/listing/rightmove:99999001")
    body = r.text
    _assert("section_present", "floorplan" in body.lower())
    _assert("total_size", "720 sqft" in body)
    _assert("compass_se", ">SE<" in body or "SE</span>" in body or "aspect: SE" in body)
    _assert("balcony_flag", ">balcony<" in body)
    _assert("room_bedroom", "BEDROOM" in body)
    _assert("room_dimensions", "12ft 6in x 10ft 4in" in body)


def test_epc_section_renders():
    print("Test 5: EPC certificate section renders when EPC populated")
    client = _setup()
    r = client.get("/listing/rightmove:99999001")
    body = r.text
    _assert("section_title", "EPC certificate" in body)
    _assert("sap_current", "72" in body, "SAP current")
    _assert("sap_potential", "84" in body, "SAP potential")
    _assert("built_form", "Mid-Terrace" in body)


def test_view_on_rightmove_cta():
    print("Test 6: 'View on Rightmove' CTA present in footer")
    client = _setup()
    r = client.get("/listing/rightmove:99999001")
    body = r.text
    _assert("cta_text", "View on Rightmove" in body)
    _assert("rightmove_link", "rightmove.co.uk/properties/99999001" in body)


def main():
    test_existing_uid_returns_200()
    test_unknown_uid_returns_404()
    test_score_breakdown_section_renders()
    test_floorplan_section_renders_with_rooms()
    test_epc_section_renders()
    test_view_on_rightmove_cta()
    print("\n" + "=" * 60)
    print("ALL 6 test groups passed.")


if __name__ == "__main__":
    main()
