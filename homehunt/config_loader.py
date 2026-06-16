"""
Load and validate london-search.yaml, returning a flat list of search dicts
ready for discover_properties().

Schema (v2.0):
  defaults:    shared numeric search parameters
  profiles[]:  named groups of locations
    locations[]:  each has name (str) and region_id (str, must start REGION^)
    overrides:   optional dict that shadows any default key

Wave 2 Agent F additions:
  load_unified_config(path)  -> full dict for filter-scoring-config.yaml
  load_hard_filters(path)    -> hard_filters block
  load_scoring_config(path)  -> scoring block (components + curve params)

The unified config is the single source of truth for hard filters, scoring
weights and tier-to-name mapping. See
docs/plans/2026-05-04-unified-filter-scoring-config.md for the design.
"""

import os
from typing import Any

import yaml


_PLACEHOLDER_VALUES = {"", "TODO", "todo", "FIXME", "fixme", None}


def _is_valid_region_id(value: Any) -> bool:
    # Rightmove's locationIdentifier accepts both REGION^NNNN (named areas)
    # and STATION^NNNN (tube/overground/rail stops). Some London neighbourhoods
    # (Seven Sisters, Blackhorse Road) have only a STATION typeahead match.
    if not isinstance(value, str) or len(value) <= 7:
        return False
    return value.startswith("REGION^") or value.startswith("STATION^")


def _scrape_bounds_from_unified_config() -> dict:
    """
    Pull min_price/max_price/min_bedrooms/max_bedrooms from the unified
    filter-scoring-config.yaml so Rightmove's query layer agrees with the
    local hard filter. Returns an empty dict if the unified config is
    missing or unreadable; the caller's per-key defaults will then apply.
    """
    try:
        cfg = load_unified_config()
    except FileNotFoundError:
        return {}
    hf = cfg.get("hard_filters") or {}
    bounds: dict = {}
    price = hf.get("price") or {}
    if "min" in price:
        bounds["min_price"] = price["min"]
    if "max" in price:
        bounds["max_price"] = price["max"]
    beds = hf.get("bedrooms") or {}
    if "min" in beds:
        bounds["min_bedrooms"] = beds["min"]
    if "max" in beds:
        bounds["max_bedrooms"] = beds["max"]
    return bounds


def load_search_profiles(yaml_path: str) -> list[dict]:
    """
    Load yaml_path and return one dict per location across all profiles.

    Each dict contains:
      profile       str    profile name
      location      str    human location name
      region_id     str    Rightmove locationIdentifier (REGION^...)
      min_price     int    (Wave 2 Agent F: falls back to filter-scoring-config.yaml)
      max_price     int    (Wave 2 Agent F: falls back to filter-scoring-config.yaml)
      min_bedrooms  int    (Wave 2 Agent F: falls back to filter-scoring-config.yaml)
      max_bedrooms  int    (Wave 2 Agent F: falls back to filter-scoring-config.yaml)
      radius        float
      max_results   int
      page_delay    float
      scrape_delay  float

    Wave 2 Agent F: min_price, max_price, min_bedrooms and max_bedrooms have
    moved to filter-scoring-config.yaml (under hard_filters). When the search
    yaml's defaults block does not supply them, the unified config is read
    so the Rightmove query stays aligned with the local hard filter.

    Raises:
      FileNotFoundError  if yaml_path does not exist
      ValueError         if any location has a missing or invalid region_id
    """
    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Search config not found: {yaml_path}")

    defaults: dict = data.get("defaults", {})
    profiles_raw: list = data.get("profiles", [])

    # Pull scrape-side bounds from the unified config when they are not in
    # the search yaml. The search yaml's `defaults` block still wins when
    # present so per-environment profiles can override.
    unified_bounds = _scrape_bounds_from_unified_config()

    results: list[dict] = []

    for profile in profiles_raw:
        profile_name: str = profile.get("name", "")
        overrides: dict = profile.get("overrides", {})

        # Merge: unified-config < search-yaml defaults < profile overrides.
        merged = {**unified_bounds, **defaults, **overrides}

        for loc in profile.get("locations", []):
            loc_name: str = loc.get("name", "")
            region_id: Any = loc.get("region_id")

            if region_id in _PLACEHOLDER_VALUES or not _is_valid_region_id(region_id):
                raise ValueError(
                    f"Location '{loc_name}' in profile '{profile_name}' is missing a valid "
                    f"region_id (got {region_id!r}). "
                    f"Run scripts/resolve_region_ids.py to populate it."
                )

            results.append(
                {
                    "profile": profile_name,
                    "location": loc_name,
                    "region_id": str(region_id),
                    "min_price": int(merged.get("min_price", 0)),
                    "max_price": int(merged.get("max_price", 999999)),
                    "min_bedrooms": int(merged.get("min_bedrooms", 1)),
                    "max_bedrooms": int(merged.get("max_bedrooms", 2)),
                    "radius": float(merged.get("radius", 1.0)),
                    "max_results": int(merged.get("max_results", 40)),
                    "page_delay": float(merged.get("page_delay", 2.0)),
                    "scrape_delay": float(merged.get("scrape_delay", 1.0)),
                }
            )

    return results


