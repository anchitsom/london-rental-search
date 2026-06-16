"""
Tests for the balanced-brace PAGE_MODEL extractor in
homehunt.scrapers.direct_http.

Run: python3 tests/test_page_model_extractor.py

Plain async script convention. Verifies the extractor handles modern
Rightmove HTML (PAGE_MODEL JSON containing nested objects, arrays, and
strings with embedded braces) without truncating at the first '}'.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def assert_eq(label, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


def test_simple_object():
    from homehunt.scrapers.direct_http import _extract_page_model
    html = 'foo bar window.PAGE_MODEL = {"a": 1, "b": 2}; window.OTHER = 3;'
    result = _extract_page_model(html)
    assert_eq("simple_a", result["a"], 1)
    assert_eq("simple_b", result["b"], 2)


def test_nested_objects_and_arrays():
    """Nested structure with internal braces -- the failure mode of the old regex."""
    from homehunt.scrapers.direct_http import _extract_page_model
    html = (
        'prefix window.PAGE_MODEL = '
        '{"propertyData":{"id":"123","sizings":[{"unit":"sqft","minimumSize":506,"maximumSize":506}]}}'
        '; trailing js'
    )
    result = _extract_page_model(html)
    pd = result["propertyData"]
    assert_eq("nested_id", pd["id"], "123")
    assert_eq("nested_sizings_len", len(pd["sizings"]), 1)
    assert_eq("nested_sizings_unit", pd["sizings"][0]["unit"], "sqft")
    assert_eq("nested_sizings_max", pd["sizings"][0]["maximumSize"], 506)


def test_string_with_braces_does_not_truncate():
    """A '}' inside a string value must not be treated as a structural close."""
    from homehunt.scrapers.direct_http import _extract_page_model
    html = (
        'window.PAGE_MODEL = '
        '{"description":"see {note} below","value":42}'
        ';'
    )
    result = _extract_page_model(html)
    assert_eq("string_with_brace_value", result["value"], 42)
    assert_eq("string_with_brace_desc", result["description"], "see {note} below")


def test_escaped_quote_in_string():
    """Backslash-escaped quote inside a string must not flip in_string state."""
    from homehunt.scrapers.direct_http import _extract_page_model
    html = r'window.PAGE_MODEL = {"a":"he said \"hi\" then {","b":1};'
    result = _extract_page_model(html)
    assert_eq("escaped_quote_a", result["a"], 'he said "hi" then {')
    assert_eq("escaped_quote_b", result["b"], 1)


def test_no_page_model_returns_none():
    from homehunt.scrapers.direct_http import _extract_page_model
    assert_eq("no_marker", _extract_page_model("<html>nothing here</html>"), None)


def test_malformed_json_returns_none():
    from homehunt.scrapers.direct_http import _extract_page_model
    html = 'window.PAGE_MODEL = {"a":1, malformed "}'
    assert_eq("malformed", _extract_page_model(html), None)


def test_extract_size_from_sizings_via_full_path():
    """End-to-end: bs4 -> _extract_size_sqft -> sizings sqft entry."""
    from bs4 import BeautifulSoup
    from homehunt.scrapers.direct_http import DirectHTTPScraper

    html = (
        '<html><body><script type="text/javascript">'
        'window.PAGE_MODEL = '
        '{"propertyData":{"id":"87921411","sizings":['
        '{"unit":"sqm","minimumSize":47,"maximumSize":47},'
        '{"unit":"sqft","minimumSize":506,"maximumSize":506}'
        ']}};'
        'window.X = 1;'
        '</script></body></html>'
    )
    soup = BeautifulSoup(html, "html.parser")
    scraper = DirectHTTPScraper()
    size = scraper._extract_size_sqft(soup)
    assert_eq("e2e_sqft_value", size, 506)


def test_extract_size_from_sqm_only_via_full_path():
    """When sqft entry is absent, sqm entry must be converted (factor 10.764)."""
    from bs4 import BeautifulSoup
    from homehunt.scrapers.direct_http import DirectHTTPScraper

    html = (
        '<html><body><script>'
        'window.PAGE_MODEL = '
        '{"propertyData":{"id":"x","sizings":['
        '{"unit":"sqm","minimumSize":47,"maximumSize":47}'
        ']}};'
        '</script></body></html>'
    )
    soup = BeautifulSoup(html, "html.parser")
    scraper = DirectHTTPScraper()
    size = scraper._extract_size_sqft(soup)
    # 47 * 10.764 = 505.908 -> rounds to 506
    assert_eq("e2e_sqm_to_sqft", size, 506)


if __name__ == "__main__":
    tests = [
        ("simple_object", test_simple_object),
        ("nested_objects_and_arrays", test_nested_objects_and_arrays),
        ("string_with_braces", test_string_with_braces_does_not_truncate),
        ("escaped_quote", test_escaped_quote_in_string),
        ("no_page_model", test_no_page_model_returns_none),
        ("malformed_json", test_malformed_json_returns_none),
        ("e2e_sqft", test_extract_size_from_sizings_via_full_path),
        ("e2e_sqm_only", test_extract_size_from_sqm_only_via_full_path),
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
            print(f"  ERROR in {name}: {exc}")
            traceback.print_exc()
            failures.append(name)

    print(f"\n{'=' * 50}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    print(f"ALL {len(tests)} test groups passed.")
