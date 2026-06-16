"""
Weighted scoring engine.
Takes a PropertyListing (optionally enriched with carpet and EPC data)
and returns a 0-100 total score plus a per-criterion breakdown.

Wave 2 Agent F (2026-05-04 redesign): hard filter thresholds and scoring
curve parameters now live in `filter-scoring-config.yaml` at the project
root. The runtime weight source order is unchanged: most recent row in the
`scoring_weights` table wins; on a table miss the seed weights are read
from the unified yaml. The constants `BUDGET_MAX`, `BUDGET_HARD_ZERO`, and
`SIZE_MIN_SQFT` plus the in-code `_SEED` dicts are gone.

Region preference scoring is read from the unified config's regions_by_tier
block (folded in from tiers.yaml). The legacy `tiers_yaml_path` kwarg on
`_score_region_pref` is preserved for backward compatibility with existing
tests; when provided, it falls back to the old tiers.yaml schema.

Carpet was demoted from a hard filter to a soft signal split across two
components: `carpet_bedroom` and `carpet_other_areas`, weighted 0.10 each.
The legacy `carpet_detected` parameter still produces a single `carpet`
breakdown key for backward compatibility with the previous scorer surface.
"""

import json
import os
import sqlite3
from typing import Optional

import yaml

ALERT_THRESHOLD = 60      # minimum score to trigger a Telegram alert

# Default unified config path: project root (one directory above this module).
_DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "filter-scoring-config.yaml",
)

# Default tiers.yaml path: kept for backward compatibility with callers that
# still pass tiers_yaml_path. tiers.yaml is deprecated by the unified config.
_DEFAULT_TIERS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tiers.yaml",
)

# Default db path: homehunt.db in the project root.
_DEFAULT_DB_PATH = os.environ.get("HOMEHUNT_DB", "homehunt.db")


# ---------------------------------------------------------------------------
# Unified config loader (cached)
# ---------------------------------------------------------------------------

_CONFIG_CACHE: dict[str, dict] = {}


def _load_config(config_path: str = _DEFAULT_CONFIG_PATH) -> dict:
    """
    Load and cache the unified filter-scoring-config.yaml. Returns an empty
    dict on read failure so legacy callers do not break when the file is
    missing (the fallback behaviour mirrors the pre-refactor seed values).
    """
    if config_path in _CONFIG_CACHE:
        return _CONFIG_CACHE[config_path]
    try:
        with open(config_path, "r") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        data = {}
    _CONFIG_CACHE[config_path] = data
    return data


def _yaml_seed_weights(config_path: str = _DEFAULT_CONFIG_PATH) -> dict:
    """
    Build the seed weights dict from the unified config's
    scoring.components[*].weight values.

    Falls back to the pre-refactor numeric defaults when the config cannot
    be read; the totals match the original scorer.py constants exactly so
    existing fixtures remain numerically identical.
    """
    cfg = _load_config(config_path)
    components = (cfg.get("scoring") or {}).get("components") or {}
    if components:
        return {k: v.get("weight", 0.0) for k, v in components.items()}
    # Pre-refactor defaults (single 'carpet' key).
    return {
        "price": 0.25,
        "carpet": 0.20,
        "size": 0.15,
        "bedrooms": 0.15,
        "epc": 0.10,
        "transport": 0.10,
        "outside": 0.05,
    }


# ---------------------------------------------------------------------------
# Weight loading
# ---------------------------------------------------------------------------

def load_active_weights(
    db_path: str = _DEFAULT_DB_PATH,
    config_path: str = _DEFAULT_CONFIG_PATH,
) -> dict:
    """
    Read the most recent row from scoring_weights and return the weights dict.
    On a table miss (or any DB error) falls back to the unified yaml seed.
    """
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT weights_json FROM scoring_weights ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row:
            return json.loads(row[0])
    except Exception:
        pass
    return _yaml_seed_weights(config_path)


# ---------------------------------------------------------------------------
# Region preference scoring
# ---------------------------------------------------------------------------

