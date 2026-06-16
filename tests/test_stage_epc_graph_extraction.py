"""
Wave 1.7 Item 1: structural epcGraphs URL extraction.

Two cases:
  1. PAGE_MODEL with non-empty propertyData.epcGraphs -> first URL returned.
  2. PAGE_MODEL with empty epcGraphs array -> None returned.

Cached fixtures from epc_enrichment/round2/output/page_models/. Each fixture
file is a top-level wrap of the form {url, status, propertyData}; the test
constructs an HTML stub embedding window.PAGE_MODEL = {"propertyData": ...}
so the BeautifulSoup-based scraper extractor sees the same shape Rightmove
serves at run time.

Run:
  .venv/bin/python tests/test_stage_epc_graph_extraction.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from homehunt.scrapers.direct_http import DirectHTTPScraper


_FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "epc_enrichment", "round2", "output", "page_models",
)


def assert_eq(label: str, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


def assert_none(label: str, got):
    if got is not None:
        raise AssertionError(f"FAIL [{label}]: expected None, got {got!r}")
    print(f"  PASS [{label}] (None as expected)")


def assert_not_none(label: str, got):
    if got is None:
        raise AssertionError(f"FAIL [{label}]: expected not-None")
    print(f"  PASS [{label}] (not-None)")


def _build_soup_from_fixture(fixture_name: str) -> BeautifulSoup:
    """
    Load a cached page_models fixture and build a minimal HTML soup with the
    PAGE_MODEL JS embedded inside a <script> tag.

    The cached fixture is shaped {url, status, propertyData}; the live
    Rightmove PAGE_MODEL is shaped {analyticsInfo, propertyData, ...}. The
    extractor only reads .propertyData so the wrapper is sufficient.
    """
    path = os.path.join(_FIXTURE_DIR, fixture_name)
    with open(path, "r") as f:
        cached = json.load(f)
    page_model = {"propertyData": cached.get("propertyData", {})}
    html = (
        "<html><body><script>"
        f"window.PAGE_MODEL = {json.dumps(page_model)};"
        "</script></body></html>"
    )
    return BeautifulSoup(html, "html.parser")


def test_non_empty_epc_graphs():
    """
    87921411 has a single non-empty epcGraphs entry pointing at a Rightmove
    property-epc image (Solar House, 915 High Road).
    """
    print("Test 1: non-empty epcGraphs returns first URL")
    soup = _build_soup_from_fixture("rightmove_87921411.json")
    scraper = DirectHTTPScraper()
    url = scraper._extract_epc_graphs(soup)
    assert_not_none("epc_graph_url returned", url)
    assert_eq(
        "url is the cached value",
        url,
        "https://media.rightmove.co.uk/property-epc/60de2b120/87921411/60de2b120b3be180c255f0b807152564.jpeg",
    )


def test_empty_epc_graphs():
    """
    135995240 has epcGraphs: [] in the cached PAGE_MODEL. Extractor must
    return None.
    """
    print("Test 2: empty epcGraphs returns None")
    soup = _build_soup_from_fixture("rightmove_135995240.json")
    scraper = DirectHTTPScraper()
    url = scraper._extract_epc_graphs(soup)
    assert_none("epc_graph_url is None", url)


def main():
    tests = [test_non_empty_epc_graphs, test_empty_epc_graphs]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as exc:
            print(f"  {exc}")
            failed += 1
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"  ERROR [{t.__name__}]: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
