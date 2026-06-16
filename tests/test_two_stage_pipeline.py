"""
Two-stage enrichment with double hard filter (Wave 2 Agent F follow-up).

Verifies:
  1. _build_partial_listing produces a Listing the hard filter accepts.
  2. The stage-1 hard filter drops listings on cheap signals (price,
     bedrooms, region, EPC, TfL zone, stage-1 size).
  3. Vision-only stage-2 enrichment runs ONLY on stage-1 survivors.
  4. The re-applied hard filter drops listings whose stage-2 vision
     reveals a size_sqft below the floor.
  5. The re-applied hard filter passes through listings whose vision
     lifts size_sqft into the survivable range.

Run:
  .venv/bin/python tests/test_two_stage_pipeline.py

Project convention: plain async script. No pytest. Mocks vision modules
to avoid the Ollama dependency and keep the test deterministic.
"""

import asyncio
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run as _run
from homehunt.filters import apply_hard_filters

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "filter-scoring-config.yaml",
)


def _make_stage1_enriched(uid: str, **kwargs) -> dict:
    """Build a stage-1 enriched dict that _build_partial_listing accepts."""
    raw = {
        "address": kwargs.get("address", "Test Address"),
        "postcode": kwargs.get("postcode", "N5 1AB"),
        "price": kwargs.get("price_str", "£2,200 pcm"),
        "bedrooms": kwargs.get("bedrooms", 2),
        "title": "Test Listing",
        "size_sqft": kwargs.get("structured_size"),
        "images": kwargs.get("images", ["https://test.example/img.jpg"]),
        "floorplan_urls": kwargs.get("floorplan_urls", []),
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
    }
    return {
        "url": f"https://www.rightmove.co.uk/properties/{uid}",
        "property_id": uid,
        "uid": f"rightmove:{uid}",
        "raw": raw,
        "tfl": {
            "tfl_zone": kwargs.get("tfl_zone", 2),
            "commute_to": {},
            "tube_distance": None,
            "cycle_canary_wharf": None,
            "commute_pass_40min": None,
            "nearest_station": None,
        },
        "epc": kwargs.get("epc", "C"),
        "epc_multifield": {},
        "epc_graph_url": kwargs.get("epc_graph_url"),
        "epc_rating_source": "epc_api",
        "epc_rating_confidence": "epc_api_match",
        "nlp_size": kwargs.get("nlp_size"),
        "nlp_epc": None,
        "lat": 51.5485,
        "lng": -0.1037,
        "region": kwargs.get("region", "Highbury, North London"),
        "size_sqft": kwargs.get("stage1_size"),
        "size_sqft_source": "structured" if kwargs.get("stage1_size") else None,
        "size_sqft_confidence": 0.95 if kwargs.get("stage1_size") else None,
        "carpet": None,
        "floorplan": None,
    }


async def test_partial_listing_passes_filter_when_signals_clean():
    """Sanity: a partial Listing built from clean stage-1 data survives the filter."""
    hf = _run.load_hard_filters(CONFIG_PATH)
    enriched = _make_stage1_enriched("01", bedrooms=2, stage1_size=600)
    partial = _run._build_partial_listing(enriched)
    survivors, drops = apply_hard_filters([partial], hf)
    assert len(survivors) == 1, f"expected 1 survivor, got {len(survivors)} ({drops})"
    print("PASS: clean stage-1 partial passes the filter")


async def test_stage1_filter_drops_obvious_misses():
    """The stage-1 filter eliminates rows with bad cheap signals."""
    hf = _run.load_hard_filters(CONFIG_PATH)

    rejected_bedrooms = _run._build_partial_listing(
        _make_stage1_enriched("11", bedrooms=4, stage1_size=600)
    )
    rejected_zone = _run._build_partial_listing(
        _make_stage1_enriched("12", tfl_zone=5, stage1_size=600)
    )
    # 2026-05-08: region demoted from hard filter to soft signal. A listing
    # with region='Other' now SURVIVES the filter and is penalised on the
    # region_tier scoring component instead. The kept stub remains as a
    # confirmation that the soft-signal default is in force.
    kept_other_region = _run._build_partial_listing(
        _make_stage1_enriched("13", region="Other", stage1_size=600)
    )
    survivor = _run._build_partial_listing(
        _make_stage1_enriched("14", bedrooms=2, stage1_size=600)
    )

    survivors, drops = apply_hard_filters(
        [rejected_bedrooms, rejected_zone, kept_other_region, survivor],
        hf,
    )
    assert len(survivors) == 2, f"expected 2 survivors, got {len(survivors)} ({drops})"
    survivor_uids = {l.uid for l in survivors}
    assert survivor.uid in survivor_uids
    assert kept_other_region.uid in survivor_uids
    assert drops["bedrooms"] >= 1
    assert drops["tfl_zone"] >= 1
    assert drops.get("region", 0) == 0
    print(f"PASS: stage-1 filter drops bad rows; region='Other' survives ({drops})")


