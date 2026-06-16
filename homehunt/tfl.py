"""
TfL (Transport for London) commute enrichment.

Free tier: 500 requests/minute (no key required). A TfL app_key (free registration
at api.tfl.gov.uk) removes that cap. Set TFL_APP_KEY env var if registered.

Pipeline for a listing:
  1. postcodes.io -> (lat, lon)   [handles active and terminated postcodes]
  2. Fan out in parallel:
     Track A: (lat, lon) -> TfL StopPoint (nearest station + zone)
     Track B: TfL Journey Planner (lat,lon as origin -> each destination)

Using coordinates as the Journey Planner origin avoids HTTP 300 (disambiguation)
that TfL returns for terminated postcodes and any postcode its gazetteer cannot
locate. The postcodes.io 404 body carries historical coords for terminated
postcodes, so enrichment succeeds even for retired postcode strings.

Destination defaults to Old Street but is overridden by TFL_DESTINATION env var.
The destination is resolved once to a NaPTAN StopPoint ID at first use to avoid
300 Multiple Choices from the Journey Planner when the destination name is ambiguous.

Rate limiting:
  The module-level _TFL_LIMITER caps outbound TfL calls at TFL_MAX_PER_MIN (default 480,
  leaving headroom below the 500/min hard limit). postcodes.io calls are not counted.
"""

import asyncio
import os
import re
import time
import urllib.parse
from typing import Optional

import httpx
from structlog import get_logger

logger = get_logger()

TFL_APP_KEY = os.environ.get("TFL_APP_KEY", "")
TFL_DESTINATION = os.environ.get("TFL_DESTINATION", "Old Street Station, London")
TFL_MAX_PER_MIN = int(os.environ.get("TFL_MAX_PER_MIN", "480"))  # stay under 500/min

_POSTCODES_IO = "https://api.postcodes.io/postcodes"
_TFL_BASE = "https://api.tfl.gov.uk"

# --- Rate limiter -----------------------------------------------------------

class _TokenBucket:
    """Simple async token bucket. Caps TfL calls at `rate` per 60 seconds."""

    def __init__(self, rate: int):
        self._rate = rate                    # tokens per minute
        self._tokens = float(rate)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(
                self._rate,
                self._tokens + elapsed * (self._rate / 60.0),
            )
            self._last = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / (self._rate / 60.0)
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


_TFL_LIMITER = _TokenBucket(TFL_MAX_PER_MIN)

# --- Destination StopPoint cache -------------------------------------------
# Resolved once per process. Maps destination string to NaPTAN ID (or None).
_stop_id_cache: dict[str, Optional[str]] = {}
_stop_id_lock = asyncio.Lock()

# Matches numeric icsIds (e.g. "1000169"), NaPTAN IDs (e.g. "940GZZLUODS"),
# and HUB codes (e.g. "HUBOLD"). Anything with spaces or commas is a name.
_STOPID_RE = re.compile(r'^[A-Z0-9]{4,}$')

def _looks_like_stopid(value: str) -> bool:
    """True if value is already a resolvable StopPoint ID rather than a name."""
    return bool(_STOPID_RE.match(value.strip()))


def _clean_query(destination: str) -> str:
    """
    Strip suffixes that cause zero-match results from StopPoint/Search.
    The TfL search API does not handle "Station" or ", London" appended to names.
    """
    # Remove ", London" and everything after it.
    query = re.sub(r',\s*london\b.*$', "", destination, flags=re.IGNORECASE)
    # Remove trailing "Station" or "Underground Station" or "Rail Station".
    query = re.sub(r'\s+(underground\s+)?station\b.*$', "", query, flags=re.IGNORECASE)
    return query.strip()