def _score_region_pref(
    region: Optional[str],
    config_path: str = _DEFAULT_CONFIG_PATH,
    tiers_yaml_path: Optional[str] = None,
) -> int:
    """
    Score a listing's region against the tier map.

    Source order:
      1. If `tiers_yaml_path` is provided, read the legacy tiers.yaml schema
         (list of tier blocks with locations[]). Kept for backward compatibility
         with existing tests.
      2. Otherwise read `regions_by_tier` and `scoring.components.region_tier`
         from the unified filter-scoring-config.yaml.

    Rules (unchanged from pre-refactor):
      - tier 1 location: 100
      - tier 2 location: 75
      - tier 3 location: 50
      - region not in any tier: 75 (quiet middle default)
      - region None: 0
    """
    if not region:
        return 0

    # Legacy path: read tiers.yaml directly when explicitly requested.
    if tiers_yaml_path is not None:
        try:
            with open(tiers_yaml_path, "r") as fh:
                tiers_data = yaml.safe_load(fh)
        except Exception:
            return 75
        name_to_tier: dict[str, int] = {}
        for tier_block in tiers_data or []:
            tier_name = tier_block.get("name", "")
            if "tier 1" in tier_name.lower():
                tier_num = 1
            elif "tier 2" in tier_name.lower():
                tier_num = 2
            elif "tier 3" in tier_name.lower():
                tier_num = 3
            else:
                continue
            for loc in tier_block.get("locations", []):
                name_to_tier[loc["name"]] = tier_num
        tier_num = name_to_tier.get(region)
        if tier_num == 1:
            return 100
        if tier_num == 2:
            return 75
        if tier_num == 3:
            return 50
        return 75

    # Unified config path.
    cfg = _load_config(config_path)
    tiers = cfg.get("regions_by_tier") or {}
    rt = ((cfg.get("scoring") or {}).get("components") or {}).get("region_tier") or {}

    if region in (tiers.get("tier_1") or []):
        return int(rt.get("tier_1", 100))
    if region in (tiers.get("tier_2") or []):
        return int(rt.get("tier_2", 75))
    if region in (tiers.get("tier_3") or []):
        return int(rt.get("tier_3", 50))
    return int(rt.get("unlisted", 75))


# ---------------------------------------------------------------------------
# Per-criterion sub-scorers
# ---------------------------------------------------------------------------

def _score_price(price_pcm: Optional[int], config_path: str = _DEFAULT_CONFIG_PATH) -> int:
    """Price in pounds per calendar month."""
    if price_pcm is None or price_pcm <= 0:
        return 0
    cfg = _load_config(config_path)
    price_block = ((cfg.get("scoring") or {}).get("components") or {}).get("price") or {}
    full_at = int(price_block.get("full_marks_at_or_below", 2800))
    zero_at = int(price_block.get("zero_marks_at_or_above", 3200))
    if price_pcm <= full_at:
        return 100
    if price_pcm >= zero_at:
        return 0
    return round(100 * (zero_at - price_pcm) / (zero_at - full_at))


def _score_carpet(carpet_detected: Optional[bool], confidence: float) -> int:
    """
    Legacy single-signal carpet score (used when the caller still passes
    carpet_detected as a boolean rather than the per-area flags).

    Unchanged from the pre-refactor scorer so existing test fixtures
    produce identical numerical scores.
    """
    if carpet_detected is None:
        return 50
    if carpet_detected:
        return round(50 + 50 * confidence)
    return round(50 - 50 * confidence)


def _score_carpet_binary(
    carpet_detected: Optional[bool],
    component_block: dict,
) -> int:
    """Binary carpet score: detected is bad, not detected is full marks."""
    if carpet_detected is None:
        return int(component_block.get("null_score", 50))
    if carpet_detected:
        return int(component_block.get("detected_score", 20))
    return int(component_block.get("not_detected_score", 100))


def _score_carpet_split(
    detected: Optional[bool],
    component_block: dict,
) -> int:
    """
    Per-area carpet score using the unified config's
    scoring.components.carpet_bedroom or carpet_other_areas block.

    True  -> detected_score      (0 for other_areas, 30 for bedroom by default)
    False -> not_detected_score  (100)
    None  -> null_score          (50)
    """
    if detected is None:
        return int(component_block.get("null_score", 50))
    if detected:
        return int(component_block.get("detected_score", 30))
    return int(component_block.get("not_detected_score", 100))


