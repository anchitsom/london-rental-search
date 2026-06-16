"""
Zoopla search URL builder.

Mirrors the interface of the Rightmove URL helpers so a single
enabled_portals list can drive both portals from one SearchConfig.
"""

from typing import Optional
from urllib.parse import quote, urlencode

from homehunt.core.models import SearchConfig

_BASE = "https://www.zoopla.co.uk"
_SEARCH_PATH = "/to-rent/property"


def _slug(location: str) -> str:
    return location.strip().lower().split(",")[0].replace(" ", "-")


def build_zoopla_search_url(
    location: str,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    min_bedrooms: Optional[int] = None,
    max_bedrooms: Optional[int] = None,
    page: int = 1,
) -> str:
    """Build a Zoopla to-rent search URL for the given location and filters."""
    slug = _slug(location)
    params: dict = {
        "price_frequency": "per_month",
        "q": location,
        "search_source": "to-rent",
    }
    if min_price is not None:
        params["price_min"] = str(min_price)
    if max_price is not None:
        params["price_max"] = str(max_price)
    if min_bedrooms is not None:
        params["beds_min"] = str(min_bedrooms)
    if max_bedrooms is not None:
        params["beds_max"] = str(max_bedrooms)
    if page > 1:
        params["pn"] = str(page)
    qs = urlencode(params, quote_via=quote)
    return f"{_BASE}{_SEARCH_PATH}/{slug}/?{qs}"


class ZooplaURLBuilder:
    """Builds Zoopla search URLs from a SearchConfig."""

    def build(self, config: SearchConfig, page: int = 1) -> str:
        return build_zoopla_search_url(
            location=config.location,
            min_price=config.min_price,
            max_price=config.max_price,
            min_bedrooms=config.min_bedrooms,
            max_bedrooms=config.max_bedrooms,
            page=page,
        )