async def resolve_destination_to_stopid(destination: str) -> Optional[str]:
    """
    Resolve a human-readable destination string to a TfL StopPoint icsId.

    Hits /StopPoint/Search, strips city qualifiers from the query (e.g.
    ", London") which otherwise produce zero results. Returns the icsId of
    the best tube/rail match, or None if no match found.
    Result is cached per process.
    """
    dest_key = destination.strip()

    # Already cached (including a cached None for known-bad strings).
    if dest_key in _stop_id_cache:
        return _stop_id_cache[dest_key]

    async with _stop_id_lock:
        # Re-check inside the lock to avoid a race.
        if dest_key in _stop_id_cache:
            return _stop_id_cache[dest_key]

        query = _clean_query(dest_key)
        await _TFL_LIMITER.acquire()
        params = _tfl_params(
            query=query,
            modes="tube,overground,elizabeth-line,national-rail",
            maxResults=10,
        )
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_TFL_BASE}/StopPoint/Search",
                    params=params,
                    timeout=10.0,
                )
                resp.raise_for_status()
                data = resp.json()
                matches = data.get("matches", [])
                if not matches:
                    logger.warning(
                        "tfl_stoppoint_search_no_results",
                        destination=dest_key,
                        query=query,
                    )
                    _stop_id_cache[dest_key] = None
                    return None

                # Prefer tube stops; fall back to first result.
                tube_matches = [
                    m for m in matches
                    if "tube" in [md.lower() for md in m.get("modes", [])]
                ]
                best = tube_matches[0] if tube_matches else matches[0]
                # icsId is the numeric hub ID accepted by Journey Planner.
                stop_id = best.get("icsId") or best.get("id")
                logger.info(
                    "tfl_stoppoint_resolved",
                    destination=dest_key,
                    stop_id=stop_id,
                    name=best.get("name"),
                )
                _stop_id_cache[dest_key] = stop_id
                return stop_id
        except Exception as exc:
            logger.warning(
                "tfl_stoppoint_search_failed", destination=dest_key, error=str(exc)
            )
            _stop_id_cache[dest_key] = None
            return None


def _tfl_params(**kwargs) -> dict:
    """Merge optional app_key into query params."""
    params = {k: v for k, v in kwargs.items() if v is not None}
    if TFL_APP_KEY:
        params["app_key"] = TFL_APP_KEY
    return params


async def _postcode_to_latlon(
    client: httpx.AsyncClient, postcode: str
) -> tuple[Optional[float], Optional[float]]:
    """
    postcodes.io lookup. Free, no auth required.

    Handles two cases:
    - Active postcode (200): returns lat/lon from result.
    - Terminated postcode (404): postcodes.io includes a 'terminated' record in
      the 404 body that carries the historical lat/lon. We extract those coords
      so Journey Planner calls can still use coordinates for the correct area.
      If neither path yields coords, returns (None, None) and logs the miss.
    """
    clean = re.sub(r"\s+", "", postcode).upper()
    try:
        resp = await client.get(
            f"{_POSTCODES_IO}/{urllib.parse.quote(clean)}", timeout=10.0
        )
        if resp.status_code == 200:
            result = resp.json().get("result", {})
            return result.get("latitude"), result.get("longitude")
        if resp.status_code == 404:
            body = resp.json()
            terminated = body.get("terminated", {})
            lat = terminated.get("latitude")
            lon = terminated.get("longitude")
            if lat is not None and lon is not None:
                logger.warning(
                    "tfl_postcode_terminated_using_historical_coords",
                    postcode=postcode,
                    lat=lat,
                    lon=lon,
                )
                return lat, lon
            logger.warning(
                "tfl_postcode_not_found",
                postcode=postcode,
                status=resp.status_code,
                error=body.get("error", "unknown"),
            )
            return None, None
        resp.raise_for_status()
        result = resp.json().get("result", {})
        return result.get("latitude"), result.get("longitude")
    except Exception as exc:
        logger.warning("tfl_postcode_lookup_failed", postcode=postcode, error=str(exc))
        return None, None