async def test_stage1_null_size_passes_then_stage2_drops_revealed_small():
    """Null size at stage 1 passes; vision reveals 350 sqft; refilter drops it."""
    hf = _run.load_hard_filters(CONFIG_PATH)

    enriched = _make_stage1_enriched("21", bedrooms=2, stage1_size=None)
    enriched["raw"]["floorplan_urls"] = ["https://test.example/fp.jpg"]
    partial = _run._build_partial_listing(enriched)

    survivors_s1, _ = apply_hard_filters([partial], hf)
    assert len(survivors_s1) == 1, "null size should pass the stage-1 filter"

    async def _fake_floorplan(_url):
        return {"total_size_sqft": 350}

    async def _fake_carpet(_imgs):
        return {
            "carpet_detected": False,
            "carpet_confidence": 0.9,
            "carpet_in_bedroom": False,
            "carpet_other_areas": False,
        }

    with mock.patch("run.extract_floorplan_data", _fake_floorplan), \
         mock.patch("run._detect_carpet_guarded", _fake_carpet):
        await _run._enrich_stage2(enriched)
    _run._finalise_listing(partial, enriched)

    assert partial.size_sqft == 350, f"expected 350, got {partial.size_sqft}"

    survivors_s2, drops_s2 = apply_hard_filters([partial], hf)
    assert len(survivors_s2) == 0, "350 sqft should drop on the refilter"
    assert drops_s2.get("size_sqft", 0) == 1, f"expected 1 size drop, got {drops_s2}"
    print("PASS: refilter drops on stage-2-revealed small size")


async def test_stage1_null_size_passes_then_stage2_keeps_revealed_adequate():
    """Null size at stage 1 passes; vision reveals 700 sqft; refilter keeps it."""
    hf = _run.load_hard_filters(CONFIG_PATH)

    enriched = _make_stage1_enriched("22", bedrooms=2, stage1_size=None)
    enriched["raw"]["floorplan_urls"] = ["https://test.example/fp.jpg"]
    partial = _run._build_partial_listing(enriched)

    async def _fake_floorplan(_url):
        return {"total_size_sqft": 700}

    async def _fake_carpet(_imgs):
        return {
            "carpet_detected": False,
            "carpet_confidence": 0.9,
            "carpet_in_bedroom": False,
            "carpet_other_areas": False,
        }

    with mock.patch("run.extract_floorplan_data", _fake_floorplan), \
         mock.patch("run._detect_carpet_guarded", _fake_carpet):
        await _run._enrich_stage2(enriched)
    _run._finalise_listing(partial, enriched)

    assert partial.size_sqft == 700, f"expected 700, got {partial.size_sqft}"

    survivors_s2, _ = apply_hard_filters([partial], hf)
    assert len(survivors_s2) == 1, "700 sqft should pass the refilter"
    print("PASS: refilter keeps stage-2-revealed adequate size")


async def test_stage2_only_invoked_on_stage1_survivors():
    """End-to-end: drive run_pipeline with mocks; stage 2 must skip stage-1 rejects."""

    stage2_invocations: list[str] = []
    upsert_invocations: list[str] = []

    async def _fake_stage1(scraper, url):
        # The URL carries a tag that determines which stub to return.
        if "fail" in url:
            return _make_stage1_enriched("ff", bedrooms=4, stage1_size=600)
        return _make_stage1_enriched("ok", bedrooms=2, stage1_size=600)

    async def _fake_stage2(enriched):
        stage2_invocations.append(enriched["uid"])
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
            "https://www.rightmove.co.uk/properties/ok",
            "https://www.rightmove.co.uk/properties/fail",
        ]

    def _fake_upsert(db, listing):
        upsert_invocations.append(listing.uid)

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
         mock.patch("run._upsert", _fake_upsert), \
         mock.patch("run.Database") as _MockDB:
        _MockDB.return_value.create_tables.return_value = None
        _MockDB.return_value.engine = None

        result = await _run.run_pipeline(profiles_override=profiles)

    assert len(stage2_invocations) == 1, (
        f"stage 2 should run on the one survivor only, got {stage2_invocations}"
    )
    assert stage2_invocations[0].endswith(":ok")
    assert len(upsert_invocations) == 1
    assert upsert_invocations[0].endswith(":ok")
    assert result["filtered_stage1"] == 1, result
    assert result["saved"] == 1, result
    print(f"PASS: stage 2 invoked once (on survivor), filtered_stage1={result['filtered_stage1']}")


async def main():
    await test_partial_listing_passes_filter_when_signals_clean()
    await test_stage1_filter_drops_obvious_misses()
    await test_stage1_null_size_passes_then_stage2_drops_revealed_small()
    await test_stage1_null_size_passes_then_stage2_keeps_revealed_adequate()
    await test_stage2_only_invoked_on_stage1_survivors()
    print("\nAll tests pass.")


if __name__ == "__main__":
    asyncio.run(main())
