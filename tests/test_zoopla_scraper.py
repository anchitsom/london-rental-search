"""
Wave 2.7 Agent K: Zoopla HTTP scraper tests.

Project convention: plain async script, NOT pytest. Run via
.venv/bin/python -m tests.test_zoopla_scraper from the project root.

Test cases:
  1. Search-page fixture parses to >=5 detail-page URLs
  2. Populated-detail fixture extracts price_pcm, bedrooms, address,
     postcode, and >=5 photo_urls
  3. Removed-detail fixture returns success=False with error="property_removed"
  4. scrape_property called before scrape_search_page raises RuntimeError
  5. URL builder produces a Zoopla URL for a known SearchConfig
"""

import asyncio
import os
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


async def test_search_page_parses_urls():
    print("Test 1: search-page fixture yields >=5 detail-page URLs")
    from homehunt.scrapers.zoopla_http import ZooplaHTTPScraper

    html = (FIXTURES / "zoopla_search.html").read_text()
    scraper = ZooplaHTTPScraper()
    urls = scraper._parse_search_html(html)
    _assert("at_least_five", len(urls) >= 5, f"got {len(urls)}")
    _assert("all_strings", all(isinstance(u, str) for u in urls))
    pattern = re.compile(r"^https://www\.zoopla\.co\.uk/to-rent/details/\d+/$")
    _assert("all_match_pattern", all(pattern.match(u) for u in urls),
            f"non-matching: {[u for u in urls if not pattern.match(u)][:3]}")


async def test_populated_detail_parses():
    print("Test 2: populated-detail fixture extracts core fields")
    from homehunt.scrapers.zoopla_http import ZooplaHTTPScraper

    html = (FIXTURES / "zoopla_detail_populated.html").read_text()
    scraper = ZooplaHTTPScraper()
    data = scraper._parse_detail_html(html, "https://www.zoopla.co.uk/to-rent/details/69240407/")
    _assert("price_pcm_present", data.get("price_pcm") is not None,
            f"price_pcm={data.get('price_pcm')!r}")
    _assert("bedrooms_present", data.get("bedrooms") is not None,
            f"bedrooms={data.get('bedrooms')!r}")
    _assert("address_present", bool(data.get("address")),
            f"address={data.get('address')!r}")
    _assert("postcode_present", bool(data.get("postcode")),
            f"postcode={data.get('postcode')!r}")
    photos = data.get("photo_urls") or []
    _assert("photos_at_least_five", len(photos) >= 5, f"got {len(photos)}")


async def test_removed_detail_flagged():
    print("Test 3: removed-detail fixture flagged as property_removed")
    from homehunt.scrapers.zoopla_http import ZooplaHTTPScraper

    html = (FIXTURES / "zoopla_detail_removed.html").read_text()
    scraper = ZooplaHTTPScraper()
    result = scraper._classify_detail_html(html, "https://www.zoopla.co.uk/to-rent/details/12345/")
    _assert("not_success", result["success"] is False)
    _assert("error_property_removed", result["error"] == "property_removed",
            f"got {result['error']!r}")


async def test_scrape_property_before_seed_raises():
    print("Test 4: scrape_property before seed raises RuntimeError")
    from homehunt.scrapers.zoopla_http import ZooplaHTTPScraper

    scraper = ZooplaHTTPScraper()
    raised = False
    try:
        await scraper.scrape_property("https://www.zoopla.co.uk/to-rent/details/69240407/")
    except RuntimeError as e:
        raised = "session not seeded" in str(e).lower()
    _assert("runtime_error_raised", raised)


async def test_url_builder_known_config():
    print("Test 5: URL builder returns valid Zoopla URL for known SearchConfig")
    from homehunt.core.models import SearchConfig
    from homehunt.scrapers.zoopla_url_builder import ZooplaURLBuilder

    config = SearchConfig(
        location="Highbury, London",
        min_bedrooms=1,
        max_bedrooms=1,
        max_price=2800,
    )
    url = ZooplaURLBuilder().build(config)
    _assert("starts_with_zoopla", url.startswith("https://www.zoopla.co.uk/to-rent/property/"),
            f"got {url}")
    _assert("contains_beds_min", "beds_min=1" in url)
    _assert("contains_beds_max", "beds_max=1" in url)
    _assert("contains_price_max", "price_max=2800" in url)
    _assert("contains_per_month", "price_frequency=per_month" in url)
    _assert("contains_to_rent_source", "search_source=to-rent" in url)