def _score_carpet_count(
    bedrooms_with_carpet: Optional[int],
    has_living_area_carpet: Optional[bool],
    component_block: dict,
) -> int:
    """
    Three-state carpet score introduced 2026-05-10 after the carpet TDD
    calibration. Reads bedrooms_with_carpet (int) and has_living_area_carpet
    (bool) from the two-step detector, applies the user's stated preference:

        carpet in 1 bedroom   -> acceptable    (75 by default)
        carpet in 2 bedrooms  -> very low      (20 by default)
        no carpet             -> full marks    (100)
        living-area carpet    -> override to 0 regardless of bedroom count

    Both flags null -> null_score (50).
    """
    if bedrooms_with_carpet is None and has_living_area_carpet is None:
        return int(component_block.get("null_score", 50))

    if has_living_area_carpet:
        return int(component_block.get("has_living_area_carpet_override", 0))

    scores = component_block.get("bedrooms_with_carpet_score") or {}
    if bedrooms_with_carpet is None:
        return int(component_block.get("null_score", 50))
    if bedrooms_with_carpet in scores:
        return int(scores[bedrooms_with_carpet])
    if str(bedrooms_with_carpet) in scores:
        return int(scores[str(bedrooms_with_carpet)])
    # Cap at the highest defined key for any count above the table.
    int_keys = sorted([k for k in scores.keys() if isinstance(k, int)])
    if int_keys:
        return int(scores[int_keys[-1]])
    return 50


def _score_size(size_sqft: Optional[int], config_path: str = _DEFAULT_CONFIG_PATH) -> int:
    cfg = _load_config(config_path)
    size_block = ((cfg.get("scoring") or {}).get("components") or {}).get("size") or {}
    full_at = int(size_block.get("full_marks_at_or_above_sqft", 650))
    zero_at = int(size_block.get("zero_marks_at_or_below_sqft", 400))
    null_score = int(size_block.get("null_score", 50))

    if size_sqft is None:
        return null_score
    if size_sqft >= full_at:
        bonus = min(20, round((size_sqft - full_at) / 10))
        return min(100, 100 + bonus)
    if size_sqft <= zero_at:
        return 0
    span = full_at - zero_at
    if span <= 0:
        return 100
    return round(100 * (size_sqft - zero_at) / span)


def _score_bedrooms(bedrooms: Optional[int], config_path: str = _DEFAULT_CONFIG_PATH) -> int:
    cfg = _load_config(config_path)
    bed_block = ((cfg.get("scoring") or {}).get("components") or {}).get("bedrooms") or {}
    if bedrooms is None:
        return int(bed_block.get("null_score", 0))
    scores = bed_block.get("scores") or {}
    # YAML keys parse as ints already; defend against str keys in case of
    # quoted yaml.
    if bedrooms in scores:
        return int(scores[bedrooms])
    if str(bedrooms) in scores:
        return int(scores[str(bedrooms)])
    # Fallback to pre-refactor table: 2+ -> 100, 1 -> 50, 0 -> 0.
    if bedrooms >= 2:
        return 100
    if bedrooms == 1:
        return 50
    return 0


def _score_epc(rating: Optional[str], config_path: str = _DEFAULT_CONFIG_PATH) -> int:
    """Read the EPC score map from the unified config."""
    cfg = _load_config(config_path)
    epc_block = ((cfg.get("scoring") or {}).get("components") or {}).get("epc") or {}
    if rating is None:
        return int(epc_block.get("null_score", 0))
    scores = epc_block.get("scores") or {}
    return int(scores.get(rating.upper(), 50))


