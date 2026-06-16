"""
Rightmove search discovery via plain httpx.

Plain HTTP with a realistic User-Agent is sufficient; Rightmove does not
require JS execution or challenge cookies for search result pages.

Flow per discover_properties() call:
  1. Resolve location name → locationIdentifier via typeahead API
  2. Fetch paginated search pages
  3. Extract property IDs from __NEXT_DATA__ JSON embedded in each page
  4. Return deduplicated property URLs

Rate limiting: 1.5s delay between pages.
"""

import asyncio
import json
import re
from typing import Optional
from urllib.parse import urlencode

import httpx
from structlog import get_logger

logger = get_logger()

_BASE = "https://www.rightmove.co.uk"
_SEARCH = f"{_BASE}/property-to-rent/find.html"
# The correct typeahead endpoint is on los.rightmove.co.uk, not www.
# www.rightmove.co.uk/typeAhead/uknostreet/typeahead returns a 404 page.
# los.rightmove.co.uk/typeahead accepts ?query=<term>&limit=N and returns
# JSON with a "matches" array: each match has "id", "type", and "displayName".
# Build locationIdentifier as f"{match['type']}^{match['id']}".
_TYPEAHEAD = "https://los.rightmove.co.uk/typeahead"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
}

# Fallback table for areas that typeahead fails to resolve, or as a fast path
# to avoid a network round-trip for frequently used locations.
# All IDs verified live on 2026-05-02 via the search endpoint.
#
# Removed stale entries:
#   REGION^85279 (old Islington) -- redirects to page-not-found
#   REGION^87487 (Farringdon/Clerkenwell) -- returns zero results
#   REGION^87490 was mapped to Shoreditch/Hackney but is actually Greater London
#   (13,000+ results). Replaced with correct neighbourhood-level IDs.
#   REGION^87494 was mapped to Finsbury Park but is actually Bloomsbury.
#   REGION^87498 resolves correctly via the new typeahead endpoint.
_KNOWN_IDS: dict[str, str] = {
    # Islington borough
    "canonbury":                    "REGION^85331",
    "canonbury, north london":      "REGION^85331",
    "islington":                    "REGION^87515",
    "islington, north london":      "REGION^87515",
    "barnsbury":                    "REGION^85330",
    "barnsbury, north london":      "REGION^85330",
    "highbury":                     "REGION^70438",
    "highbury, north london":       "REGION^70438",
    "holloway":                     "REGION^87514",
    "holloway, north london":       "REGION^87514",
    "upper holloway":               "REGION^85205",
    "upper holloway, north london": "REGION^85205",
    "lower holloway":               "REGION^85221",
    "lower holloway, north london": "REGION^85221",
    "tufnell park":                 "REGION^85222",
    "tufnell park, north london":   "REGION^85222",
    "archway":                      "REGION^93727",
    "archway, london":              "REGION^93727",
    "finsbury park":                "REGION^85353",
    "finsbury park, north london":  "REGION^85353",
    "kings cross":                  "REGION^87399",
    "kings cross, north london":    "REGION^87399",
    # Camden borough
    "camden town":                      "REGION^85262",
    "camden town, north west london":   "REGION^85262",
    "kentish town":                     "REGION^85230",
    "kentish town, north west london":  "REGION^85230",
    "belsize park":                     "REGION^70356",
    "belsize park, north west london":  "REGION^70356",
    "hampstead":                        "REGION^87509",
    "hampstead, north west london":     "REGION^87509",
    "primrose hill":                    "REGION^87390",
    "primrose hill, north west london": "REGION^87390",
    "chalk farm":                       "REGION^85263",
    "chalk farm, north west london":    "REGION^85263",
    "fitzrovia":                        "REGION^93764",
    "fitzrovia, london":                "REGION^93764",
    "bloomsbury":                       "REGION^87494",
    "bloomsbury, central london":       "REGION^87494",
    "holborn":                          "REGION^87513",
    "holborn, central london":          "REGION^87513",
    # Hackney borough
    "shoreditch":                       "REGION^87528",
    "shoreditch, east london":          "REGION^87528",
    "hoxton":                           "REGION^85332",
    "hoxton, north london":             "REGION^85332",
    "haggerston":                       "REGION^85390",
    "haggerston, east london":          "REGION^85390",
    "dalston":                          "REGION^85389",
    "dalston, east london":             "REGION^85389",
    "de beauvoir town":                 "REGION^70393",
    "de beauvoir town, north london":   "REGION^70393",
    "stoke newington":                  "REGION^85413",
    "stoke newington, north london":    "REGION^85413",
    "clapton":                          "REGION^85455",
    "clapton, east london":             "REGION^85455",
    "london fields":                    "REGION^70417",
    "london fields, east london":       "REGION^70417",
    "homerton":                         "REGION^70408",
    "homerton, east london":            "REGION^70408",
    "hackney wick":                     "REGION^85371",
    "hackney wick, east london":        "REGION^85371",
}


