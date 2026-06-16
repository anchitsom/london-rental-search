"""
EPC (Energy Performance Certificate) enrichment via the UK Open Data Communities API.
https://epc.opendatacommunities.org/

Requires free registration. Set credentials via env vars:
  EPC_EMAIL   -- registered email address
  EPC_API_KEY -- API key from the dashboard

The primary public function is enrich_listing_with_epc(listing) -> dict,
which returns all 20 EPC-derived fields per the spec (section 8a), or
all-None if no EPC record can be matched to the listing.

Address matching uses rapidfuzz.fuzz.token_set_ratio to compare each EPC
record's concatenated address fields against the listing's address field.
Threshold: 60 (0-100 scale). Ties broken by most-recent lodgement-date.

The legacy get_epc_rating() is retained for backward compatibility but
now delegates to enrich_listing_with_epc internally.
"""

import os
from typing import Any, Optional

import httpx
from rapidfuzz import fuzz
from structlog import get_logger

logger = get_logger()

EPC_EMAIL = os.environ.get("EPC_EMAIL", "")
EPC_API_KEY = os.environ.get("EPC_API_KEY", "")
EPC_BASE_URL = "https://epc.opendatacommunities.org/api/v1/domestic/search"

# Threshold read from the sweep-derived module.  The sweep script writes
# homehunt/epc_threshold.py when you run scripts/sweep_epc_threshold.py.
# Default 60 retained as a fallback when the module is absent.
try:
    from homehunt.epc_threshold import EPC_MATCH_THRESHOLD as _SWEPT_THRESHOLD
    MATCH_THRESHOLD = int(_SWEPT_THRESHOLD * 100)  # convert 0-1 -> 0-100 scale
except ImportError:
    MATCH_THRESHOLD = 60  # original default

EPC_FETCH_SIZE = 20   # maximum records to request per postcode


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _all_none_result() -> dict[str, Any]:
    """Return the standard all-None EPC result dict."""
    return {
        "epc_rating": None,
        "total_floor_area_sqm": None,
        "size_sqft_fallback": None,
        "current_energy_efficiency": None,
        "potential_energy_efficiency": None,
        "number_habitable_rooms": None,
        "number_heated_rooms": None,
        "built_form": None,
        "property_type_epc": None,
        "construction_age_band": None,
        "tenure_epc": None,
        "windows_description": None,
        "windows_energy_eff": None,
        "walls_description": None,
        "walls_energy_eff": None,
        "roof_description": None,
        "floor_level": None,
        "mains_gas_flag": None,
        "heating_cost_current": None,
        "epc_lodgement_date": None,
        "epc_inspection_date": None,
    }


def _row_address_string(row: dict) -> str:
    """Concatenate address1 + address2 + address3 from an EPC row."""
    parts = [
        (row.get("address1") or "").strip(),
        (row.get("address2") or "").strip(),
        (row.get("address3") or "").strip(),
    ]
    return ", ".join(p for p in parts if p)


def _parse_date(date_str: Optional[str]) -> str:
    """Return date string as-is for comparison; empty string if None."""
    return date_str or ""


def _select_best_row(rows: list[dict], listing_address: str) -> Optional[dict]:
    """
    Pick the EPC row whose address best matches listing_address.

    1. Score every row using rapidfuzz token_set_ratio.
    2. Discard rows below MATCH_THRESHOLD.
    3. Apply the unit gate (homehunt.epc_unit_gate.passes_unit_gate). Rows
       that fail the gate are removed from the candidate set BEFORE the
       highest-score-wins logic runs. The gate is a precision floor catching
       wrong-unit-number assignments the fuzzy matcher would otherwise make.
    4. Among those that survive, take the highest score.
    5. If two rows tie on score, take the more recent lodgement-date.
    6. Return None if nothing clears threshold or if all rows fail the gate.
    """
    from homehunt.epc_unit_gate import passes_unit_gate

    candidates = []
    for row in rows:
        epc_addr = _row_address_string(row)
        score = fuzz.token_set_ratio(listing_address, epc_addr)
        candidates.append((score, row))

    above_threshold = [(s, r) for s, r in candidates if s >= MATCH_THRESHOLD]

    if not above_threshold:
        return None

    # Apply the unit gate before tie-break selection.
    gated = [(s, r) for s, r in above_threshold if passes_unit_gate(listing_address, r)]
    if not gated:
        return None

    # Sort descending by score, then descending by lodgement-date for ties.
    gated.sort(
        key=lambda x: (x[0], _parse_date(x[1].get("lodgement-date"))),
        reverse=True,
    )
    return gated[0][1]