def _score_transport(
    commute_mins: Optional[int],
    tfl_zone: Optional[int] = None,
    config_path: str = _DEFAULT_CONFIG_PATH,
) -> int:
    """
    Public transport commute time (primary) + TfL zone (secondary tiebreaker).

    Reads thresholds from the unified config's
    scoring.components.transport block. Falls back to pre-refactor numeric
    defaults if the config block is missing.
    """
    cfg = _load_config(config_path)
    transport_block = ((cfg.get("scoring") or {}).get("components") or {}).get("transport") or {}
    journey_minutes = transport_block.get("journey_minutes") or [
        {"at_or_below": 15, "score": 100},
        {"at_or_below": 25, "score": 80},
        {"at_or_below": 35, "score": 60},
        {"at_or_below": 45, "score": 30},
        {"above": 45, "score": 0},
    ]
    zone_scores = transport_block.get("zone_scores") or {1: 100, 2: 100, 3: 50, 4: 0}
    blend = float(transport_block.get("journey_zone_blend", 0.75))
    null_score = int(transport_block.get("null_score", 50))

    if commute_mins is not None:
        journey_score = None
        for tier in journey_minutes:
            if "at_or_below" in tier and commute_mins <= int(tier["at_or_below"]):
                journey_score = int(tier["score"])
                break
        if journey_score is None:
            # The 'above' fallback row, or no row matched.
            journey_score = 0
            for tier in journey_minutes:
                if "above" in tier:
                    journey_score = int(tier["score"])
                    break
    else:
        if tfl_zone is None:
            return null_score
        journey_score = None

    if tfl_zone is not None:
        # zone_scores keys may be int or str depending on yaml; defend both.
        zone_int_score = zone_scores.get(tfl_zone)
        if zone_int_score is None:
            zone_int_score = zone_scores.get(str(tfl_zone))
        if zone_int_score is None:
            zone_int_score = 0 if tfl_zone >= 4 else 50
        zone_score = int(zone_int_score)
    else:
        zone_score = 50

    if journey_score is None:
        return zone_score

    return round(journey_score * blend + zone_score * (1.0 - blend))


def _score_outside(
    garden: Optional[bool],
    balcony: Optional[bool],
    config_path: str = _DEFAULT_CONFIG_PATH,
) -> int:
    cfg = _load_config(config_path)
    outside_block = ((cfg.get("scoring") or {}).get("components") or {}).get("outside") or {}
    scores = outside_block.get("scores") or {"garden": 100, "balcony": 60, "none": 0}
    if garden:
        return int(scores.get("garden", 100))
    if balcony:
        return int(scores.get("balcony", 60))
    return int(scores.get("none", 0))


# ---------------------------------------------------------------------------
# Core scoring entry points
# ---------------------------------------------------------------------------

