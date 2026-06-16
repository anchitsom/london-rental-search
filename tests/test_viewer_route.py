"""
Wave 2 Agent G: read-only /viewer route tests.

Project convention: plain async-style script, NOT pytest. Each test asserts and
prints PASS / FAIL; run via .venv/bin/python tests/test_viewer_route.py.

Fixture strategy: build a small SQLite db in tests/fixtures/viewer.db with the
Listing schema from homehunt.core.db. Override HOMEHUNT_DB to point the route
at that fixture, then exercise the FastAPI app via TestClient.

Tests:
  1. GET /viewer returns 200.
  2. Top-N listings render in the HTML response.
  3. ?sort=price_asc reorders.
  4. ?source=rightmove filters.
  5. ?region=Highbury filters.
  6. ?page=2 returns page 2.
  7. EPC-populated listing renders the expandable panel with SAP score, floor
     area sqm and sqft, glazing, walls energy eff, age band, heating cost.
  8. Listing with no EPC match renders a hairline "EPC unmatched" note.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_DB = os.path.join(FIXTURES_DIR, "viewer.db")


def _build_fixture_db(path: str) -> None:
    """Build a fresh fixture sqlite db with the Listing table and 25 rows."""
    if os.path.exists(path):
        os.remove(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Use SQLModel metadata to create the canonical schema. The Listing table
    # is registered once at import time; create it directly via the existing
    # metadata against a fresh engine bound to this fixture path.
    from sqlalchemy import create_engine
    from homehunt.core.db import Listing  # noqa: F401  (registers the table)
    from sqlmodel import SQLModel
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()

    # Now hand-insert rows via raw sqlite3 so we have full control over every
    # field, including the EPC-populated case and the EPC-missing case.
    conn = sqlite3.connect(path)
    cur = conn.cursor()

    base_ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()

    rows = []
    # 25 listings: 22 Rightmove + 3 Zoopla. 12 Highbury + 13 other regions.
    # Listing 0 has fully populated EPC fields.
    # Listing 1 has all EPC fields NULL.
    # Listings 2-24 are score-varied filler.
    for i in range(25):
        portal = "rightmove" if i % 5 != 4 else "zoopla"
        region = "Highbury, North London" if i < 12 else "Shoreditch, East London"
        score = 100 - i  # 100, 99, 98 ... 76
        price_pence = (1700 + i * 50) * 100
        size_sqft = 500 + i * 20
        bedrooms = 2 if i % 2 == 0 else 1
        epc_rating = "C" if i == 0 else (None if i == 1 else "D")

        # Listing 0 = fully populated EPC panel.
        # Listing 1 = fully NULL EPC for "unmatched" rendering.
        if i == 0:
            total_floor_area_sqm = 75.0
            current_eff = 72
            potential_eff = 84
            num_hab = 4
            num_heated = 4
            built_form = "Mid-Terrace"
            construction_age_band = "England and Wales: 1996-2002"
            windows_description = "Fully double glazed"
            windows_energy_eff = "Good"
            walls_description = "Cavity wall, as built, partial insulation"
            walls_energy_eff = "Average"
            roof_description = "Pitched, insulated"
            floor_level = "1st"
            mains_gas_flag = "Y"
            heating_cost_current = 540
            epc_lodgement_date = "2021-08-15"
            epc_inspection_date = "2021-08-12"
        elif i == 1:
            total_floor_area_sqm = None
            current_eff = None
            potential_eff = None
            num_hab = None
            num_heated = None
            built_form = None
            construction_age_band = None
            windows_description = None
            windows_energy_eff = None
            walls_description = None
            walls_energy_eff = None
            roof_description = None
            floor_level = None
            mains_gas_flag = None
            heating_cost_current = None
            epc_lodgement_date = None
            epc_inspection_date = None
        else:
            total_floor_area_sqm = 60.0 + i
            current_eff = 60
            potential_eff = 75
            num_hab = 3
            num_heated = 3
            built_form = "Mid-Terrace"
            construction_age_band = "England and Wales: 1983-1990"
            windows_description = "Fully double glazed"
            windows_energy_eff = "Good"
            walls_description = "Cavity wall"
            walls_energy_eff = "Average"
            roof_description = "Pitched, insulated"
            floor_level = "Ground"
            mains_gas_flag = "Y"
            heating_cost_current = 600
            epc_lodgement_date = "2020-01-01"
            epc_inspection_date = "2020-01-01"

        breakdown = {
            "price": 80,
            "size": 70,
            "bedrooms": 100 if bedrooms == 2 else 50,
            "carpet": 50,
            "epc": 60 if epc_rating == "C" else (40 if epc_rating == "D" else 0),
            "transport": 100,
            "outside": 60,
            "region_pref": 100 if "Highbury" in region else 100,
        }

        rows.append(
            (
                f"{portal}:1000{i:02d}",  # uid
                portal,
                f"1000{i:02d}",  # property_id
                f"https://www.rightmove.co.uk/properties/1000{i:02d}",
                f"{bedrooms} bed flat in fixture row {i}",  # title
                f"£{price_pence // 100}",  # price
                price_pence,
                bedrooms,
                f"Test Address {i}, London N5 1AA",  # address
                "N5 1AA",  # postcode
                "Highbury" if "Highbury" in region else "Shoreditch",  # area
                51.55 + i * 0.001,  # latitude
                -0.10 + i * 0.001,  # longitude
                "flat",  # property_type
                "furnished",  # furnished
                json.dumps(["balcony", "modern kitchen"]),  # features
                bool(i % 3 == 0),  # garden
                bool(i % 4 == 0),  # balcony
                "direct_http",  # extraction_method
                json.dumps(
                    [
                        f"https://media.rightmove.co.uk/x/1000{i:02d}/img1.jpg",
                        f"https://media.rightmove.co.uk/x/1000{i:02d}/img2.jpg",
                    ]
                ),  # images
                base_ts,  # first_seen
                base_ts,  # last_scraped
                1,  # scrape_count
                True,  # is_active
                "active",  # status
                size_sqft,
                epc_rating,
                False,  # carpet_detected
                0.8,  # carpet_confidence
                False,  # carpet_in_bedroom
                False,  # carpet_other_areas
                2,  # tfl_zone
                "Highbury and Islington",  # nearest_station
                28,  # commute_canary_wharf
                22,  # commute_whitechapel
                True,  # commute_pass_40min
                score,
                json.dumps(breakdown),  # score_breakdown
                region,
                total_floor_area_sqm,
                current_eff,
                potential_eff,
                num_hab,
                num_heated,
                built_form,
                "flat",  # property_type_epc
                construction_age_band,
                "rental-private",  # tenure_epc
                windows_description,
                windows_energy_eff,
                walls_description,
                walls_energy_eff,
                roof_description,
                floor_level,
                mains_gas_flag,
                heating_cost_current,
                epc_lodgement_date,
                epc_inspection_date,
                "structured",  # size_sqft_source
                0.95,  # size_sqft_confidence
            )
        )

    cur.executemany(
        """
        INSERT INTO listing (
            uid, portal, property_id, url, title, price, price_numeric,
            bedrooms, address, postcode, area, latitude, longitude,
            property_type, furnished, features, garden, balcony,
            extraction_method, images, first_seen, last_scraped,
            scrape_count, is_active, status,
            size_sqft, epc_rating, carpet_detected, carpet_confidence,
            carpet_in_bedroom, carpet_other_areas, tfl_zone, nearest_station,
            commute_canary_wharf, commute_whitechapel, commute_pass_40min,
            score, score_breakdown, region,
            total_floor_area_sqm, current_energy_efficiency,
            potential_energy_efficiency, number_habitable_rooms,
            number_heated_rooms, built_form, property_type_epc,
            construction_age_band, tenure_epc, windows_description,
            windows_energy_eff, walls_description, walls_energy_eff,
            roof_description, floor_level, mains_gas_flag,
            heating_cost_current, epc_lodgement_date, epc_inspection_date,
            size_sqft_source, size_sqft_confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def _client():
    """Build the FastAPI TestClient for homehunt.api with the fixture db."""
    from fastapi.testclient import TestClient
    from homehunt import api as api_module
    return TestClient(api_module.app)


_FIXTURE_BUILT = False


def _setup() -> "TestClient":
    global _FIXTURE_BUILT
    os.environ["HOMEHUNT_DB"] = FIXTURE_DB
    if not _FIXTURE_BUILT:
        _build_fixture_db(FIXTURE_DB)
        _FIXTURE_BUILT = True
    return _client()


def _assert(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL [{label}]: {detail}")
    print(f"  PASS [{label}]")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_viewer_returns_200():
    print("Test 1: GET /viewer returns 200")
    client = _setup()
    r = client.get("/viewer")
    _assert("status_200", r.status_code == 200, f"got {r.status_code}")
    _assert("html_doctype", "<!DOCTYPE html>" in r.text or "<!doctype html>" in r.text.lower(), "no doctype")


def test_top_listings_render():
    print("Test 2: top listings render")
    client = _setup()
    r = client.get("/viewer")
    _assert("status_200", r.status_code == 200)
    # Highest score (100) is uid rightmove:100000, with title "2 bed flat in fixture row 0"
    _assert(
        "top_listing_title_present",
        "fixture row 0" in r.text,
        "expected 'fixture row 0' in HTML body",
    )
    # Price string should appear
    _assert(
        "top_listing_price_present",
        "1,700" in r.text or "1700" in r.text,
        "expected 1700 price in HTML",
    )


def test_sort_price_asc():
    print("Test 3: ?sort=price_asc reorders by ascending price")
    client = _setup()
    r = client.get("/viewer?sort=price_asc")
    _assert("status_200", r.status_code == 200)
    body = r.text
    # Listing row 0 has the lowest price (£1700). Listing row 24 has £2900.
    pos_lowest = body.find("fixture row 0")
    pos_high = body.find("fixture row 10")
    _assert(
        "lowest_price_appears_first",
        pos_lowest != -1 and pos_high != -1 and pos_lowest < pos_high,
        f"order failed: row0={pos_lowest}, row10={pos_high}",
    )


def test_filter_by_source():
    print("Test 4: ?source=rightmove filters to rightmove only")
    client = _setup()
    r = client.get("/viewer?source=rightmove")
    _assert("status_200", r.status_code == 200)
    # Zoopla rows are i=4, 9, 14, 19, 24. Each carries portal=zoopla and uid prefix zoopla.
    _assert(
        "no_zoopla_uids",
        "zoopla:100004" not in r.text and "zoopla:100009" not in r.text,
        "zoopla rows leaked into rightmove filter",
    )
    _assert(
        "rightmove_uid_present",
        "rightmove:100000" in r.text,
        "rightmove rows missing",
    )


def test_filter_by_region():
    # Region is now an exact-match dropdown (the form submits a full distinct
    # region value, not free text), so query the exact fixture value.
    print("Test 5: ?region=Highbury, North London filters to Highbury")
    client = _setup()
    r = client.get("/viewer?region=Highbury%2C%20North%20London")
    _assert("status_200", r.status_code == 200)
    # Highbury rows are i=0..11. Shoreditch rows are i=12..24.
    _assert(
        "highbury_present",
        "fixture row 0" in r.text and "fixture row 11" in r.text,
        "highbury rows missing",
    )
    _assert(
        "shoreditch_absent",
        "fixture row 12" not in r.text and "fixture row 20" not in r.text,
        "shoreditch rows leaked through Highbury filter",
    )


def test_pagination_page_2():
    print("Test 6: ?page=2 returns page 2 (cards 21+)")
    client = _setup()
    # 25 listings, 20 per page. Page 1 = rows 0..19 (by score desc). Page 2 = 20..24.
    r1 = client.get("/viewer?page=1")
    r2 = client.get("/viewer?page=2")
    _assert("page1_status_200", r1.status_code == 200)
    _assert("page2_status_200", r2.status_code == 200)
    # Score-desc orders by score: row 0 highest, row 24 lowest. Page 1 = rows 0..19,
    # page 2 = rows 20..24.
    _assert(
        "page1_has_top",
        "fixture row 0" in r1.text,
        "page 1 missing top row",
    )
    _assert(
        "page1_no_bottom",
        "fixture row 24" not in r1.text,
        "page 1 leaked bottom row",
    )
    _assert(
        "page2_has_bottom",
        "fixture row 24" in r2.text,
        "page 2 missing bottom row",
    )
    _assert(
        "page2_no_top",
        "fixture row 0" not in r2.text,
        "page 2 leaked top row",
    )


def test_epc_panel_populated():
    print("Test 7: listing with EPC fields renders expandable panel")
    client = _setup()
    r = client.get("/viewer")
    _assert("status_200", r.status_code == 200)
    body = r.text
    # Required content: SAP score 72 and 84, floor area 75 sqm and ~807 sqft,
    # 'Fully double glazed', walls energy 'Average', age band '1996-2002',
    # heating cost 540.
    _assert("sap_current_72", "72" in body, "current SAP missing")
    _assert("sap_potential_84", "84" in body, "potential SAP missing")
    # 75 sqm and the converted sqft (807) -- 75 * 10.7639 ~= 807.3
    _assert("floor_area_sqm", "75" in body, "floor area sqm missing")
    _assert("floor_area_sqft", "807" in body, "floor area sqft missing")
    _assert("glazing", "Fully double glazed" in body, "glazing missing")
    _assert("walls_eff", "Average" in body, "walls energy eff missing")
    _assert("age_band", "1996-2002" in body, "age band missing")
    _assert("heating_cost", "540" in body, "heating cost missing")


def test_epc_unmatched_note():
    print("Test 8: listing without EPC match renders 'EPC unmatched' hairline")
    client = _setup()
    r = client.get("/viewer")
    _assert("status_200", r.status_code == 200)
    _assert(
        "epc_unmatched_text",
        "EPC unmatched" in r.text,
        "expected the literal 'EPC unmatched' marker",
    )


def test_main_card_action_buttons():
    print("Test 9: main-tab cards render accept, contact, and reject buttons")
    client = _setup()
    r = client.get("/viewer?tab=main")
    _assert("status_200", r.status_code == 200)
    body = r.text
    _assert(
        "contact_now_button",
        'data-action="contact-now"' in body,
        "expected the new contact-now button marker",
    )
    _assert(
        "accept_button",
        'data-action="accept"' in body,
        "expected the accept button marker",
    )
    _assert(
        "reject_button",
        'value="triage_reject"' in body,
        "expected the reject button (value=triage_reject)",
    )
    _assert(
        "contact_targets_contacted_state",
        'value="contacted" data-action="contact-now"' in body,
        "expected the contact button to transition new -> contacted directly (not via contact_queued)",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        ("viewer_returns_200", test_viewer_returns_200),
        ("top_listings_render", test_top_listings_render),
        ("sort_price_asc", test_sort_price_asc),
        ("filter_by_source", test_filter_by_source),
        ("filter_by_region", test_filter_by_region),
        ("pagination_page_2", test_pagination_page_2),
        ("epc_panel_populated", test_epc_panel_populated),
        ("epc_unmatched_note", test_epc_unmatched_note),
        ("main_card_action_buttons", test_main_card_action_buttons),
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