async def _nearest_station(
    client: httpx.AsyncClient, lat: float, lon: float
) -> tuple[Optional[str], Optional[int], Optional[int]]:
    """
    Find the nearest tube/overground/Elizabeth line station.

    Returns (station_name, walk_mins, zone).
    walk_mins is estimated from distance at 80 m/min walking pace.
    """
    await _TFL_LIMITER.acquire()
    params = _tfl_params(
        lat=lat,
        lon=lon,
        stopTypes="NaptanMetroStation,NaptanRailStation",
        radius=1200,
        modes="tube,overground,elizabeth-line",
        returnLines="false",
    )
    try:
        resp = await client.get(f"{_TFL_BASE}/StopPoint", params=params, timeout=10.0)
        resp.raise_for_status()
        stops = resp.json().get("stopPoints", [])
        if not stops:
            return None, None, None

        nearest = stops[0]
        name = nearest.get("commonName", "")
        distance_m = nearest.get("distance", 0)
        walk_mins = max(1, round(distance_m / 80))

        # Zone from additionalProperties
        zone = None
        for prop in nearest.get("additionalProperties", []):
            if prop.get("key") == "Zone":
                nums = re.findall(r"\d+", prop.get("value", ""))
                if nums:
                    zone = int(min(nums))
                break

        return name, walk_mins, zone
    except Exception as exc:
        logger.warning("tfl_nearest_station_failed", error=str(exc))
        return None, None, None


async def _journey_time(
    client: httpx.AsyncClient,
    postcode: str,
    destination: str,
    mode: str = "tube,overground,elizabeth-line,bus,national-rail",
    from_latlon: Optional[tuple[float, float]] = None,
) -> Optional[int]:
    """
    TfL Journey Planner: origin -> destination, mode configurable.
    Returns journey duration in minutes for the fastest option, or None.

    Default mode is the public-transport bundle. Pass mode="cycle" for the
    cycle network, or any other TfL-supported mode string.

    If destination is not already a NaPTAN ID, it is resolved via
    resolve_destination_to_stopid first to avoid 300 Multiple Choices.

    from_latlon: when provided, the origin is encoded as "{lat},{lon}" rather
    than the raw postcode string. This avoids HTTP 300 disambiguation from TfL
    for terminated postcodes and postcodes TfL cannot locate in its gazetteer.
    Pass coords obtained from _postcode_to_latlon (which handles both active
    and terminated postcodes via postcodes.io).
    """
    # Resolve ambiguous destination strings to a stable StopPoint ID.
    if not _looks_like_stopid(destination):
        resolved = await resolve_destination_to_stopid(destination)
        to_param = resolved if resolved else destination
    else:
        to_param = destination

    await _TFL_LIMITER.acquire()

    # Prefer lat/lon origin to avoid TfL 300 disambiguation on terminated/
    # unrecognised postcodes.
    if from_latlon is not None:
        origin = f"{from_latlon[0]},{from_latlon[1]}"
    else:
        origin = postcode.strip()

    from_enc = urllib.parse.quote(origin)
    to_enc = urllib.parse.quote(to_param.strip())

    params = _tfl_params(
        mode=mode,
        journeyPreference="LeastTime",
    )
    try:
        resp = await client.get(
            f"{_TFL_BASE}/Journey/JourneyResults/{from_enc}/to/{to_enc}",
            params=params,
            timeout=15.0,
        )
        if resp.status_code == 300:
            # Disambiguation: TfL could not resolve the origin.
            # This happens when from_latlon was None and the raw postcode is
            # unrecognised. Log a structured miss and return None.
            logger.warning(
                "tfl_journey_planner_300_disambiguation",
                postcode=postcode,
                origin=origin,
                destination=destination,
                mode=mode,
                status_code=300,
                decision="returning None; resolve postcode to lat/lon to fix",
            )
            return None
        if resp.status_code == 404:
            # No journey found (e.g. postcodes too close or unresolvable)
            logger.warning(
                "tfl_journey_planner_404_no_journey",
                postcode=postcode,
                origin=origin,
                destination=destination,
                mode=mode,
                status_code=404,
            )
            return None
        resp.raise_for_status()
        journeys = resp.json().get("journeys", [])
        if not journeys:
            logger.warning(
                "tfl_journey_planner_no_journeys",
                postcode=postcode,
                origin=origin,
                destination=destination,
                mode=mode,
            )
            return None
        # Take the fastest journey
        durations = [j.get("duration") for j in journeys if j.get("duration")]
        return min(durations) if durations else None
    except Exception as exc:
        logger.warning(
            "tfl_journey_planner_failed",
            postcode=postcode,
            origin=origin,
            destination=destination,
            mode=mode,
            error=str(exc),
        )
        return None