def _row_to_result(row: dict) -> dict[str, Any]:
    """Map an EPC API row dict to the canonical 20-field result dict."""
    floor_area = row.get("total-floor-area")
    if floor_area is not None:
        try:
            floor_area = float(floor_area)
        except (TypeError, ValueError):
            floor_area = None

    size_sqft_fallback = None
    if floor_area is not None:
        size_sqft_fallback = round(floor_area * 10.7639)

    def _int_field(key: str) -> Optional[int]:
        val = row.get(key)
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    return {
        "epc_rating": row.get("current-energy-rating"),
        "total_floor_area_sqm": floor_area,
        "size_sqft_fallback": size_sqft_fallback,
        "current_energy_efficiency": _int_field("current-energy-efficiency"),
        "potential_energy_efficiency": _int_field("potential-energy-efficiency"),
        "number_habitable_rooms": _int_field("number-habitable-rooms"),
        "number_heated_rooms": _int_field("number-heated-rooms"),
        "built_form": row.get("built-form"),
        "property_type_epc": row.get("property-type"),
        "construction_age_band": row.get("construction-age-band"),
        "tenure_epc": row.get("tenure"),
        "windows_description": row.get("windows-description"),
        "windows_energy_eff": row.get("windows-energy-eff"),
        "walls_description": row.get("walls-description"),
        "walls_energy_eff": row.get("walls-energy-eff"),
        "roof_description": row.get("roof-description"),
        "floor_level": row.get("floor-level"),
        "mains_gas_flag": row.get("mains-gas-flag"),
        "heating_cost_current": _int_field("heating-cost-current"),
        "epc_lodgement_date": row.get("lodgement-date"),
        "epc_inspection_date": row.get("inspection-date"),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def enrich_listing_with_epc(listing) -> dict[str, Any]:
    """
    Fetch EPC records for a listing's postcode and return the 20-field
    enrichment dict for the best-matching record.

    Parameters
    ----------
    listing : any object with .postcode and .address attributes

    Returns
    -------
    dict with keys matching _all_none_result(). Values are None when:
      - credentials are missing
      - postcode is absent
      - no record is returned for the postcode
      - no record scores >= MATCH_THRESHOLD against the listing's address
    """
    null_result = _all_none_result()

    if not EPC_EMAIL or not EPC_API_KEY:
        logger.debug("epc_credentials_missing")
        return null_result

    postcode = getattr(listing, "postcode", None)
    if not postcode:
        logger.debug("epc_no_postcode", listing_uid=getattr(listing, "uid", "unknown"))
        return null_result

    postcode_clean = postcode.strip().upper()
    listing_address = getattr(listing, "address", "") or ""

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                EPC_BASE_URL,
                params={"postcode": postcode_clean, "size": str(EPC_FETCH_SIZE)},
                auth=(EPC_EMAIL, EPC_API_KEY),
                headers={"Accept": "application/json"},
                timeout=15.0,
            )
            resp.raise_for_status()
            # The EPC API returns 200 with an empty body for fully-unindexed
            # postcodes. resp.json() raises JSONDecodeError on empty input,
            # which historically routed to the broad except below and emitted
            # an epc_lookup_failed log line. Treat the empty case as zero rows
            # so it flows through the epc_no_records path with the correct label.
            data = resp.json() if resp.text.strip() else {"rows": []}

        rows = data.get("rows", [])

        if not rows:
            logger.info(
                "epc_no_records",
                postcode=postcode_clean,
                listing_address=listing_address,
                listing_uid=getattr(listing, "uid", "unknown"),
            )
            return null_result

        best = _select_best_row(rows, listing_address)

        if best is None:
            candidate_addresses = [_row_address_string(r) for r in rows]
            scores = [
                fuzz.token_set_ratio(listing_address, _row_address_string(r))
                for r in rows
            ]
            logger.info(
                "epc_address_match_miss",
                postcode=postcode_clean,
                listing_address=listing_address,
                listing_uid=getattr(listing, "uid", "unknown"),
                candidate_addresses=candidate_addresses,
                match_scores=scores,
                threshold=MATCH_THRESHOLD,
                decision="no_match",
            )
            return null_result

        result = _row_to_result(best)
        logger.info(
            "epc_matched",
            postcode=postcode_clean,
            listing_address=listing_address,
            matched_address=_row_address_string(best),
            epc_rating=result["epc_rating"],
            floor_area_sqm=result["total_floor_area_sqm"],
            listing_uid=getattr(listing, "uid", "unknown"),
        )
        return result

    except httpx.HTTPStatusError as exc:
        logger.warning(
            "epc_http_error",
            postcode=postcode_clean,
            status=exc.response.status_code,
            listing_uid=getattr(listing, "uid", "unknown"),
        )
        return null_result
    except Exception as exc:
        logger.warning(
            "epc_lookup_failed",
            postcode=postcode_clean,
            error=str(exc),
            listing_uid=getattr(listing, "uid", "unknown"),
        )
        return null_result


async def get_epc_rating(postcode: str) -> Optional[str]:
    """
    Legacy single-field EPC lookup. Retained for backward compatibility.

    Returns the most-recent EPC rating letter ("A"-"G") for the postcode,
    without address matching. Use enrich_listing_with_epc for new code.
    """
    if not EPC_EMAIL or not EPC_API_KEY:
        logger.debug("epc_credentials_missing")
        return None

    if not postcode:
        return None

    postcode_clean = postcode.strip().upper()

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                EPC_BASE_URL,
                params={"postcode": postcode_clean, "size": "1"},
                auth=(EPC_EMAIL, EPC_API_KEY),
                headers={"Accept": "application/json"},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()

        rows = data.get("rows", [])
        if not rows:
            logger.debug("epc_no_results", postcode=postcode_clean)
            return None

        rating = rows[0].get("current-energy-rating")
        logger.info("epc_rating_found", postcode=postcode_clean, rating=rating)
        return rating

    except httpx.HTTPStatusError as exc:
        logger.warning(
            "epc_http_error",
            postcode=postcode_clean,
            status=exc.response.status_code,
        )
        return None
    except Exception as exc:
        logger.warning("epc_lookup_failed", postcode=postcode_clean, error=str(exc))
        return None


def score_epc(rating: Optional[str]) -> int:
    """Convert EPC letter rating to 0-100 score."""
    mapping = {
        "A": 100,
        "B": 100,
        "C": 100,
        "D": 40,
        "E": 0,
        "F": 0,
        "G": 0,
    }
    if rating is None:
        return 50  # Unknown -- neutral
    return mapping.get(rating.upper(), 50)
