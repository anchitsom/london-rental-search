"""
Discover Rightmove region IDs for all neighbourhoods within a London borough.

Uses the los.rightmove.co.uk typeahead API to resolve curated neighbourhood
lists to region identifiers, then verifies each ID returns live search results
before printing it.

Usage:
  python3 scripts/discover_borough_regions.py islington
  python3 scripts/discover_borough_regions.py camden
  python3 scripts/discover_borough_regions.py hackney
  python3 scripts/discover_borough_regions.py all

Output (one line per verified region):
  REGION^XXXXX  Name, Location  [verified: N results]

Unresolved or zero-result regions are printed with a [SKIP] prefix so the
caller can decide how to handle them.
"""

import asyncio
import json
import re
import sys
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

# ---------------------------------------------------------------------------
# Curated borough neighbourhood lists.
# Each tuple is (display_name, typeahead_query).
# The typeahead_query is the string we send to los.rightmove.co.uk to resolve
# the REGION ID. It is more specific than the display name to avoid ambiguity
# with identically-named places outside London.
# ---------------------------------------------------------------------------

BOROUGH_NEIGHBOURHOODS: dict[str, list[tuple[str, str]]] = {
    "islington": [
        ("Islington, North London",      "Islington North London"),
        ("Barnsbury, North London",      "Barnsbury London"),
        ("Highbury, North London",       "Highbury London"),
        ("Holloway, North London",       "Holloway London"),
        ("Upper Holloway, North London", "Upper Holloway London"),
        ("Lower Holloway, North London", "Lower Holloway London"),
        ("Tufnell Park, North London",   "Tufnell Park London"),
        ("Archway, London",              "Archway London"),
        ("Finsbury Park, North London",  "Finsbury Park London"),
        ("Canonbury, North London",      "Canonbury London"),
        ("Kings Cross, North London",    "Kings Cross London"),
    ],
    "camden": [
        ("Camden Town, North West London",  "Camden Town London"),
        ("Kentish Town, North West London", "Kentish Town London"),
        ("Belsize Park, North West London", "Belsize Park London"),
        ("Hampstead, North West London",    "Hampstead London"),
        ("Primrose Hill, North West London","Primrose Hill London"),
        ("Chalk Farm, North West London",   "Chalk Farm London"),
        ("Fitzrovia, London",               "Fitzrovia London"),
        ("Bloomsbury, Central London",      "Bloomsbury London"),
        ("Holborn, Central London",         "Holborn London"),
    ],
    "hackney": [
        ("Shoreditch, East London",      "Shoreditch London"),
        ("Hoxton, North London",         "Hoxton London"),
        ("Haggerston, East London",      "Haggerston London"),
        ("Dalston, East London",         "Dalston London"),
        ("De Beauvoir Town, North London","De Beauvoir London"),
        ("Stoke Newington, North London","Stoke Newington London"),
        ("Clapton, East London",         "Clapton London"),
        ("London Fields, East London",   "London Fields"),
        ("Homerton, East London",        "Homerton London"),
        ("Hackney Wick, East London",    "Hackney Wick"),
    ],
    "haringey": [
        ("Tottenham Hale, North London", "Tottenham Hale London"),
        # Seven Sisters has no REGION in the London typeahead; only a STATION.
        # Query the station explicitly so the resolver does not pick up the
        # Welsh village named Seven Sisters as a REGION hit.
        ("Seven Sisters, North London",  "Seven Sisters Station"),
    ],
    "waltham_forest": [
        # Blackhorse Road also has no REGION, only a STATION. "London" as a
        # suffix narrows the typeahead to STREETs only and misses it.
        ("Blackhorse Road, East London", "Blackhorse Road"),
        # Plain "Walthamstow" resolves to REGION^85310. "Walthamstow London"
        # returns only STREETs in the typeahead.
        ("Walthamstow, East London",     "Walthamstow"),
    ],
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-GB,en;q=0.5",
    "Referer": "https://www.rightmove.co.uk/",
}

_TYPEAHEAD = "https://los.rightmove.co.uk/typeahead"
_SEARCH = "https://www.rightmove.co.uk/property-to-rent/find.html"

_SEARCH_HEADERS = {
    **_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}