_DEFAULT_OPENRENT_PROFILE = {
    "centroids": [(51.5326, -0.1058)],
    "radius_km": 2.0,
}


def load_openrent_profiles(yaml_path: str) -> list[dict]:
    """Return a list of OpenRent search profiles from the yaml.

    Reads the top-level `openrent_profiles` key. Each entry becomes a dict with
    at minimum `centroids` (list of (lat, lng) tuples) and `radius_km` (float).
    Returns a single-element default list (Angel centroid, 2 km) when the key
    is absent so callers always get a usable profile.
    """
    try:
        with open(yaml_path) as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return [dict(_DEFAULT_OPENRENT_PROFILE)]

    profiles = data.get("openrent_profiles")
    if profiles is None:
        return [dict(_DEFAULT_OPENRENT_PROFILE)]
    if not isinstance(profiles, list):
        raise ValueError("openrent_profiles must be a list")

    result = []
    for p in profiles:
        entry = dict(p)
        raw_centroids = entry.get("centroids") or _DEFAULT_OPENRENT_PROFILE["centroids"]
        entry["centroids"] = [(float(c[0]), float(c[1])) for c in raw_centroids]
        entry["radius_km"] = float(entry.get("radius_km", _DEFAULT_OPENRENT_PROFILE["radius_km"]))
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Unified filter and scoring config loaders (Wave 2 Agent F)
# ---------------------------------------------------------------------------

# Default location: filter-scoring-config.yaml at the project root,
# i.e. one directory above this module.
_DEFAULT_UNIFIED_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "filter-scoring-config.yaml",
)


def load_unified_config(yaml_path: str = _DEFAULT_UNIFIED_CONFIG_PATH) -> dict:
    """
    Load the canonical filter-scoring-config.yaml and return the parsed dict.

    The dict has three top-level keys: hard_filters, scoring, regions_by_tier.
    See docs/plans/2026-05-04-unified-filter-scoring-config.sample.yaml for
    the schema.

    Raises FileNotFoundError if yaml_path does not exist.
    """
    try:
        with open(yaml_path) as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise FileNotFoundError(f"Unified config not found: {yaml_path}")
    return data


def load_hard_filters(yaml_path: str = _DEFAULT_UNIFIED_CONFIG_PATH) -> dict:
    """
    Return the hard_filters block from the unified config.

    The block includes: price.min, price.max, bedrooms.min, bedrooms.max,
    epc_min_rating (single letter), epc_drop_null (bool), tfl_zone_max,
    size_sqft_min, regions (allowlist).
    """
    return load_unified_config(yaml_path).get("hard_filters", {})


def load_scoring_config(yaml_path: str = _DEFAULT_UNIFIED_CONFIG_PATH) -> dict:
    """
    Return the scoring block from the unified config.

    Has one sub-key, components, mapping component name to its dict
    (weight, curve params, score map, null_score, etc).
    """
    return load_unified_config(yaml_path).get("scoring", {})


def load_regions_by_tier(yaml_path: str = _DEFAULT_UNIFIED_CONFIG_PATH) -> dict:
    """
    Return the regions_by_tier block (tier_1 / tier_2 / tier_3 -> list of names).
    """
    return load_unified_config(yaml_path).get("regions_by_tier", {})
