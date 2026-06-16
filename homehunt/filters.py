"""
Hard-filter pre-stage for the rental-engine pipeline (Wave 2 Agent F).

Reads `hard_filters` from `filter-scoring-config.yaml` (via the unified config
loader in homehunt.config_loader) and applies the filter set to a list of
duck-typed listing objects, returning the survivors plus a per-filter
drop count for structured logging.

Design: docs/plans/2026-05-04-unified-filter-scoring-config.md (Section 3).

Filters applied in order:
  1. price (price_pcm in pounds-per-month, derived from price_numeric / 100
     when only price_numeric is available)
  2. bedrooms
  3. epc (rating letter, A best to G worst; null passes when epc_drop_null
     is False)
  4. tfl_zone (null passes; >= tfl_zone_max+1 drops)
  5. size_sqft (null passes; below size_sqft_min drops)
  6. region (allowlist; null and 'Other' both drop)

The filter assumes carpet_other_areas is a soft scoring signal (decision
2026-05-04). It is intentionally NOT enforced here.
"""

from typing import Any, Iterable, Optional

from homehunt.config_loader import load_hard_filters as _load_hard_filters


# Re-export so callers can `from homehunt.filters import load_hard_filters`.
load_hard_filters = _load_hard_filters

# EPC rating order from best to worst. A listing's rating is acceptable if its
# index is at or below the floor letter's index.
_EPC_ORDER = ["A", "B", "C", "D", "E", "F", "G"]


def _get_price_pcm(listing: Any) -> Optional[int]:
    """
    Return the listing's price in pounds-per-month, or None.

    Accepts either:
      - listing.price_pcm  (int/float, pounds-per-month)
      - listing.price_numeric  (int, pence-per-month from the Listing ORM row)
    """
    val = getattr(listing, "price_pcm", None)
    if val is not None:
        return int(val)
    pence = getattr(listing, "price_numeric", None)
    if pence is not None:
        return int(pence) // 100
    return None


def _attr(listing: Any, name: str) -> Any:
    """Read an attribute or key, supporting both objects and dicts."""
    if isinstance(listing, dict):
        return listing.get(name)
    return getattr(listing, name, None)


def apply_hard_filters(
    listings: Iterable[Any],
    hard_filters: dict,
) -> tuple[list, dict[str, int]]:
    """
    Apply the hard filters block to an iterable of listings.

    Returns (surviving_listings, drop_counts) where drop_counts is a dict
    keyed by filter name with the count of listings that this filter (and
    only this filter, given the sequential application order) eliminated.
    """
    survivors = list(listings)
    drops: dict[str, int] = {}

    # 1. Price (pounds per calendar month)
    price_block = hard_filters.get("price", {}) or {}
    price_min = price_block.get("min")
    price_max = price_block.get("max")
    if price_min is not None or price_max is not None:
        before = len(survivors)
        new_survivors = []
        for listing in survivors:
            price = _get_price_pcm(listing)
            if price is None:
                # No price means the listing is not actionable -- drop it.
                continue
            if price_min is not None and price < price_min:
                continue
            if price_max is not None and price > price_max:
                continue
            new_survivors.append(listing)
        drops["price"] = before - len(new_survivors)
        survivors = new_survivors

    # 2. Bedrooms
    bed_block = hard_filters.get("bedrooms", {}) or {}
    bed_min = bed_block.get("min")
    bed_max = bed_block.get("max")
    if bed_min is not None or bed_max is not None:
        before = len(survivors)
        new_survivors = []
        for listing in survivors:
            beds = _attr(listing, "bedrooms")
            if beds is None:
                # No bedroom count means we cannot judge -- drop it conservatively.
                continue
            if bed_min is not None and beds < bed_min:
                continue
            if bed_max is not None and beds > bed_max:
                continue
            new_survivors.append(listing)
        drops["bedrooms"] = before - len(new_survivors)
        survivors = new_survivors

    # 3. EPC
    epc_floor = hard_filters.get("epc_min_rating")
    epc_drop_null = bool(hard_filters.get("epc_drop_null", False))
    if epc_floor:
        floor_letter = epc_floor.upper()
        if floor_letter not in _EPC_ORDER:
            raise ValueError(
                f"Invalid epc_min_rating in config: {epc_floor!r}. "
                f"Expected one of {_EPC_ORDER}."
            )
        floor_idx = _EPC_ORDER.index(floor_letter)
        before = len(survivors)
        new_survivors = []
        for listing in survivors:
            rating = _attr(listing, "epc_rating")
            if rating is None:
                if epc_drop_null:
                    continue
                new_survivors.append(listing)
                continue
            up = str(rating).upper()
            if up not in _EPC_ORDER:
                # Unknown letter -- treat as null (do not drop unless flagged).
                if epc_drop_null:
                    continue
                new_survivors.append(listing)
                continue
            if _EPC_ORDER.index(up) <= floor_idx:
                new_survivors.append(listing)
        drops["epc"] = before - len(new_survivors)
        survivors = new_survivors

    # 4. TfL zone
    zone_max = hard_filters.get("tfl_zone_max")
    if zone_max is not None:
        before = len(survivors)
        new_survivors = []
        for listing in survivors:
            zone = _attr(listing, "tfl_zone")
            if zone is None:
                # Null passes (TfL enrichment can fail).
                new_survivors.append(listing)
                continue
            try:
                zone_int = int(zone)
            except (TypeError, ValueError):
                new_survivors.append(listing)
                continue
            if zone_int <= zone_max:
                new_survivors.append(listing)
        drops["tfl_zone"] = before - len(new_survivors)
        survivors = new_survivors

    # 5. Size sqft
    size_min = hard_filters.get("size_sqft_min")
    if size_min is not None:
        before = len(survivors)
        new_survivors = []
        for listing in survivors:
            size = _attr(listing, "size_sqft")
            if size is None:
                # Null passes (size enrichment via vision is still maturing).
                new_survivors.append(listing)
                continue
            try:
                if int(size) >= size_min:
                    new_survivors.append(listing)
            except (TypeError, ValueError):
                new_survivors.append(listing)
        drops["size_sqft"] = before - len(new_survivors)
        survivors = new_survivors

    # 6. Let-agreed title flag (added 2026-05-10 from carpet TDD calibration).
    # Drops listings whose title contains let-agreed / under-offer markers.
    # Currently produces near-zero drops because the Rightmove and Zoopla
    # scrapers do not consistently surface these badges in the title; kept
    # so future scraper improvements (which will populate the field) take
    # effect without code changes.
    if hard_filters.get("drop_let_agreed", False):
        before = len(survivors)
        new_survivors = [
            listing for listing in survivors
            if not bool(_attr(listing, "is_let_agreed"))
        ]
        drops["let_agreed"] = before - len(new_survivors)
        survivors = new_survivors

    # 7. Region allowlist (only enforced when region_drop_unlisted is true).
    # Default 2026-05-08: region is a soft signal, not a hard filter. NULL and
    # 'Other' regions remain in the DB and rank low via scoring.components.
    # region_tier; the hard drop is opt-in via region_drop_unlisted: true.
    regions_allowed = hard_filters.get("regions")
    drop_unlisted = hard_filters.get("region_drop_unlisted", False)
    if regions_allowed is not None and drop_unlisted:
        allowed = set(regions_allowed)
        before = len(survivors)
        new_survivors = [
            listing for listing in survivors
            if _attr(listing, "region") in allowed
        ]
        drops["region"] = before - len(new_survivors)
        survivors = new_survivors
    else:
        drops["region"] = 0

    return survivors, drops