def compute_score(
    price_pcm: Optional[int] = None,
    size_sqft: Optional[int] = None,
    bedrooms: Optional[int] = None,
    carpet_detected: Optional[bool] = None,
    carpet_confidence: float = 0.0,
    carpet_in_bedroom: Optional[bool] = None,
    carpet_other_areas: Optional[bool] = None,
    bedrooms_with_carpet: Optional[int] = None,
    has_living_area_carpet: Optional[bool] = None,
    epc_rating: Optional[str] = None,
    commute_mins: Optional[int] = None,
    tfl_zone: Optional[int] = None,
    garden: Optional[bool] = None,
    balcony: Optional[bool] = None,
    weights: Optional[dict] = None,
    config_path: str = _DEFAULT_CONFIG_PATH,
) -> tuple[int, dict[str, int]]:
    """
    Compute a 0-100 score for a listing using the seven non-region components.

    Carpet handling:
      - When `carpet_in_bedroom` or `carpet_other_areas` is provided, the
        scorer uses the per-area split components from the unified config
        (carpet_bedroom and carpet_other_areas, weight 0.10 each).
      - Otherwise it falls back to the legacy single-signal `carpet_detected`
        boolean and produces a single `carpet` breakdown key (preserving the
        pre-refactor numerical surface for existing test fixtures).

    Returns:
        (total_score, breakdown_dict)
    """
    if weights is None:
        weights = _yaml_seed_weights(config_path)

    cfg = _load_config(config_path)
    components_cfg = (cfg.get("scoring") or {}).get("components") or {}

    breakdown: dict[str, int] = {
        "price": _score_price(price_pcm, config_path=config_path),
        "size": _score_size(size_sqft, config_path=config_path),
        "bedrooms": _score_bedrooms(bedrooms, config_path=config_path),
        "epc": _score_epc(epc_rating, config_path=config_path),
        "transport": _score_transport(commute_mins, tfl_zone, config_path=config_path),
        "outside": _score_outside(garden, balcony, config_path=config_path),
    }

    # Carpet path selection priority:
    #   1. Active binary path whenever the single carpet_detected signal is
    #      supplied, or when no room-area fields are supplied.
    #   2. Older count/split paths remain available for legacy rows/tests but
    #      are no longer wired from production enrichment.
    new_signal_present = bedrooms_with_carpet is not None or has_living_area_carpet is not None
    legacy_signal_present = carpet_detected is not None
    split_signal_present = (
        carpet_in_bedroom is not None or carpet_other_areas is not None
    )
    new_weight_present = weights.get("carpet", 0.0) > 0.0
    split_weight_present = (
        weights.get("carpet_bedroom", 0.0) > 0.0
        or weights.get("carpet_other_areas", 0.0) > 0.0
    )

    if legacy_signal_present or not (new_signal_present or split_signal_present):
        carpet_block = components_cfg.get("carpet") or {}
        breakdown["carpet"] = _score_carpet_binary(carpet_detected, carpet_block)
    elif new_signal_present and (new_weight_present or not split_signal_present):
        carpet_block = components_cfg.get("carpet") or {}
        breakdown["carpet"] = _score_carpet_count(
            bedrooms_with_carpet, has_living_area_carpet, carpet_block
        )
    elif split_signal_present and not legacy_signal_present:
        bed_block = components_cfg.get("carpet_bedroom") or {}
        other_block = components_cfg.get("carpet_other_areas") or {}
        breakdown["carpet_bedroom"] = _score_carpet_split(carpet_in_bedroom, bed_block)
        breakdown["carpet_other_areas"] = _score_carpet_split(carpet_other_areas, other_block)
    else:
        carpet_block = components_cfg.get("carpet") or {}
        breakdown["carpet"] = _score_carpet_binary(carpet_detected, carpet_block)

    total = round(sum(breakdown[k] * weights.get(k, 0.0) for k in breakdown))
    return total, breakdown


def compute_score_from_db(
    db_path: str = _DEFAULT_DB_PATH,
    price_pcm: Optional[int] = None,
    size_sqft: Optional[int] = None,
    bedrooms: Optional[int] = None,
    carpet_detected: Optional[bool] = None,
    carpet_confidence: float = 0.0,
    carpet_in_bedroom: Optional[bool] = None,
    carpet_other_areas: Optional[bool] = None,
    bedrooms_with_carpet: Optional[int] = None,
    has_living_area_carpet: Optional[bool] = None,
    epc_rating: Optional[str] = None,
    commute_mins: Optional[int] = None,
    tfl_zone: Optional[int] = None,
    garden: Optional[bool] = None,
    balcony: Optional[bool] = None,
    region: Optional[str] = None,
    config_path: str = _DEFAULT_CONFIG_PATH,
    tiers_yaml_path: Optional[str] = None,
) -> tuple[int, dict[str, int]]:
    """
    Full scoring entry point. Loads weights from the scoring_weights table
    (most recent row), scores each component, adds region_pref, returns
    the total and a score_breakdown dict suitable for persisting as JSON.
    """
    weights = load_active_weights(db_path=db_path, config_path=config_path)
    region_pref_weight = weights.get("region_pref", weights.get("region_tier", 0.0))

    # Determine which non-region keys to apply weights to. Includes both
    # legacy (`carpet`) and split (`carpet_bedroom`, `carpet_other_areas`)
    # so a weights row of either shape produces the right total.
    non_region_keys = [
        "price", "carpet", "carpet_bedroom", "carpet_other_areas",
        "size", "bedrooms", "epc", "transport", "outside",
    ]

    _, partial_breakdown = compute_score(
        price_pcm=price_pcm,
        size_sqft=size_sqft,
        bedrooms=bedrooms,
        carpet_detected=carpet_detected,
        carpet_confidence=carpet_confidence,
        carpet_in_bedroom=carpet_in_bedroom,
        carpet_other_areas=carpet_other_areas,
        bedrooms_with_carpet=bedrooms_with_carpet,
        has_living_area_carpet=has_living_area_carpet,
        epc_rating=epc_rating,
        commute_mins=commute_mins,
        tfl_zone=tfl_zone,
        garden=garden,
        balcony=balcony,
        weights={k: weights.get(k, 0.0) for k in non_region_keys},
        config_path=config_path,
    )

    region_score = _score_region_pref(
        region,
        config_path=config_path,
        tiers_yaml_path=tiers_yaml_path,
    )

    breakdown = dict(partial_breakdown)
    breakdown["region_pref"] = region_score

    total = round(
        sum(partial_breakdown[k] * weights.get(k, 0.0) for k in partial_breakdown)
        + region_score * region_pref_weight
    )
    return total, breakdown


