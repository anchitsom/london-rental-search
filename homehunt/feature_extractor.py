"""
NLP-style feature extraction from Rightmove listing text.

All extraction uses regex and keyword matching only. No external NLP libraries.
Each public function takes (description, features, title) and returns a typed value.
The features argument is the short-bullet array from the scraper (highest signal).
The description is free text (hundreds of words). Title is the page title.

Convention: boolean extractors return True / False / None.
  True  = feature confirmed present
  False = feature explicitly ruled out
  None  = no signal found (unknown)
"""

import re
from datetime import date
from typing import Optional

from dateutil import parser as date_parser

from homehunt.nlp_extractor import extract_size_and_epc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _haystack(description: str, features: list[str], title: str) -> str:
    """Combine all text sources into one lowercase string for matching."""
    parts = [description or "", title or ""] + (features or [])
    return " ".join(parts).lower()


def _features_lower(features: list[str]) -> list[str]:
    return [f.lower() for f in (features or [])]


def _any_feature(features: list[str], *keywords: str) -> bool:
    fl = _features_lower(features)
    for kw in keywords:
        if any(kw in f for f in fl):
            return True
    return False


def _desc_matches(text: str, *patterns: str, flags: int = re.IGNORECASE) -> bool:
    for p in patterns:
        if re.search(p, text, flags):
            return True
    return False


# ---------------------------------------------------------------------------
# 1. garden
# ---------------------------------------------------------------------------

_GARDEN_POS = [
    r"\bprivate garden\b",
    r"\bshared garden\b",
    r"\brear garden\b",
    r"\bfront garden\b",
    r"\bsouth.facing garden\b",
    r"\bgarden access\b",
    r"\bgarden to the\b",
    r"\bwith garden\b",
    r"\bhas a garden\b",
    r"\bbenefits from.{0,30}garden\b",
    r"\bgarden area\b",
]

_GARDEN_NEG = [
    r"\bno garden\b",
    r"\bno outdoor space\b",
    r"\bwithout a garden\b",
]


