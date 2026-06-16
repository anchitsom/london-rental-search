"""
NLP extraction of size_sqft and epc_rating from listing description and features.

Primary extraction path for both fields. Regex-only, no external NLP libraries.

The EPC API call (Agent B scope) is the fallback when this module returns None
for either field.

Structured miss logging: on every call where one or both fields are missing, a
structured log entry is written at INFO level with listing_uid, missing fields,
and a description excerpt. Callers supply listing_uid; the default is "unknown"
so the module is safe to call outside the enrichment pipeline (e.g. in tests).
"""

import re
from typing import Optional

from structlog import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# SQM to SQFT conversion factor (exact, per international agreement)
# ---------------------------------------------------------------------------
_SQM_TO_SQFT = 10.7639

# ---------------------------------------------------------------------------
# Size patterns
# ---------------------------------------------------------------------------

# Matches: "720 sq ft", "720 sq.ft", "720 sqft", "720 sq. ft.", "1,200 sqft",
#          "720 ft²" (Unicode superscript 2, U+00B2), "720 square feet"
_SQFT_RE = re.compile(
    r"([\d,]{2,7})\s*(?:sq\.?\s*ft|ft²|square\s+fe?e?t)",
    re.IGNORECASE,
)

# Matches: "65 sqm", "65 sq m", "65 sq.m", "65 sq. m", "65 m²" (U+00B2),
#          "65 square metres", "65 sq metres" -- not "sqm2" or "sqm."
_SQM_RE = re.compile(
    r"([\d,]{2,6})\s*(?:sq\.?\s*m(?:etres?|eters?)?(?!\w)|m²|square\s+me?t(?:re|er)s?)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# EPC patterns
# ---------------------------------------------------------------------------

# Matches: "EPC Rating: C", "EPC rating B", "EPC: D"
_EPC_RATING_RE = re.compile(
    r"\bEPC\s*(?:rating|band|:)?\s*[:\-]?\s*([A-G])\b",
    re.IGNORECASE,
)

# Matches: "Energy efficiency D", "Energy efficiency rating: C"
_ENERGY_EFF_RE = re.compile(
    r"\benergy\s+efficiency(?:\s+rating)?\s*[:\-]?\s*([A-G])\b",
    re.IGNORECASE,
)

# Matches: "EPC band C", "Energy Performance Certificate: B"
_EPC_CERT_RE = re.compile(
    r"\benergy\s+performance\s+(?:certificate|cert)\s*[:\-]?\s*([A-G])\b",
    re.IGNORECASE,
)


def _to_int(raw: str) -> Optional[int]:
    """Parse a potentially comma-separated digit string to int."""
    try:
        return int(raw.replace(",", ""))
    except ValueError:
        return None


def _extract_size(text: str) -> Optional[int]:
    """
    Extract size from a single text block. Prefers sq ft over sqm.
    Returns integer sq ft or None.
    """
    # Try sq ft first
    m = _SQFT_RE.search(text)
    if m:
        return _to_int(m.group(1))

    # Fall back to sqm conversion
    m = _SQM_RE.search(text)
    if m:
        sqm = _to_int(m.group(1))
        if sqm is not None:
            return int(round(sqm * _SQM_TO_SQFT))

    return None


def _extract_epc(text: str) -> Optional[str]:
    """
    Extract EPC letter rating from a single text block.
    Returns uppercase letter A-G or None.
    """
    for pattern in (_EPC_RATING_RE, _ENERGY_EFF_RE, _EPC_CERT_RE):
        m = pattern.search(text)
        if m:
            return m.group(1).upper()
    return None


def extract_size_and_epc(
    description: Optional[str],
    features: Optional[list],
    listing_uid: str = "unknown",
) -> dict:
    """
    Parse size_sqft and epc_rating from listing description and features text.

    Primary extraction path for both fields. Agent B's EPC API enrichment is
    the fallback when this function returns None for either field.

    Args:
        description: Free-text listing description (may be None or empty).
        features: Short bullet list of property features (may be None or empty).
        listing_uid: Identifier for structured miss logging. Optional in tests.

    Returns:
        dict with keys:
          "size_sqft": int | None
          "epc_rating": str | None  (single uppercase letter A-G)
    """
    # Collect all text sources. Features are checked first because they are
    # structured and higher-signal than free prose.
    sources: list[str] = list(features or []) + [description or ""]

    size_sqft: Optional[int] = None
    epc_rating: Optional[str] = None

    for text in sources:
        if size_sqft is None:
            size_sqft = _extract_size(text)
        if epc_rating is None:
            epc_rating = _extract_epc(text)
        if size_sqft is not None and epc_rating is not None:
            break

    # Structured miss log
    missing = []
    if size_sqft is None:
        missing.append("size")
    if epc_rating is None:
        missing.append("epc")

    if missing:
        excerpt = (description or "")[:120].strip()
        logger.info(
            "nlp_extractor_miss",
            listing_uid=listing_uid,
            missing=missing,
            description_excerpt=excerpt,
        )

    return {
        "size_sqft": size_sqft,
        "epc_rating": epc_rating,
    }
