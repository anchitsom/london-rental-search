"""
EPC unit gate (Wave 1.7 round 2 finding).

The fuzzy address matcher in homehunt/epc.py occasionally selects an EPC row
from the wrong building because token-set scoring collapses building-number
tokens. The Wave 1.7 round 2 audit (epc_enrichment/REPORT_ROUND2.md) found
this on at least 2 of 35 control listings (uids 87972531 and 87876822).

passes_unit_gate() requires the listing's leading building number (the first
numeric token in the listing address) to literally appear as a token in the
EPC row's address1+address2 after normalisation. If the listing has no
leading number (building name like "Solar House"), the gate falls open
(returns True) so it never regresses on building-name-only listings.

Wire-in point: homehunt/epc.py:_select_best_row. Rows that fail the gate are
removed from the candidate set BEFORE the highest-score-wins logic runs;
this is a precision floor, not a tie-breaker on the chosen row.
"""

from __future__ import annotations

import re
from typing import Optional


# Match the first numeric token in the listing address. Allows a trailing
# letter (e.g. "22a" -> "22") because the EPC row often uses the bare number
# while the listing carries the unit suffix.
_LEADING_NUMBER = re.compile(r"^\s*(\d+)")

# Punctuation strip; EPC row addresses use commas, hyphens, and full stops.
_PUNCTUATION = re.compile(r"[^\w\s]+")
_WHITESPACE = re.compile(r"\s+")


def _leading_number(address: str) -> Optional[str]:
    """
    Return the first numeric token at the start of the address, or None when
    the address starts with a building name. Treats hyphenated ranges like
    "3-5" as the leading number "3"; the gate then accepts EPC rows whose
    address1 or address2 contains EITHER end of the range as a token.
    """
    if not address:
        return None
    m = _LEADING_NUMBER.match(address)
    if not m:
        return None
    return m.group(1)


def _hyphen_endpoints(token: str) -> list[str]:
    """
    Given the leading number token, return all endpoint numbers when it is a
    hyphenated range. "3-5" -> ["3", "5"]. Plain numbers return as-is.
    """
    if "-" in token:
        # Token may carry the range, e.g. "3-5". Split on hyphen and keep the
        # purely numeric pieces.
        parts = [p for p in token.split("-") if p.isdigit()]
        if parts:
            return parts
    return [token]


def _normalise_for_tokens(text: str) -> list[str]:
    """
    Normalise an EPC row address string and split into tokens.

    Lowercase, strip punctuation, collapse whitespace. Tokens are word units
    suitable for membership tests like "is '22' a token here". A leading
    digit-letter unit suffix (e.g. "22a") is also added in its bare-number
    form so "22a" is matched by the gate looking for "22".
    """
    if not text:
        return []
    s = str(text).lower()
    s = _PUNCTUATION.sub(" ", s)
    s = _WHITESPACE.sub(" ", s).strip()
    tokens = s.split()
    expanded: list[str] = []
    for tok in tokens:
        expanded.append(tok)
        # Strip a trailing alphabetic suffix on a number-letter token so the
        # bare number is also present (e.g. "22a" -> also "22").
        m = re.match(r"^(\d+)[a-z]+$", tok)
        if m:
            expanded.append(m.group(1))
    return expanded


def _row_address_string(row: dict) -> str:
    """Concatenate address1 + address2 from an EPC row."""
    parts = [
        (row.get("address1") or "").strip(),
        (row.get("address2") or "").strip(),
    ]
    return " ".join(p for p in parts if p)


def passes_unit_gate(listing_address: str, epc_row: dict) -> bool:
    """
    Return True if the listing's leading building number appears as a token in
    the EPC row's normalised address1+address2, OR if the listing has no
    leading number.

    The gate falls open (returns True) for listings whose address starts with
    a building name and no number (e.g. "Solar House, 915 High Road"). The
    listing-side number is the load-bearing precision signal and is missing
    in the building-name case; rejecting on the row side would over-reject
    legitimate matches that fuzzy scoring already accepts.

    Listings whose leading number is a hyphenated range (e.g. "3-5 Crouch End
    Hill") accept EPC rows whose address contains EITHER endpoint as a
    token. The gate REJECTS rows whose address contains a different building
    number range entirely (e.g. "33-35 Crouch End Hill" when the listing is
    "3-5"); a token from a different range cannot satisfy the gate.
    """
    listing_lead = _leading_number(listing_address)
    if listing_lead is None:
        # No leading number on the listing side: gate falls open.
        return True

    row_text = _row_address_string(epc_row)
    if not row_text:
        return False

    row_tokens = _normalise_for_tokens(row_text)
    if not row_tokens:
        return False

    # Detect whether the EPC row also carries a hyphenated range as a contiguous
    # building-number token in its raw text. If it does, the row's range MUST
    # share at least one endpoint with the listing's range; otherwise the
    # range is a different building entirely.
    listing_endpoints = set(_hyphen_endpoints(listing_lead))
    row_text_norm = _PUNCTUATION.sub(" ", row_text.lower())
    row_text_norm = _WHITESPACE.sub(" ", row_text_norm).strip()

    # Find any contiguous "n-m" or "n m" range tokens in the row text. The
    # punctuation strip turned hyphens into spaces, so "33-35" becomes "33 35";
    # detect ranges as adjacent numeric tokens that form a plausible building
    # range (small absolute difference). When a range is present, require an
    # endpoint match.
    row_tokens_seq = row_text_norm.split()
    ranges_in_row: list[tuple[str, str]] = []
    for i in range(len(row_tokens_seq) - 1):
        a, b = row_tokens_seq[i], row_tokens_seq[i + 1]
        if a.isdigit() and b.isdigit():
            ranges_in_row.append((a, b))

    if ranges_in_row:
        # If any range in the row shares an endpoint with the listing's
        # endpoints, accept; otherwise this row is a different building range.
        for (a, b) in ranges_in_row:
            if a in listing_endpoints or b in listing_endpoints:
                return True
        # No range in the row matches the listing's range. Fall through to the
        # plain-token check; if the listing's lead also appears as a stand-alone
        # token outside any detected range, allow that.
        for endpoint in listing_endpoints:
            if endpoint in row_tokens and not any(
                endpoint == a or endpoint == b for (a, b) in ranges_in_row
            ):
                return True
        return False

    # No range in the row. Plain token membership test on each listing endpoint.
    for endpoint in listing_endpoints:
        if endpoint in row_tokens:
            return True
    return False