_DEFAULT_DESTINATIONS: dict[str, str] = {
    "canary_wharf": "Canary Wharf Station, London",
    "whitechapel": "Whitechapel Station, London",
}


async def enrich_commute_multi(
    postcode: Optional[str],
    destinations: Optional[dict[str, str]] = None,
    max_commute_mins: int = 40,
) -> dict:
    """
    TfL enrichment for multiple commute destinations.

    Returns:
      nearest_station, commute_walking, tfl_zone (shared station fields)
      commute_to[name] = minutes for each destination
      commute_pass_40min = True if all destinations under max_commute_mins,
                           False if any exceed or any failed,
                           None if postcode is invalid/None

    Postcodes are resolved to lat/lon via postcodes.io before any Journey
    Planner call. This prevents HTTP 300 disambiguation from TfL, which
    occurs for terminated postcodes and postcodes TfL cannot locate in its
    gazetteer. The postcodes.io lookup handles terminated postcodes by
    extracting coords from the 404 response body.
    """
    null_result: dict = {
        "nearest_station": None,
        "tube_distance": None,
        "tfl_zone": None,
        "commute_to": {},
        "cycle_canary_wharf": None,
        "commute_pass_40min": None,
    }

    if not postcode:
        return null_result

    if destinations is None:
        destinations = _DEFAULT_DESTINATIONS

    null_result["commute_to"] = {name: None for name in destinations}

    async with httpx.AsyncClient() as client:
        # Resolve postcode to lat/lon first. Required so Journey Planner calls
        # use coordinates, avoiding TfL 300 disambiguation on terminated or
        # unrecognised postcodes.
        lat, lon = await _postcode_to_latlon(client, postcode)
        from_latlon: Optional[tuple[float, float]] = (lat, lon) if lat is not None else None

        if from_latlon is None:
            logger.warning(
                "tfl_postcode_coords_unavailable",
                postcode=postcode,
                decision="journey planner calls will use raw postcode; 300 possible",
            )

        async def _track_a_station() -> tuple[Optional[str], Optional[int], Optional[int]]:
            if lat is None:
                return None, None, None
            return await _nearest_station(client, lat, lon)

        async def _journey_for(name: str, dest: str) -> tuple[str, Optional[int]]:
            mins = await _journey_time(
                client, postcode, dest, from_latlon=from_latlon
            )
            return name, mins

        async def _cycle_to_canary() -> Optional[int]:
            cw = destinations.get("canary_wharf")
            if not cw:
                return None
            return await _journey_time(
                client, postcode, cw, mode="cycle", from_latlon=from_latlon
            )

        # Run station lookup, all destination journeys, and cycle-to-CW in parallel.
        # postcodes.io was already called above; remaining tasks are TfL-only.
        tasks = [_track_a_station()] + [
            _journey_for(name, dest) for name, dest in destinations.items()
        ] + [_cycle_to_canary()]
        all_results = await asyncio.gather(*tasks)

    station_result = all_results[0]
    journey_results = all_results[1:-1]
    cycle_canary = all_results[-1]

    station, walk_mins, zone = station_result
    commute_to: dict[str, Optional[int]] = {}
    for name, mins in journey_results:
        commute_to[name] = mins

    # Pass only if all destinations have a valid time and are under the cap.
    if any(mins is None for mins in commute_to.values()):
        commute_pass = False
    else:
        commute_pass = all(mins <= max_commute_mins for mins in commute_to.values())

    result = {
        "nearest_station": station,
        "tube_distance": walk_mins,
        "tfl_zone": zone,
        "commute_to": commute_to,
        "cycle_canary_wharf": cycle_canary,
        "commute_pass_40min": commute_pass,
    }
    logger.info("tfl_multi_enrichment_complete", postcode=postcode, cycle_cw=cycle_canary, **{
        f"commute_{k}": v for k, v in commute_to.items()
    })
    return result