def _extract_properties(html: str) -> tuple[list[dict], int]:
    """
    Extract property list and result count from Rightmove HTML.

    Primary path: __NEXT_DATA__ at props.pageProps.searchResults.properties
    Fallback: property IDs from href patterns
    """
    # Pattern 1: __NEXT_DATA__ (current Rightmove format)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            page_props = data.get("props", {}).get("pageProps", {})
            # Try both key variants Rightmove has used
            sr = page_props.get("searchResults") or page_props.get("searchResult") or {}
            props = sr.get("properties") or sr.get("listings") or page_props.get("properties")
            if props is not None:
                result_count = sr.get("resultCount") or sr.get("total") or len(props)
                try:
                    result_count = int(str(result_count).replace(",", ""))
                except (ValueError, TypeError):
                    result_count = len(props)
                logger.debug(
                    "rightmove_nextjs_extracted",
                    properties=len(props),
                    result_count=result_count,
                )
                return props, result_count
            # Log what we did find to help debug future structure changes
            sr_keys = list(sr.keys())[:10] if isinstance(sr, dict) else repr(type(sr))
            logger.debug(
                "rightmove_nextjs_no_properties",
                pageprops_keys=list(page_props.keys())[:15],
                sr_keys=sr_keys,
            )
        except json.JSONDecodeError as exc:
            logger.warning("rightmove_nextjs_parse_error", error=str(exc))

    # Pattern 2: window.PAGE_MODEL (legacy)
    for pattern in [
        r"window\.PAGE_MODEL\s*=\s*(\{.*?\});\s*</script>",
        r"window\.PAGE_MODEL\s*=\s*(\{.*\})",
    ]:
        m = re.search(pattern, html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                props = (
                    data.get("properties")
                    or data.get("searchResult", {}).get("properties")
                )
                if props is not None:
                    count = (
                        data.get("resultCount")
                        or data.get("searchResult", {}).get("resultCount", 0)
                    )
                    return props, int(str(count).replace(",", "") or "0")
            except json.JSONDecodeError:
                pass

    # Pattern 3: href fallback, property IDs from anchor tags
    ids = list(dict.fromkeys(re.findall(r'/properties/(\d+)', html)))
    if ids:
        logger.info("rightmove_fallback_href", count=len(ids))
        return [{"id": pid} for pid in ids], len(ids)

    js_vars = list(set(re.findall(r'window\.(\w+)\s*=', html)))[:15]
    logger.warning("rightmove_no_data_found", js_vars=js_vars)
    return [], 0


def _extract_urls(properties: list[dict]) -> list[str]:
    """Build property URLs from extracted property objects."""
    urls = []
    for prop in properties:
        pid = (
            prop.get("id")
            or prop.get("propertyId")
            or prop.get("property_id")
        )
        if pid:
            url = prop.get("propertyUrl")
            if url and url.startswith("/"):
                urls.append(f"{_BASE}{url}")
            else:
                urls.append(f"{_BASE}/properties/{pid}")
    return urls


def _build_search_url(
    location_id: Optional[str],
    location_text: str,
    min_price: Optional[int],
    max_price: Optional[int],
    min_bedrooms: Optional[int],
    max_bedrooms: Optional[int],
    radius: float,
    index: int,
) -> str:
    params: dict = {
        "sortType": "6",
        "channel": "RENT",
        "includeLetAgreed": "false",
        "dontShow": "houseShare,retirement,student",
        "propertyTypes": "flat",
        "index": str(index),
        "radius": str(radius),
    }
    if location_id:
        params["locationIdentifier"] = location_id
    else:
        params["searchType"] = "RENT"
        params["searchLocation"] = location_text
        params["useLocationIdentifier"] = "false"
    if min_price is not None:
        params["minPrice"] = str(min_price)
    if max_price is not None:
        params["maxPrice"] = str(max_price)
    if min_bedrooms is not None:
        params["minBedrooms"] = str(min_bedrooms)
    if max_bedrooms is not None:
        params["maxBedrooms"] = str(max_bedrooms)
    return f"{_SEARCH}?{urlencode(params)}"


async def _resolve_location(client: httpx.AsyncClient, location: str) -> Optional[str]:
    """
    Resolve a location name to a Rightmove locationIdentifier.

    Tries:
      1. Known ID table (instant, no network)
      2. Typeahead API via httpx
    """
    key = location.strip().lower()

    # 1. Known table
    if key in _KNOWN_IDS:
        lid = _KNOWN_IDS[key]
        logger.info("rightmove_location_from_table", location=location, identifier=lid)
        return lid

    # 2. Typeahead API (los.rightmove.co.uk/typeahead)
    # Response format: {"matches": [{"id": "NNNNN", "type": "REGION", "displayName": "..."}]}
    # Use the first match whose type is REGION. Other types (STREET, STATION, OUTCODE)
    # can appear in results but are not suitable as search location identifiers.
    try:
        resp = await client.get(
            _TYPEAHEAD,
            params={"query": location, "limit": 10},
            headers={**_HEADERS, "Accept": "application/json", "Referer": "https://www.rightmove.co.uk/"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            body = resp.text.strip()
            if body.startswith("{"):
                data = json.loads(body)
                matches = data.get("matches", [])
                for match in matches:
                    if match.get("type") == "REGION" and match.get("id"):
                        lid = f"REGION^{match['id']}"
                        logger.info(
                            "rightmove_location_typeahead",
                            location=location,
                            identifier=lid,
                            display_name=match.get("displayName", ""),
                        )
                        return lid
    except Exception as exc:
        logger.warning("rightmove_typeahead_failed", location=location, error=str(exc))

    logger.warning("rightmove_location_unresolved", location=location)
    return None


async def discover_properties(
    location: str,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    min_bedrooms: Optional[int] = None,
    max_bedrooms: Optional[int] = None,
    radius: float = 1.0,
    max_results: int = 40,
    page_delay: float = 1.5,
    location_id: Optional[str] = None,
) -> list[str]:
    """
    Discover Rightmove property URLs for a given location via plain HTTP.

    Pass location_id (e.g. "REGION^85331") to skip name resolution entirely.
    Returns deduplicated list of property URLs.
    """
    results_per_page = 24
    max_pages = max(1, -(-max_results // results_per_page))
    all_urls: list[str] = []

    async with httpx.AsyncClient(
        headers=_HEADERS,
        follow_redirects=True,
        timeout=20.0,
    ) as client:
        if location_id is None:
            location_id = await _resolve_location(client, location)

        for page_num in range(max_pages):
            index = page_num * results_per_page
            url = _build_search_url(
                location_id=location_id,
                location_text=location,
                min_price=min_price,
                max_price=max_price,
                min_bedrooms=min_bedrooms,
                max_bedrooms=max_bedrooms,
                radius=radius,
                index=index,
            )

            try:
                resp = await client.get(url)

                if resp.status_code != 200 or "page-not-found" in str(resp.url):
                    logger.warning(
                        "rightmove_page_blocked",
                        location=location,
                        page=page_num,
                        status=resp.status_code,
                        url=str(resp.url)[:100],
                    )
                    break

                properties, total = _extract_properties(resp.text)
                page_urls = _extract_urls(properties)

                if not page_urls:
                    logger.info(
                        "rightmove_no_urls_on_page",
                        location=location,
                        page=page_num,
                    )
                    break

                all_urls.extend(page_urls)
                logger.info(
                    "rightmove_page_scraped",
                    location=location,
                    page=page_num,
                    found=len(page_urls),
                    total_so_far=len(all_urls),
                    result_count=total,
                )

                if total and index + results_per_page >= total:
                    break
                if not total and len(page_urls) < results_per_page:
                    break

                if page_num < max_pages - 1:
                    await asyncio.sleep(page_delay)

            except Exception as exc:
                logger.error(
                    "rightmove_page_error",
                    location=location,
                    page=page_num,
                    error=str(exc),
                )
                break

    # Deduplicate preserving order
    seen: set[str] = set()
    unique = []
    for u in all_urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    logger.info(
        "rightmove_discovery_complete",
        location=location,
        total_unique=len(unique),
    )
    return unique


def _resolve_dehydrated(arr: list, value, depth: int = 0):
    """Resolve a dehydrated PAGE_MODEL value: integers are indices into arr.

    Rightmove's modern `window.__PAGE_MODEL` shape ships JSON as a flat array
    where most object fields are integer indices pointing back into the array.
    Walk one level at a time; depth-cap to avoid pathological cycles. Booleans
    are returned as-is (Python treats bool as a subclass of int).
    """
    if depth > 4:
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and 0 <= value < len(arr):
        return _resolve_dehydrated(arr, arr[value], depth + 1)
    return value


def detect_let_agreed_from_page_model(page_model: Optional[dict]) -> bool:
    """Return True if the Rightmove PAGE_MODEL indicates the listing is let-agreed.

    Accepts the dict returned by `direct_http._extract_page_model`. Handles
    both shapes:
      1. Legacy hydrated: page_model["propertyData"] is a dict with "status",
         "lettings", "displayStatus" reachable directly.
      2. Modern dehydrated: page_model = {"data": "<json array>", "encoding": ...}.
         Walk the index references to read status.published / status.archived.

    Returns True when any of:
      - propertyData.status.archived is True (Rightmove pulls let-agreed listings)
      - propertyData.status.published is False
      - displayStatus contains "let agreed" (case-insensitive)
      - propertyData.lettings.letAgreed is True

    Defensive: returns False on missing data or unexpected shape, never raises.
    """
    if not isinstance(page_model, dict):
        return False

    # Modern dehydrated form.
    raw = page_model.get("data")
    if isinstance(raw, str):
        try:
            arr = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return False
        if not isinstance(arr, list) or len(arr) < 2:
            return False
        # arr[0] is the top object; arr[0].propertyData is the index of the
        # propertyData record.
        try:
            top = arr[0]
            if not isinstance(top, dict):
                return False
            pd_idx = top.get("propertyData")
            if isinstance(pd_idx, bool) or not isinstance(pd_idx, int):
                return False
            if not (0 <= pd_idx < len(arr)):
                return False
            pd = arr[pd_idx]
            if not isinstance(pd, dict):
                return False
            status_idx = pd.get("status")
            if (
                isinstance(status_idx, int)
                and not isinstance(status_idx, bool)
                and 0 <= status_idx < len(arr)
            ):
                status = arr[status_idx]
                if isinstance(status, dict):
                    pub = _resolve_dehydrated(arr, status.get("published"))
                    arch = _resolve_dehydrated(arr, status.get("archived"))
                    if arch is True:
                        return True
                    if pub is False:
                        return True
            # displayStatus may be a direct string index.
            ds = _resolve_dehydrated(arr, pd.get("displayStatus"))
            if isinstance(ds, str) and "let agreed" in ds.lower():
                return True
            # lettings.letAgreed
            lettings = _resolve_dehydrated(arr, pd.get("lettings"))
            if isinstance(lettings, dict):
                la = _resolve_dehydrated(arr, lettings.get("letAgreed"))
                if la is True:
                    return True
        except (KeyError, TypeError, IndexError):
            return False
        return False

    # Legacy hydrated form.
    prop = page_model.get("propertyData")
    if not isinstance(prop, dict):
        return False
    status = prop.get("status")
    if isinstance(status, dict):
        if status.get("archived") is True:
            return True
        if status.get("published") is False:
            return True
    ds = prop.get("displayStatus")
    if isinstance(ds, str) and "let agreed" in ds.lower():
        return True
    lettings = prop.get("lettings")
    if isinstance(lettings, dict) and lettings.get("letAgreed") is True:
        return True
    return False


async def resolve_location_id(location: str) -> Optional[str]:
    """
    Public helper: resolve a human location string to a Rightmove REGION^ID.

    Checks the known-ID table first (no network), then hits the typeahead API.
    Returns None if the location cannot be resolved.
    Used by scripts/resolve_region_ids.py.
    """
    async with httpx.AsyncClient(
        headers=_HEADERS,
        follow_redirects=True,
        timeout=10.0,
    ) as client:
        return await _resolve_location(client, location)
