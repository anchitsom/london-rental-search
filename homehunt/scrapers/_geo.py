"""
Geographic helper functions extracted from listing HTML and text.

These are utility functions for postcode extraction, address cleaning,
and coordinate extraction from Rightmove page HTML. They do not depend on
external geocoding services.
"""

import re
from typing import Optional, Tuple


_POSTCODE_RE = re.compile(
    r"\b([A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2})\b", re.IGNORECASE
)


def extract_postcode(text: str) -> Optional[str]:
    """Return the first UK postcode found in text, normalised to uppercase."""
    if not text:
        return None
    m = _POSTCODE_RE.search(text)
    if m:
        return m.group(1).upper().strip()
    return None


def clean_address(address: str) -> str:
    """Strip trailing postcode and extra whitespace from an address string."""
    if not address:
        return address
    cleaned = _POSTCODE_RE.sub("", address)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip().strip(",").strip()
    return cleaned


def extract_coordinates_from_maps_embed(html: str) -> Optional[Tuple[float, float]]:
    """
    Extract lat/lng from Rightmove page HTML.

    Looks for:
    - window.PAGE_MODEL latitude/longitude JSON keys
    - Google Maps embed URLs (?q=lat,lng or &ll=lat,lng)
    - lat/lng in script variable assignments
    """
    if not html:
        return None

    # Pattern 1: PAGE_MODEL JSON contains location.latitude / location.longitude
    m = re.search(
        r'"latitude"\s*:\s*(-?\d{1,3}\.\d+).*?"longitude"\s*:\s*(-?\d{1,3}\.\d+)',
        html,
        re.DOTALL,
    )
    if m:
        try:
            lat, lng = float(m.group(1)), float(m.group(2))
            if _valid_london_coords(lat, lng):
                return lat, lng
        except ValueError:
            pass

    # Pattern 2: Google Maps embed ?q=LAT,LNG or &center=LAT,LNG
    m = re.search(
        r"[?&](?:q|center|ll)=(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)", html
    )
    if m:
        try:
            lat, lng = float(m.group(1)), float(m.group(2))
            if _valid_london_coords(lat, lng):
                return lat, lng
        except ValueError:
            pass

    return None


def extract_coordinates_from_text(text: str) -> Optional[Tuple[float, float]]:
    """
    Fallback: look for bare decimal coordinate pairs in any text/URL content.
    Only returns if coordinates fall within Greater London bounding box.
    """
    if not text:
        return None
    for m in re.finditer(r"(-?\d{1,3}\.\d{4,})\s*,\s*(-?\d{1,3}\.\d{4,})", text):
        try:
            lat, lng = float(m.group(1)), float(m.group(2))
            if _valid_london_coords(lat, lng):
                return lat, lng
        except ValueError:
            continue
    return None


def _valid_london_coords(lat: float, lng: float) -> bool:
    """Rough Greater London bounding box check."""
    return 51.2 <= lat <= 51.8 and -0.6 <= lng <= 0.4
