"""OpenRent discovery: one search GET, geo-prune by Haversine, return (id, lat, lng).

OpenRent loads the entire London universe per search GET; price/bed URL params
do not narrow server-side (probe REPORT 2026-05-23). So discovery returns ALL
inline-array ids by default, then prunes by distance to one or more
neighbourhood centroids. Default radius 2km matches the existing region
resolver's nearest-centroid behaviour.
"""

import math
from typing import Iterable

from homehunt.scrapers.openrent_http import OpenRentHTTP
from homehunt.scrapers.openrent_extract import extract_search


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _passes_geo_filter(lat: float, lng: float, centroids: Iterable[tuple[float, float]], radius_km: float) -> bool:
    return any(_haversine_km(lat, lng, clat, clng) <= radius_km for clat, clng in centroids)


async def discover_openrent_properties(
    *,
    search_url: str,
    centroids: list[tuple[float, float]],
    radius_km: float = 2.0,
) -> list[tuple[int, float, float]]:
    """Return (property_id, lat, lng) for OpenRent listings within radius_km of any centroid."""
    async with OpenRentHTTP() as client:
        status, body = await client.fetch_search(search_url)
        if status != 200:
            raise RuntimeError(f"OpenRent search returned {status}")
    parsed = extract_search(body)
    candidates: list[tuple[int, float, float]] = []
    for pid, lat, lng in zip(parsed["property_ids"], parsed["latitudes"], parsed["longitudes"]):
        if _passes_geo_filter(lat, lng, centroids, radius_km):
            candidates.append((pid, lat, lng))
    return candidates
