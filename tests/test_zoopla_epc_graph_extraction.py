"""
Pre-Wave-3 S2: Zoopla EPC graph URL extraction tests.

Project convention: plain async script, NOT pytest. Run via
.venv/bin/python -m tests.test_zoopla_epc_graph_extraction from the project root.

Background. Wave 1.7 wired the EPC vision OCR rescue at run.py:312, gated on
data["epc_graph_url"] being present on the scrape result. Rightmove populates
that field via direct_http._extract_epc_graphs (PAGE_MODEL.propertyData.epcGraphs
plus a regex fallback over media.rightmove.co.uk/...epc...). Zoopla rows had
no equivalent extraction, so the vision rescue could never fire on Zoopla
listings even when the page hydration carried an EPC chart filename.

Empirical investigation of zoopla_detail_full_disclosure.html (NW3, listing
70812007) found Zoopla exposes the EPC chart inside the React Server
Components hydration payload at:

    "epc": {
      "image": [{"caption": "EPC", "filename": "<sha1>.jpg"}],
      "links": null,
      "pdf": null
    }

The filename resolves to https://lid.zoocdn.com/u/1024/768/<filename> (the
same imgproxy CDN as photos). Live HEAD against that URL returns 200 and the
body is a 552x613 JPEG of the standard EPC Energy Efficiency Rating chart.

zoopla_detail_populated.html (the older 69240407 fixture) contains
"epc":{"image":null,...} which is the null case.

Tests.
  1. full_disclosure fixture extracts epc_graph_url to a plausible CDN URL
     ending in the recovered filename.
  2. populated fixture leaves epc_graph_url absent (the null branch).
  3. Existing Wave 2.7 Agent K extracted fields are pinned: size_sqft,
     size_sqm, epc_rating, floorplan_urls. Regression guard so the new
     extraction does not accidentally damage them.
  4. The EPC chart filename does not leak into photo_urls. The pre-existing
     _parse_photos picked up every sha1 hash in the document including the
     EPC chart's, which polluted carpet detection input. The fix de-noises
     photo_urls by subtracting the EPC filename.
"""

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"


def _assert(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL [{label}]: {detail}")
    print(f"  PASS [{label}]")


async def test_full_disclosure_epc_graph_url_populated():
    print("Test 1: full-disclosure fixture extracts epc_graph_url")
    from homehunt.scrapers.zoopla_http import ZooplaHTTPScraper

    html = (FIXTURES / "zoopla_detail_full_disclosure.html").read_text()
    scraper = ZooplaHTTPScraper()
    data = scraper._parse_detail_html(
        html, "https://www.zoopla.co.uk/to-rent/details/70812007/"
    )
    url = data.get("epc_graph_url")
    _assert(
        "epc_graph_url_string",
        isinstance(url, str) and url.startswith("https://"),
        f"got {url!r}",
    )
    _assert(
        "epc_graph_url_zoocdn",
        "zoocdn.com" in url,
        f"got {url!r}",
    )
    _assert(
        "epc_graph_url_image_extension",
        url.lower().endswith((".jpg", ".jpeg", ".png")),
        f"got {url!r}",
    )
    # The hydration payload has filename 4c8e7635...e5e0dd27862.jpg. Pin
    # that sha1 so a regex-shape regression is caught.
    _assert(
        "epc_graph_url_contains_known_hash",
        "4c8e7635f6fb222ffa4c2df931585e5e0dd27862" in url,
        f"got {url!r}",
    )


async def test_populated_fixture_epc_graph_url_absent():
    print("Test 2: populated fixture (epc.image null) yields no epc_graph_url")
    from homehunt.scrapers.zoopla_http import ZooplaHTTPScraper

    html = (FIXTURES / "zoopla_detail_populated.html").read_text()
    scraper = ZooplaHTTPScraper()
    data = scraper._parse_detail_html(
        html, "https://www.zoopla.co.uk/to-rent/details/69240407/"
    )
    _assert(
        "epc_graph_url_absent",
        data.get("epc_graph_url") is None,
        f"got {data.get('epc_graph_url')!r}",
    )


async def test_wave2_7_k_fields_not_regressed():
    print("Test 3: Wave 2.7 K-extracted fields still populated on full-disclosure")
    from homehunt.scrapers.zoopla_http import ZooplaHTTPScraper

    html = (FIXTURES / "zoopla_detail_full_disclosure.html").read_text()
    scraper = ZooplaHTTPScraper()
    data = scraper._parse_detail_html(
        html, "https://www.zoopla.co.uk/to-rent/details/70812007/"
    )
    _assert(
        "size_sqft_560",
        data.get("size_sqft") == 560,
        f"got {data.get('size_sqft')!r}",
    )
    _assert(
        "size_sqm_int",
        isinstance(data.get("size_sqm"), int) and data["size_sqm"] > 0,
        f"got {data.get('size_sqm')!r}",
    )
    _assert(
        "epc_rating_C",
        data.get("epc_rating") == "C",
        f"got {data.get('epc_rating')!r}",
    )
    fp = data.get("floorplan_urls") or []
    _assert(
        "floorplan_urls_at_least_one",
        len(fp) >= 1,
        f"got {fp!r}",
    )


async def test_epc_filename_not_in_photo_urls():
    print("Test 4: EPC chart filename does not leak into photo_urls")
    from homehunt.scrapers.zoopla_http import ZooplaHTTPScraper

    html = (FIXTURES / "zoopla_detail_full_disclosure.html").read_text()
    scraper = ZooplaHTTPScraper()
    data = scraper._parse_detail_html(
        html, "https://www.zoopla.co.uk/to-rent/details/70812007/"
    )
    photos = data.get("photo_urls") or []
    epc_hash = "4c8e7635f6fb222ffa4c2df931585e5e0dd27862"
    leak = [p for p in photos if epc_hash in p]
    _assert(
        "no_epc_filename_in_photos",
        len(leak) == 0,
        f"leak={leak!r}",
    )
    # And photos still has at least the listing photos (not gutted).
    _assert(
        "photos_at_least_five",
        len(photos) >= 5,
        f"got {len(photos)} photos",
    )


async def main():
    await test_full_disclosure_epc_graph_url_populated()
    await test_populated_fixture_epc_graph_url_absent()
    await test_wave2_7_k_fields_not_regressed()
    await test_epc_filename_not_in_photo_urls()
    print("\n[test_zoopla_epc_graph_extraction] ALL TESTS PASS")


if __name__ == "__main__":
    asyncio.run(main())