async def _resolve_via_typeahead(
    client: httpx.AsyncClient, query_term: str
) -> Optional[str]:
    """
    Query los.rightmove.co.uk/typeahead and return the first REGION match.

    Returns None if no REGION match is found.
    """
    resp = await client.get(
        _TYPEAHEAD,
        params={"query": query_term, "limit": 10},
        headers=_HEADERS,
        timeout=10.0,
    )
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except Exception:
        return None

    matches = data.get("matches", [])
    # Prefer a REGION (named area). Fall back to STATION when Rightmove only
    # exposes a tube / overground stop for the place (e.g. Seven Sisters,
    # Blackhorse Road). Rightmove accepts STATION^NNNN as a locationIdentifier.
    for match in matches:
        if match.get("type") == "REGION":
            return f"REGION^{match['id']}"
    for match in matches:
        if match.get("type") == "STATION":
            return f"STATION^{match['id']}"
    return None


async def _verify_region(client: httpx.AsyncClient, region_id: str) -> int:
    """
    Make a minimal search request and return the result count.

    Uses radius=0.5 and a broad price band so the check is fast. A return
    value of 0 means the region ID is broken or returns no rentals.
    """
    params = {
        "locationIdentifier": region_id,
        "channel": "RENT",
        "sortType": "6",
        "radius": "0.5",
        "minPrice": "500",
        "maxPrice": "5000",
        "propertyTypes": "flat",
        "includeLetAgreed": "false",
        "index": "0",
    }
    url = f"{_SEARCH}?{urlencode(params)}"
    resp = await client.get(url, headers=_SEARCH_HEADERS, timeout=20.0)

    if resp.status_code != 200 or "page-not-found" in str(resp.url):
        return 0

    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        resp.text,
        re.DOTALL,
    )
    if m:
        try:
            data = json.loads(m.group(1))
            sr = (
                data.get("props", {})
                .get("pageProps", {})
                .get("searchResults")
                or data.get("props", {})
                .get("pageProps", {})
                .get("searchResult")
                or {}
            )
            count = sr.get("resultCount") or sr.get("total") or 0
            return int(str(count).replace(",", ""))
        except Exception:
            pass

    ids = re.findall(r"/properties/(\d+)", resp.text)
    return len(set(ids))


async def discover_borough(borough: str) -> list[tuple[str, str, int]]:
    """
    Resolve and verify all neighbourhoods for the given borough.

    Returns a list of (display_name, region_id, result_count) for every
    neighbourhood that resolves and returns at least 1 result.
    """
    key = borough.lower()
    if key not in BOROUGH_NEIGHBOURHOODS:
        print(
            f"ERROR: unknown borough '{borough}'. "
            f"Valid: {', '.join(BOROUGH_NEIGHBOURHOODS)}"
        )
        return []

    neighbourhoods = BOROUGH_NEIGHBOURHOODS[key]
    verified: list[tuple[str, str, int]] = []

    async with httpx.AsyncClient(
        headers=_HEADERS, follow_redirects=True, timeout=20.0
    ) as client:
        for display_name, query_term in neighbourhoods:
            region_id = await _resolve_via_typeahead(client, query_term)
            if region_id is None:
                print(f"  [SKIP - unresolved]  {display_name}")
                await asyncio.sleep(0.4)
                continue

            count = await _verify_region(client, region_id)
            if count == 0:
                print(
                    f"  [SKIP - zero results] {region_id:<18}  {display_name}"
                )
            else:
                print(
                    f"  {region_id:<18}  {display_name:<45} [verified: {count} results]"
                )
                verified.append((display_name, region_id, count))

            await asyncio.sleep(0.8)

    return verified


async def main(args: list[str]) -> None:
    if not args:
        print(__doc__)
        sys.exit(1)

    target = args[0].lower()
    boroughs = list(BOROUGH_NEIGHBOURHOODS.keys()) if target == "all" else [target]

    all_results: dict[str, list[tuple[str, str, int]]] = {}
    for borough in boroughs:
        print(f"\n--- {borough.upper()} ---")
        results = await discover_borough(borough)
        all_results[borough] = results

    print("\n\n=== SUMMARY ===")
    for borough, results in all_results.items():
        print(f"\n{borough.upper()} ({len(results)} verified):")
        for display_name, region_id, count in results:
            print(f"  {region_id:<18}  {display_name}")

    print("\n\n=== YAML FRAGMENT ===")
    for borough, results in all_results.items():
        print(f"\n- name: {borough}_borough")
        print(f"  description: All {borough.title()} neighbourhoods")
        print(f"  locations:")
        for display_name, region_id, _count in results:
            print(f"  - name: {display_name}")
            print(f"    region_id: {region_id}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