async def test_full_disclosure_size_sqft():
    print("Test 6: full-disclosure fixture extracts size_sqft from __ZAD_TARGETING__")
    from homehunt.scrapers.zoopla_http import ZooplaHTTPScraper

    html = (FIXTURES / "zoopla_detail_full_disclosure.html").read_text()
    scraper = ZooplaHTTPScraper()
    data = scraper._parse_detail_html(html, "https://www.zoopla.co.uk/to-rent/details/70812007/")
    _assert("size_sqft_int", isinstance(data.get("size_sqft"), int),
            f"size_sqft={data.get('size_sqft')!r}")
    _assert("size_sqft_value_560", data.get("size_sqft") == 560,
            f"got {data.get('size_sqft')}")
    _assert("size_sqm_present", isinstance(data.get("size_sqm"), int) and data["size_sqm"] > 0,
            f"size_sqm={data.get('size_sqm')!r}")


async def test_full_disclosure_epc_rating():
    print("Test 7: full-disclosure fixture extracts epc_rating from hydration")
    from homehunt.scrapers.zoopla_http import ZooplaHTTPScraper

    html = (FIXTURES / "zoopla_detail_full_disclosure.html").read_text()
    scraper = ZooplaHTTPScraper()
    data = scraper._parse_detail_html(html, "https://www.zoopla.co.uk/to-rent/details/70812007/")
    _assert("epc_rating_present", data.get("epc_rating") in {"A","B","C","D","E","F","G"},
            f"epc_rating={data.get('epc_rating')!r}")
    _assert("epc_rating_C", data.get("epc_rating") == "C",
            f"expected 'C', got {data.get('epc_rating')!r}")


async def test_full_disclosure_floorplan_urls():
    print("Test 8: full-disclosure fixture extracts floorplan_urls")
    from homehunt.scrapers.zoopla_http import ZooplaHTTPScraper

    html = (FIXTURES / "zoopla_detail_full_disclosure.html").read_text()
    scraper = ZooplaHTTPScraper()
    data = scraper._parse_detail_html(html, "https://www.zoopla.co.uk/to-rent/details/70812007/")
    fp = data.get("floorplan_urls") or []
    _assert("floorplan_urls_list", isinstance(fp, list) and len(fp) >= 1,
            f"got {fp!r}")
    _assert("floorplan_url_zoocdn", any("zoocdn.com" in u for u in fp),
            f"got {fp!r}")
    _assert("floorplan_url_image_extension", all(u.endswith((".jpg",".jpeg",".png",".pdf")) for u in fp),
            f"got {fp!r}")


async def test_absent_fields_are_none():
    print("Test 9: detail-page fixture without size/epc/floorplan returns absent fields cleanly")
    from homehunt.scrapers.zoopla_http import ZooplaHTTPScraper

    html = (FIXTURES / "zoopla_detail_populated.html").read_text()
    scraper = ZooplaHTTPScraper()
    data = scraper._parse_detail_html(html, "https://www.zoopla.co.uk/to-rent/details/69240407/")
    _assert("size_sqft_absent", data.get("size_sqft") is None, f"got {data.get('size_sqft')!r}")
    _assert("epc_rating_absent", data.get("epc_rating") is None, f"got {data.get('epc_rating')!r}")
    fp = data.get("floorplan_urls") or []
    _assert("floorplan_urls_empty", len(fp) == 0, f"got {fp!r}")


async def main():
    await test_search_page_parses_urls()
    await test_populated_detail_parses()
    await test_removed_detail_flagged()
    await test_scrape_property_before_seed_raises()
    await test_url_builder_known_config()
    await test_full_disclosure_size_sqft()
    await test_full_disclosure_epc_rating()
    await test_full_disclosure_floorplan_urls()
    await test_absent_fields_are_none()
    print("\n[test_zoopla_scraper] ALL TESTS PASS")


if __name__ == "__main__":
    asyncio.run(main())