async def enrich_commute(
    postcode: Optional[str],
    destination: str = TFL_DESTINATION,
) -> dict:
    """
    Full TfL enrichment for a property postcode.

    Returns a dict with:
      commute_public_transport  -- journey minutes (public transport, fastest)
      commute_walking           -- walk minutes to nearest station
      tfl_zone                  -- zone number of nearest station
      nearest_station           -- station name
    All values are None if unavailable.

    Postcodes are resolved to lat/lon via postcodes.io before the Journey
    Planner call. This prevents HTTP 300 disambiguation from TfL for
    terminated and unrecognised postcodes.
    """
    result = {
        "commute_public_transport": None,
        "commute_walking": None,
        "tfl_zone": None,
        "nearest_station": None,
    }

    if not postcode:
        return result

    async with httpx.AsyncClient() as client:
        # Resolve postcode to lat/lon first, then fan out TfL calls in parallel.
        # postcodes.io handles terminated postcodes by returning coords from the
        # 404 body, so _journey_time can always use coordinates as the origin.
        lat, lon = await _postcode_to_latlon(client, postcode)
        from_latlon: Optional[tuple[float, float]] = (lat, lon) if lat is not None else None

        async def _track_a():
            if lat is None:
                return None, None, None
            return await _nearest_station(client, lat, lon)

        (station, walk_mins, zone), journey_mins = await asyncio.gather(
            _track_a(),
            _journey_time(client, postcode, destination, from_latlon=from_latlon),
        )

        result["nearest_station"] = station
        result["commute_walking"] = walk_mins
        result["tfl_zone"] = zone
        result["commute_public_transport"] = journey_mins

    logger.info("tfl_enrichment_complete", postcode=postcode, **result)
    return result


async def enrich_commute_multi_from_latlon(
    lat: float,
    lng: float,
    destinations: Optional[dict[str, str]] = None,
    max_commute_mins: int = 40,
) -> dict:
    """
    TfL enrichment using coordinates directly — no postcodes.io lookup.

    Use for portals (e.g. OpenRent) that provide lat/lng from the search page
    but never expose a full postcode. Returns the same dict shape as
    enrich_commute_multi so callers can treat them interchangeably.

    Keys: nearest_station, tube_distance, tfl_zone, commute_to,
          cycle_canary_wharf, commute_pass_40min.
    """
    if destinations is None:
        destinations = _DEFAULT_DESTINATIONS

    null_result: dict = {
        "nearest_station": None,
        "tube_distance": None,
        "tfl_zone": None,
        "commute_to": {name: None for name in destinations},
        "cycle_canary_wharf": None,
        "commute_pass_40min": None,
    }

    from_latlon = (lat, lng)

    async with httpx.AsyncClient() as client:
        station_name, distance_m, zone = await _nearest_station(client, lat, lng)

        async def _journey_for(name: str, dest: str) -> tuple[str, Optional[int]]:
            mins = await _journey_time(client, "", dest, from_latlon=from_latlon)
            return name, mins

        journey_results = await asyncio.gather(
            *[_journey_for(n, d) for n, d in destinations.items()],
            return_exceptions=False,
        )

    commute_to = {name: mins for name, mins in journey_results}
    minutes_list = [m for m in commute_to.values() if m is not None]
    pass_40 = all(m <= max_commute_mins for m in minutes_list) if minutes_list else None

    return {
        "nearest_station": station_name,
        "tube_distance": distance_m,
        "tfl_zone": zone,
        "commute_to": commute_to,
        "cycle_canary_wharf": None,
        "commute_pass_40min": pass_40,
    }