def score_listing(
    listing,
    db_path: str = _DEFAULT_DB_PATH,
    config_path: str = _DEFAULT_CONFIG_PATH,
    tiers_yaml_path: Optional[str] = None,
) -> tuple[int, dict[str, int]]:
    """
    Convenience wrapper that accepts a PropertyListing or dict.
    Extracts the relevant fields and calls compute_score_from_db.
    Also writes score_breakdown JSON back to the listing object when possible.
    """
    if hasattr(listing, "__dict__") or hasattr(listing, "model_fields"):
        price_pcm = getattr(listing, "price_numeric", None)
        if price_pcm is not None:
            price_pcm = price_pcm // 100  # stored in pence

        total, breakdown = compute_score_from_db(
            db_path=db_path,
            price_pcm=price_pcm,
            size_sqft=getattr(listing, "size_sqft", None),
            bedrooms=getattr(listing, "bedrooms", None),
            carpet_detected=getattr(listing, "carpet_detected", None),
            carpet_confidence=getattr(listing, "carpet_confidence", 0.0) or 0.0,
            carpet_in_bedroom=getattr(listing, "carpet_in_bedroom", None),
            carpet_other_areas=getattr(listing, "carpet_other_areas", None),
            bedrooms_with_carpet=getattr(listing, "bedrooms_with_carpet", None),
            has_living_area_carpet=getattr(listing, "has_living_area_carpet", None),
            epc_rating=getattr(listing, "epc_rating", None),
            commute_mins=getattr(listing, "commute_canary_wharf", None) or getattr(listing, "commute_whitechapel", None),
            tfl_zone=getattr(listing, "tfl_zone", None),
            garden=getattr(listing, "garden", None),
            balcony=getattr(listing, "balcony", None),
            region=getattr(listing, "region", None),
            config_path=config_path,
            tiers_yaml_path=tiers_yaml_path,
        )
    else:
        price_pcm = listing.get("price_numeric")
        if price_pcm is not None:
            price_pcm = price_pcm // 100

        total, breakdown = compute_score_from_db(
            db_path=db_path,
            price_pcm=price_pcm,
            size_sqft=listing.get("size_sqft"),
            bedrooms=listing.get("bedrooms"),
            carpet_detected=listing.get("carpet_detected"),
            carpet_confidence=listing.get("carpet_confidence", 0.0) or 0.0,
            carpet_in_bedroom=listing.get("carpet_in_bedroom"),
            carpet_other_areas=listing.get("carpet_other_areas"),
            bedrooms_with_carpet=listing.get("bedrooms_with_carpet"),
            has_living_area_carpet=listing.get("has_living_area_carpet"),
            epc_rating=listing.get("epc_rating"),
            commute_mins=listing.get("commute_canary_wharf") or listing.get("commute_whitechapel"),
            tfl_zone=listing.get("tfl_zone"),
            garden=listing.get("garden"),
            balcony=listing.get("balcony"),
            region=listing.get("region"),
            config_path=config_path,
            tiers_yaml_path=tiers_yaml_path,
        )

    # Write score_breakdown back to listing object when it supports assignment.
    breakdown_json = json.dumps(breakdown)
    try:
        if hasattr(listing, "score_breakdown"):
            listing.score_breakdown = breakdown_json
    except Exception:
        pass

    return total, breakdown
