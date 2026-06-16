"""
Stage 2b: Test plain httpx + curl against a real Rightmove search URL.
Tests whether we need Playwright at all, or if a simple User-Agent header suffices.

Run inside container: python tests/test_stage2b_plain_http.py
"""

import asyncio
import json
import re
import subprocess
import sys

TEST_URL = (
    "https://www.rightmove.co.uk/property-to-rent/find.html"
    "?locationIdentifier=REGION%5E85331"
    "&minBedrooms=2&maxPrice=2750&radius=0.0&sortType=6"
    "&channel=RENT&includeLetAgreed=false"
    "&propertyTypes=flat"
    "&index=0"
)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def extract_nextjs(html: str) -> dict | None:
    """Extract data from Next.js __NEXT_DATA__ script tag."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def extract_page_model(html: str) -> dict | None:
    """Extract data from window.PAGE_MODEL."""
    m = re.search(r'window\.PAGE_MODEL\s*=\s*(\{.*?\});\s*</script>', html, re.DOTALL)
    if not m:
        m = re.search(r'window\.PAGE_MODEL\s*=\s*(\{.*\})', html, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


def probe_json(data: dict, path: str) -> tuple[bool, any]:
    """Walk a dot-separated path and return (found, value)."""
    parts = path.split(".")
    cur = data
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return False, None
        cur = cur[p]
    return True, cur


def analyse_html(label: str, html: str, status: int):
    print(f"\n  Status: {status}")
    print(f"  HTML length: {len(html)} chars")
    blocked = "page-not-found" in html[:500] or status != 200
    print(f"  Blocked: {blocked}")

    if blocked:
        return

    # Try __NEXT_DATA__
    nd = extract_nextjs(html)
    if nd:
        print("  __NEXT_DATA__: found")
        # Try uk-property-cli path
        found, val = probe_json(nd, "props.pageProps.searchResults.properties")
        if found:
            print(f"    props.pageProps.searchResults.properties: {len(val)} properties")
            if val:
                p = val[0]
                print(f"    First property: id={p.get('id')} addr={p.get('displayAddress')} price={p.get('price', {}).get('amount')}")
        else:
            # Print top-level keys to help navigate the structure
            print(f"    props.pageProps.searchResults.properties: NOT FOUND")
            pp = nd.get("props", {}).get("pageProps", {})
            print(f"    pageProps keys: {list(pp.keys())[:15]}")
            sr = pp.get("searchResults") or pp.get("searchResult")
            if sr:
                print(f"    searchResults keys: {list(sr.keys())[:10]}")
                props = sr.get("properties") or sr.get("listings")
                if props:
                    print(f"    properties count: {len(props)}")
    else:
        print("  __NEXT_DATA__: not found")

    # Try window.PAGE_MODEL
    pm = extract_page_model(html)
    if pm:
        print("  window.PAGE_MODEL: found")
        props = pm.get("properties") or pm.get("searchResult", {}).get("properties")
        if props:
            print(f"    properties count: {len(props)}")
        else:
            print(f"    PAGE_MODEL keys: {list(pm.keys())[:10]}")
    else:
        print("  window.PAGE_MODEL: not found")

    # Fallback: count property IDs in href
    ids = list(set(re.findall(r'/properties/(\d+)', html)))
    print(f"  Property IDs in href: {len(ids)}")
    if ids:
        print(f"    Sample IDs: {ids[:5]}")


async def test_httpx():
    import httpx
    print("\n=== Test 1: httpx with User-Agent ===")
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        resp = await client.get(TEST_URL, headers=headers)
        analyse_html("httpx", resp.text, resp.status_code)


def test_curl():
    print("\n=== Test 2: curl with User-Agent ===")
    result = subprocess.run(
        ["curl", "-s", "-L",
         "-H", f"User-Agent: {UA}",
         "-H", "Accept: text/html,application/xhtml+xml,*/*",
         "-H", "Accept-Language: en-GB,en;q=0.5",
         "-w", "\n__STATUS__%{http_code}",
         TEST_URL],
        capture_output=True, text=True, timeout=20
    )
    output = result.stdout
    if "__STATUS__" in output:
        body, status_str = output.rsplit("__STATUS__", 1)
        status = int(status_str.strip())
    else:
        body = output
        status = 0
    analyse_html("curl", body, status)


async def main():
    print(f"URL: {TEST_URL}")
    await test_httpx()
    test_curl()
    print("\nStage 2b: DONE")


asyncio.run(main())