def extract_garden(
    description: str, features: list[str], title: str
) -> Optional[bool]:
    if _any_feature(features, "garden"):
        # Make sure it is not "no garden" in a feature bullet (unlikely but defensive)
        neg_feats = [f for f in _features_lower(features) if "no garden" in f or "without garden" in f]
        if not neg_feats:
            return True

    desc_title = f"{description or ''} {title or ''}"
    for p in _GARDEN_NEG:
        if re.search(p, desc_title, re.IGNORECASE):
            return False
    for p in _GARDEN_POS:
        if re.search(p, desc_title, re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------------------
# 2. balcony
# ---------------------------------------------------------------------------

_BALCONY_POS = [
    r"\bbalcony\b",
    r"\bjuliet balcony\b",
    r"\bprivate terrace\b",
    r"\bsouth.facing terrace\b",
    r"\boutside terrace\b",
    r"\bterrace off\b",
    r"\bterrace to the\b",
    r"\bwith a terrace\b",
    r"\bhas a terrace\b",
    r"\bpatio\b",
    r"\bjuliette balcony\b",
]

_BALCONY_NEG = [
    r"\bno balcony\b",
    r"\bno terrace\b",
]


def extract_balcony(
    description: str, features: list[str], title: str
) -> Optional[bool]:
    if _any_feature(features, "balcony", "terrace", "patio"):
        return True

    desc_title = f"{description or ''} {title or ''}"
    for p in _BALCONY_NEG:
        if re.search(p, desc_title, re.IGNORECASE):
            return False
    for p in _BALCONY_POS:
        if re.search(p, desc_title, re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------------------
# 3. bills_included
# ---------------------------------------------------------------------------

_BILLS_POS = [
    r"\bbills included\b",
    r"\bincluding bills\b",
    r"\bwith bills included\b",
    r"\butility bills.{0,20}included\b",
    r"\bincluded in the rent\b",
    r"\bincluded in the rental\b",
    r"\bwater.{0,20}included\b",
    r"\brates.{0,20}included\b",
    r"\bgas.{0,20}electric.{0,20}included\b",
    r"\bcouncil tax included\b",
    r"\ball bills\b",
]

_BILLS_NEG = [
    r"\bbills not included\b",
    r"\bexcluding bills\b",
    r"\bbills are not included\b",
    r"\btenant.{0,30}responsible for.{0,20}bills\b",
    r"\bbills are excluded\b",
]


def extract_bills_included(
    description: str, features: list[str], title: str
) -> Optional[bool]:
    if _any_feature(features, "bills included", "bills inc", "all bills"):
        return True

    desc_title = f"{description or ''} {title or ''}"
    for p in _BILLS_NEG:
        if re.search(p, desc_title, re.IGNORECASE):
            return False
    for p in _BILLS_POS:
        if re.search(p, desc_title, re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------------------
# 4. furnished
# ---------------------------------------------------------------------------

_FURNISHED_FEATURE_MAP = {
    "part furnished": "part_furnished",
    "partially furnished": "part_furnished",
    "part-furnished": "part_furnished",
    "unfurnished": "unfurnished",
    "furnished": "furnished",
}


def extract_furnished(
    description: str, features: list[str], title: str
) -> str:
    # Feature bullets are highest priority. Check most specific first.
    fl = _features_lower(features)
    for f in fl:
        for keyword, value in _FURNISHED_FEATURE_MAP.items():
            if keyword in f:
                return value

    desc_title = (f"{description or ''} {title or ''}").lower()

    # Order matters: check part/partial before plain furnished.
    if re.search(r"\bpart.?furnished\b|\bpartially furnished\b", desc_title):
        return "part_furnished"
    if re.search(r"\bunfurnished\b", desc_title):
        return "unfurnished"
    if re.search(r"\bfully furnished\b|\bfurnished\b", desc_title):
        return "furnished"

    return "unknown"


# ---------------------------------------------------------------------------
# 5. council_tax_band
# ---------------------------------------------------------------------------

_CTB_PATTERNS = [
    r"\bcouncil tax band[:\s]+([A-H])\b",
    r"\bcouncil tax band is ([A-H])\b",
    r"\bctb[:\s]+([A-H])\b",
    r"\btax band[:\s]+([A-H])\b",
]


def extract_council_tax_band(
    description: str, features: list[str], title: str
) -> Optional[str]:
    sources = (features or []) + [description or "", title or ""]
    for text in sources:
        for pattern in _CTB_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(1).upper()
    return None


# ---------------------------------------------------------------------------
# 6. let_available_date
# ---------------------------------------------------------------------------

_MONTHS = (
    "january|february|march|april|may|june|july|august|september|"
    "october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec"
)

_ORDINAL = r"(?:\d{1,2}(?:st|nd|rd|th)?)"

_DATE_PATTERNS = [
    # feature bullet with colon: "Available From: 15th May 2026"
    rf"available\s+from\s*:\s*({_ORDINAL}\s+(?:{_MONTHS})\s+\d{{4}})",
    rf"available\s+from\s*:\s*({_ORDINAL}\s+(?:{_MONTHS}))",
    # "available from 1st June 2026" (nothing between from and date)
    rf"available\s+from\s+({_ORDINAL}\s+(?:{_MONTHS})\s+\d{{4}})",
    rf"available\s+from\s+({_ORDINAL}\s+(?:{_MONTHS}))",
    # "available [anything up to 10 words] from 1st June 2026"
    rf"available\s+(?:\w+\s+){{0,5}}from\s+({_ORDINAL}\s+(?:{_MONTHS})\s+\d{{4}})",
    rf"available\s+(?:\w+\s+){{0,5}}from\s+({_ORDINAL}\s+(?:{_MONTHS}))",
    # standalone ordinal+month+year near "available"
    rf"available\b.{{0,60}}?({_ORDINAL}\s+(?:{_MONTHS})\s+\d{{4}})",
    # "available now" / "available immediately"
    r"available\s+(now|immediately)\b",
    # "available from now"
    r"available\s+from\s+(now)\b",
]


def extract_let_available_date(
    description: str, features: list[str], title: str
) -> Optional[str]:
    sources = (features or []) + [description or "", title or ""]
    for text in sources:
        for pattern in _DATE_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
    return None


_LET_AVAILABLE_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

_LET_AVAILABLE_MONTH_PATTERN = "|".join(
    sorted(_LET_AVAILABLE_MONTHS, key=len, reverse=True)
)


def normalise_let_available_date(raw: Optional[str], today: date) -> Optional[str]:
    """Return YYYY-MM-DD or None. Never raises."""
    try:
        return _normalise_let_available_date(raw, today)
    except Exception:
        return None


def _normalise_let_available_date(raw: Optional[str], today: date) -> Optional[str]:
    if raw is None or not isinstance(raw, str):
        return None

    value = re.sub(r"\s+", " ", raw.replace("\x00", " ")).strip().lower()
    if not value:
        return None

    value = re.sub(r"^available(?:\s+from)?(?:\s*:\s*|\s+)", "", value).strip()
    if not value:
        return None

    if value in {"now", "immediately", "from now", "asap"}:
        return today.isoformat()

    if value in {"tbc", "available soon"}:
        return None

    value = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", value)
    value = re.sub(r"\bof\s+", "", value)
    value = value.strip(" ,.;:")
    if not value:
        return None

    numeric = _parse_let_available_numeric_date(value)
    if numeric is not None:
        return numeric

    named = _parse_let_available_named_date(value, today)
    if named is not None:
        return named

    try:
        parsed = date_parser.parse(value, dayfirst=True, fuzzy=False).date()
    except Exception:
        return None

    return parsed.isoformat()


def _parse_let_available_numeric_date(value: str) -> Optional[str]:
    iso_match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", value)
    if iso_match:
        year, month, day = map(int, iso_match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    uk_match = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", value)
    if uk_match:
        day, month, year = map(int, uk_match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    return None


def _parse_let_available_named_date(value: str, today: date) -> Optional[str]:
    month = rf"(?P<month>{_LET_AVAILABLE_MONTH_PATTERN})"

    patterns = [
        rf"(?P<day>\d{{1,2}})\s+{month}\s+(?P<year>\d{{4}})",
        rf"{month}\s+(?P<day>\d{{1,2}})\s+(?P<year>\d{{4}})",
        rf"{month}\s+(?P<year>\d{{4}})",
        rf"(?P<day>\d{{1,2}})\s+{month}",
    ]

    for pattern in patterns:
        match = re.fullmatch(pattern, value)
        if not match:
            continue

        groups = match.groupdict()
        month_name = groups["month"]
        month_num = _LET_AVAILABLE_MONTHS[month_name]
        day_num = int(groups.get("day") or 1)
        year_text = groups.get("year")
        year_num = int(year_text) if year_text else today.year

        try:
            candidate = date(year_num, month_num, day_num)
        except ValueError:
            return None

        if not year_text and candidate < today:
            try:
                candidate = date(year_num + 1, month_num, day_num)
            except ValueError:
                return None

        return candidate.isoformat()

    return None


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

def extract_all(
    description: str, features: list[str], title: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    listing_uid: str = "unknown",
) -> dict:
    """
    Run all extractors and return a dict keyed by field name.

    Keys: garden, balcony, bills_included, furnished,
          council_tax_band, let_available_date, region, size_sqft, epc_rating

    When latitude and longitude are provided, region is resolved via
    homehunt.region.resolve_region (nearest verified neighbourhood centroid
    nearest centroid). When coordinates are absent, region is returned
    as None.
    """
    from homehunt.region import resolve_region as _resolve_region

    if latitude is not None and longitude is not None:
        region = _resolve_region(latitude, longitude)
    else:
        region = None

    nlp = extract_size_and_epc(description, features, listing_uid=listing_uid)

    return {
        "garden": extract_garden(description, features, title),
        "balcony": extract_balcony(description, features, title),
        "bills_included": extract_bills_included(description, features, title),
        "furnished": extract_furnished(description, features, title),
        "council_tax_band": extract_council_tax_band(description, features, title),
        "let_available_date": extract_let_available_date(description, features, title),
        "region": region,
        "size_sqft": nlp["size_sqft"],
        "epc_rating": nlp["epc_rating"],
    }
