"""
Source-merge layer for size_sqft.

Implements the Wave 1.6 source trust order and sanity rules.

Trust order (highest to lowest):
  structured  -- parsed from Rightmove structured sizings field
  nlp         -- regex extraction from description/features text
  floorplan_vision -- qwen2.5vl OCR of the floorplan image
  epc_interpolated -- EPC total-floor-area after interpolation
  epc_direct  -- EPC total-floor-area direct

Sanity rules applied per source before accepting:
  1. size_sqft >= 100  (catches OCR digit-drops; no UK habitable flat is smaller)
  2. size_sqft / bedrooms <= 800  (catches EPC mismatches on mixed-use streets)

Extra rule:
  3. When EPC-derived size and floorplan-vision size disagree by >30%, prefer
     floorplan and set epc_floorplan_disagreement=True in the result.

Public API:
  merge_size_sqft(...) -> dict
    Keys: size_sqft, size_sqft_source, size_sqft_confidence,
          epc_floorplan_disagreement
  SIZE_SOURCE_CONFIDENCE: dict mapping source name to float confidence
"""

from typing import Optional
import math

from structlog import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# Confidence table
# ---------------------------------------------------------------------------

SIZE_SOURCE_CONFIDENCE: dict[str, float] = {
    "structured": 0.95,
    "nlp": 0.85,
    "floorplan_vision": 0.80,
    "epc_direct": 0.70,
    "epc_interpolated": 0.50,
}

# Trust order -- highest first
_TRUST_ORDER = [
    "structured",
    "nlp",
    "floorplan_vision",
    "epc_interpolated",
    "epc_direct",
]

_MIN_SQFT = 100         # absolute floor; below this is definitely an OCR or unit error
_MAX_SQFT_PER_BED = 800  # implausibly large per bedroom


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _passes_sanity(sqft: int, bedrooms: Optional[int]) -> bool:
    """Return True if sqft passes both sanity rules."""
    if sqft < _MIN_SQFT:
        return False
    if bedrooms is not None and bedrooms > 0:
        if sqft / bedrooms > _MAX_SQFT_PER_BED:
            return False
    return True


def _pct_diff(a: float, b: float) -> float:
    """Return absolute percent difference between a and b relative to max(a, b)."""
    if a == 0 and b == 0:
        return 0.0
    return abs(a - b) / max(abs(a), abs(b))


# ---------------------------------------------------------------------------
# Public merge function
# ---------------------------------------------------------------------------

def merge_size_sqft(
    structured: Optional[int],
    nlp: Optional[int],
    floorplan_vision: Optional[int],
    epc_interpolated: Optional[int],
    epc_direct: Optional[int],
    bedrooms: Optional[int],
    listing_uid: str = "unknown",
) -> dict:
    """
    Apply source trust order and sanity rules to select the best size_sqft.

    Parameters
    ----------
    structured : int | None
        Size from Rightmove structured sizings field.
    nlp : int | None
        Size extracted by NLP regex from description/features.
    floorplan_vision : int | None
        Size from qwen2.5vl floorplan OCR.
    epc_interpolated : int | None
        Size derived from EPC total-floor-area (interpolated from sqm).
    epc_direct : int | None
        Size derived from EPC total-floor-area (direct sqm->sqft).
    bedrooms : int | None
        Number of bedrooms (used for per-bedroom sanity rule).
    listing_uid : str
        For structured logging. Default "unknown".

    Returns
    -------
    dict with keys:
        size_sqft           int | None
        size_sqft_source    str | None
        size_sqft_confidence float | None
        epc_floorplan_disagreement bool  (True when >30% disagreement detected)
    """
    source_values: dict[str, Optional[int]] = {
        "structured": structured,
        "nlp": nlp,
        "floorplan_vision": floorplan_vision,
        "epc_interpolated": epc_interpolated,
        "epc_direct": epc_direct,
    }

    # Determine EPC-derived value for disagreement check.  Use whichever EPC
    # source is non-None (interpolated first, then direct).
    epc_size = epc_interpolated if epc_interpolated is not None else epc_direct

    # Identify the floorplan disagreement flag up front (independent of trust order).
    epc_fp_disagreement = False
    if (
        floorplan_vision is not None
        and floorplan_vision >= _MIN_SQFT
        and epc_size is not None
        and epc_size >= _MIN_SQFT
    ):
        diff = _pct_diff(float(floorplan_vision), float(epc_size))
        if diff > 0.30:
            epc_fp_disagreement = True
            logger.warning(
                "size_epc_floorplan_disagreement",
                listing_uid=listing_uid,
                floorplan_vision=floorplan_vision,
                epc_size=epc_size,
                pct_diff=round(diff * 100, 1),
            )

    # Walk the trust order, pick the first value that passes sanity.
    for source in _TRUST_ORDER:
        val = source_values[source]
        if val is None:
            continue
        if not _passes_sanity(val, bedrooms):
            logger.info(
                "size_sanity_rejected",
                listing_uid=listing_uid,
                source=source,
                size_sqft=val,
                bedrooms=bedrooms,
            )
            continue
        return {
            "size_sqft": val,
            "size_sqft_source": source,
            "size_sqft_confidence": SIZE_SOURCE_CONFIDENCE[source],
            "epc_floorplan_disagreement": epc_fp_disagreement,
        }

    # All sources either None or failed sanity.
    return {
        "size_sqft": None,
        "size_sqft_source": None,
        "size_sqft_confidence": None,
        "epc_floorplan_disagreement": epc_fp_disagreement,
    }
