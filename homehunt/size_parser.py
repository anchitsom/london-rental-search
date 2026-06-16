"""
Parse Rightmove `displaySize` strings into integer square feet.

Rightmove returns size as free-text strings like:
  "650 sq ft"
  "62 sq m"
  "650 sq ft / 60 sq m"
  "Ask agent"
  ""

Canonical unit is square feet. Square metres are converted at 10.764 sqft per sqm.
Returns None when no usable number is present.
"""

import re
from typing import Optional

_SQFT_RE = re.compile(r"([\d,]{2,7})\s*sq\.?\s*ft", re.IGNORECASE)
_SQM_RE = re.compile(r"([\d,]{2,5})\s*sq\.?\s*m(?!\w)", re.IGNORECASE)


def _to_int(group: str) -> Optional[int]:
    try:
        return int(group.replace(",", ""))
    except ValueError:
        return None


def parse_size_sqft(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    m = _SQFT_RE.search(raw)
    if m:
        return _to_int(m.group(1))
    m = _SQM_RE.search(raw)
    if m:
        sqm = _to_int(m.group(1))
        if sqm is None:
            return None
        return int(round(sqm * 10.764))
    return None
