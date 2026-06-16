"""End-to-end pipeline: discover, freshness-gate, two-stage enrich, score, save.

Freshness gate (Wave 2.5):
  Before any enrichment runs, every discovered URL is converted to its UID
  and looked up in the DB. UIDs whose last_scraped is within the horizon
  (FRESHNESS_HOURS, default 23) are skipped: a cheap touch bumps last_scraped
  and scrape_count without re-scraping or re-enriching. Override with
  --force-rescrape-all to re-enrich every URL, or --max-age <hours> to widen
  or narrow the horizon (--max-age 0 is equivalent to --force-rescrape-all).

Two-stage enrichment (Wave 2 Agent F, follow-up):
  Stage 1 (cheap): scrape + NLP regex + EPC API + TfL + region resolution.
  Hard filter applied between stages so vision compute is only spent on
  listings that already pass the cheap-signal gate.
  Stage 2 (expensive vision): floorplan vision, carpet vision, EPC vision OCR.
  Hard filter re-applied after stage 2 because vision can reveal a
  size_sqft that falls below the floor or an EPC rating below D.
"""
import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Standalone CLI runs do not get a service manager's env injection. Load secrets
# explicitly so EPC_EMAIL, EPC_API_KEY, TFL_APP_KEY, etc are available regardless
# of caller. Honour a DOTENV_PATH override, otherwise fall back to a .env next to
# this file.
load_dotenv(os.environ.get("DOTENV_PATH", str(Path(__file__).parent / ".env")))

# Set HOMEHUNT_DB default to the canonical data/ directory relative to this file,
# so the pipeline writes to the right place whether run via launchd or the CLI.
_PROJECT_ROOT = Path(__file__).parent
if not os.environ.get("HOMEHUNT_DB"):
    os.environ["HOMEHUNT_DB"] = str(_PROJECT_ROOT / "data" / "homehunt.db")

from sqlmodel import Session, select

from homehunt.config_loader import load_search_profiles, load_hard_filters, load_openrent_profiles
from homehunt.core.db import Database, Listing
from homehunt.core.models import ExtractionMethod, Portal, PropertyType
from homehunt.dedup import cluster_listings
from homehunt.filters import apply_hard_filters
from homehunt.scrapers.direct_http import DirectHTTPScraper
from homehunt.scrapers.rightmove_api import discover_properties
from homehunt.scrapers.zoopla_http import ZooplaHTTPScraper
from homehunt.scrapers.zoopla_url_builder import build_zoopla_search_url
from homehunt.scrapers._geo import (
    extract_coordinates_from_maps_embed,
    extract_coordinates_from_text,
)
from homehunt.tfl import enrich_commute, enrich_commute_multi
from homehunt.epc import get_epc_rating, enrich_listing_with_epc
from homehunt.epc_vision import extract_epc_from_image
from homehunt.carpet import detect_carpet_any
from homehunt.scorer import compute_score, compute_score_from_db
from homehunt.region import resolve_region
from homehunt.size_merge import merge_size_sqft
from homehunt.floorplan_vision import extract_floorplan_data, filter_floorplan_urls

# Wave 2 Agent F: unified filter and scoring config lives at the project root.
_FILTER_SCORING_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "filter-scoring-config.yaml",
)

# Agent A (wave1-A-nlp-extractor) may not be merged yet. Import defensively so
# this branch can be tested standalone. When both branches are merged the try
# path will resolve and NLP extraction becomes the primary path.
try:
    from homehunt.nlp_extractor import extract_size_and_epc as _nlp_extract_size_and_epc
    _NLP_AVAILABLE = True
except ImportError:
    _NLP_AVAILABLE = False

FLOORPLAN_ENABLED = os.environ.get("FLOORPLAN_ENABLED", "true").lower() not in ("0", "false", "no")

CARPET_ENABLED = os.environ.get("CARPET_ENABLED", "true").lower() not in ("0", "false", "no")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

# Semaphore caps concurrent Ollama vision calls to avoid GGML crashes under load.
# A 2026-05-09 A/B (data/carpet-perf/20260509-0923) showed lifting from 2 to 3
# yields no measurable per-listing speedup on Apple Silicon Metal, because the
# single GPU serialises model work regardless of in-flight request count. Keep
# the default at 2 and override CARPET_MAX_CONCURRENT only if the runtime
# changes (multi-GPU, MLX-native model with a different concurrency profile).
_CARPET_SEM = asyncio.Semaphore(int(os.environ.get("CARPET_MAX_CONCURRENT", "2")))

# Listings worker pool. Each worker drives one full stage 1 plus stage 2 enrichment.
# The Ollama vision calls inside stage 2 (carpet, floorplan) are bounded by their
# own per-module semaphores, so the pool size can sit safely above 2: the other
# workers will be busy on TfL/EPC HTTP while the GPU is saturated. Default 4
# is conservative on Apple Silicon.
LISTINGS_POOL_SIZE = int(os.environ.get("LISTINGS_POOL_SIZE", "4"))

# Serialise SQLite writes. SQLite supports concurrent reads but only one
# writer; without this lock a 4-worker pool can hit SQLITE_BUSY under load.
_DB_WRITE_LOCK = asyncio.Lock()


async def _detect_carpet_guarded(photo_urls: list) -> dict:
    """Wrap detect_carpet_any with the concurrency semaphore."""
    async with _CARPET_SEM:
        return await detect_carpet_any(photo_urls)


_LET_AGREED_RE = re.compile(
    r"\blet\s*agreed\b|\bunder\s*offer\b|\blet\s*by\b|\bnow\s*let\b|\btenanted\b",
    re.IGNORECASE,
)


def _parse_floor_level(raw: Optional[str]) -> Optional[int]:
    """Map an EPC-derived floor_level string to a normalised integer.

    Ground floor -> 0. Numeric prefix or ordinal -> int. None for unparseable.
    Mirrors scripts/phase_a_backfill.parse_floor_level so scraped rows and
    backfilled rows produce the same values.
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if s in {"ground", "ground floor", "gr", "g"}:
        return 0
    m = re.match(r"^(-?\d+)", s)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    m = re.match(r"^(\d+)(st|nd|rd|th)\b", s)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _is_let_agreed_title(title: Optional[str]) -> bool:
    if not title:
        return False
    return bool(_LET_AGREED_RE.search(title))


def _derive_size_credibility(
    size_sqft: Optional[int],
    floorplan: Optional[dict],
) -> Optional[str]:
    """Compare scraped size_sqft against floorplan-derived sqft.

    Returns 'trusted' if within 10%, 'conflict' if more, 'missing' if either
    side is null. Mirrors scripts/phase_a_backfill.derive_credibility.
    """
    floorplan_sqft: Optional[float] = None
    if floorplan:
        for k in ("total_size_sqft", "total_area_sqft", "total_sqft", "size_sqft"):
            v = floorplan.get(k)
            if isinstance(v, (int, float)) and v > 0:
                floorplan_sqft = float(v)
                break
        if floorplan_sqft is None:
            rooms = floorplan.get("rooms") or []
            if isinstance(rooms, list):
                s = sum(
                    float(r.get("size_sqft") or 0)
                    for r in rooms
                    if isinstance(r, dict) and isinstance(r.get("size_sqft"), (int, float))
                )
                if s > 0:
                    floorplan_sqft = s
    if size_sqft is None or floorplan_sqft is None:
        return "missing"
    if size_sqft <= 0 or floorplan_sqft <= 0:
        return "missing"
    diff = abs(size_sqft - floorplan_sqft) / max(size_sqft, floorplan_sqft)
    return "trusted" if diff <= 0.10 else "conflict"


def _reconcile_size(floorplan: Optional[dict]) -> Optional[float]:
    """Return floorplan total sqft, falling back only when room sizes exist."""
    if not floorplan:
        return None
    for k in ("total_size_sqft", "total_area_sqft", "total_sqft", "size_sqft"):
        v = floorplan.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    rooms = floorplan.get("rooms") or []
    if not isinstance(rooms, list):
        return None
    total = sum(
        float(r.get("size_sqft") or 0)
        for r in rooms
        if isinstance(r, dict) and isinstance(r.get("size_sqft"), (int, float))
    )
    return total if total > 0 else None


def _parse_price_pcm(raw_price: Optional[str]) -> Optional[int]:
    """Extract an integer pence-per-month value from a raw price string."""
    if not raw_price:
        return None
    stripped = raw_price.replace(",", "")
    m = re.search(r"\d+", stripped)
    if not m:
        return None
    try:
        return int(m.group())
    except ValueError:
        return None


class _ListingStub:
    """Minimal object satisfying enrich_listing_with_epc's attribute contract."""

    def __init__(self, postcode: Optional[str], address: Optional[str], uid: str):
        self.postcode = postcode
        self.address = address or ""
        self.uid = uid


async def _enrich_common(
    portal: Portal,
    pid: str,
    url: str,
    data: dict,
) -> Optional[dict]:
    """
    Portal-agnostic stage-1 cheap enrichment.

    Inputs are the property id, URL, and a scraped data dict whose shape is
    common to both DirectHTTPScraper (Rightmove) and ZooplaHTTPScraper. The
    function calls EPC API, TfL Journey Planner, region resolution and runs
    NLP regex against the description/features. Vision is stage 2.

    Per-portal shortcuts:
      - When data["epc_rating"] is already set by the scraper (Zoopla path),
        the EPC API is still called so the full energy block is populated,
        but the scraper letter wins for the rating field. The directly
        scraped value is treated as the ground truth.
      - When data["size_sqft"] is set by the scraper, the floorplan vision
        skip is honoured at stage 2.
    """
    portal_name = portal.value if hasattr(portal, "value") else str(portal)
    uid = f"{portal_name}:{pid}"

    postcode = data.get("postcode")
    listing_stub = _ListingStub(
        postcode=postcode,
        address=data.get("address"),
        uid=uid,
    )

    tfl_coro = enrich_commute_multi(postcode) if postcode else None
    epc_coro = enrich_listing_with_epc(listing_stub) if postcode else None

    tfl_res, epc_multifield = await asyncio.gather(
        tfl_coro if tfl_coro is not None else asyncio.sleep(0, result={}),
        epc_coro if epc_coro is not None else asyncio.sleep(0, result={}),
    )
    tfl_res = tfl_res or {}
    epc_multifield = epc_multifield or {}

    # NLP regex (Agent A).
    nlp_size: Optional[int] = None
    nlp_epc: Optional[str] = None
    if _NLP_AVAILABLE:
        description = data.get("description", "") or ""
        features = data.get("features", []) or []
        nlp_result = _nlp_extract_size_and_epc(description, features)
        nlp_size = nlp_result.get("size_sqft")
        nlp_epc = nlp_result.get("epc_rating")

    # Resolve epc_rating in priority order.
    # Scraper-extracted (Zoopla hydration) wins when present, else NLP, else
    # EPC API. Vision OCR is stage 2. Source/confidence are only stamped
    # when the EPC API value wins, preserving the pre-2.7 Rightmove path
    # byte-identically; the scraper- and NLP-sourced values leave provenance
    # null so a downstream re-run can re-evaluate them.
    scraper_epc = data.get("epc_rating")
    epc_rating_source: Optional[str] = None
    epc_rating_confidence: Optional[str] = None
    if scraper_epc:
        resolved_epc_rating = scraper_epc
    elif nlp_epc:
        resolved_epc_rating = nlp_epc
    else:
        resolved_epc_rating = epc_multifield.get("epc_rating")
        if resolved_epc_rating:
            epc_rating_source = "epc_api"
            epc_rating_confidence = "epc_api_match"

    # Coords + region. region is a hard filter, so it has to be resolved here.
    raw_content = data.get("raw_content", "") or ""
    lat = data.get("latitude")
    lng = data.get("longitude")
    if not (lat and lng):
        coords = extract_coordinates_from_maps_embed(raw_content)
        if coords:
            lat, lng = coords
        else:
            coords = extract_coordinates_from_text(raw_content)
            if coords:
                lat, lng = coords
    region = resolve_region(lat, lng) if (lat is not None and lng is not None) else None

    # Initial size merge: structured + NLP + EPC fallback. No floorplan vision yet.
    bedrooms = data.get("bedrooms")
    structured_size = data.get("size_sqft")
    epc_direct = epc_multifield.get("size_sqft_fallback")
    size_merge_result = merge_size_sqft(
        structured=structured_size,
        nlp=nlp_size,
        floorplan_vision=None,
        epc_interpolated=None,
        epc_direct=epc_direct,
        bedrooms=bedrooms,
        listing_uid=uid,
    )

    return {
        "url": url,
        "property_id": pid,
        "portal": portal,
        "uid": uid,
        "raw": data,
        "tfl": tfl_res,
        "epc": resolved_epc_rating,
        "epc_multifield": epc_multifield,
        "epc_graph_url": data.get("epc_graph_url"),
        "epc_rating_source": epc_rating_source,
        "epc_rating_confidence": epc_rating_confidence,
        "nlp_size": nlp_size,
        "nlp_epc": nlp_epc,
        "lat": lat,
        "lng": lng,
        "region": region,
        "size_sqft": size_merge_result["size_sqft"],
        "size_sqft_source": size_merge_result["size_sqft_source"],
        "size_sqft_confidence": size_merge_result["size_sqft_confidence"],
        # Stage 2 fields, filled by _enrich_stage2.
        "carpet": None,
        "floorplan": None,
    }


async def _enrich_stage1(scraper: DirectHTTPScraper, url: str) -> Optional[dict]:
    """
    Rightmove stage-1: scrape via DirectHTTPScraper, then enrich.

    Thin wrapper that delegates to the portal-agnostic _enrich_common after
    the Rightmove scrape lands.
    """
    result = await scraper.scrape_property(url)
    if not result.success or not result.data:
        return None
    pid = scraper.extract_property_id(url)
    if not pid:
        return None
    return await _enrich_common(Portal.RIGHTMOVE, pid, url, result.data)


async def _enrich_stage2(enriched: dict) -> dict:
    """
    Run vision enrichment on a stage-1 enriched dict.

    Calls in parallel:
      floorplan vision (size fallback + room data)
      carpet vision (bedroom + other areas)
    Then conditionally:
      EPC vision OCR (when stage 1 left epc_rating null and the scrape
      captured an epc_graph_url).

    Floorplan vision is skipped when the scraper already produced size_sqft
    (Zoopla covers this for ~53% of listings). Other vision calls run as
    normal.

    Re-merges size_sqft with the floorplan source now available. Mutates
    and returns the same enriched dict.
    """
    raw = enriched["raw"]
    images = raw.get("images", []) or []
    floorplan_urls = raw.get("floorplan_urls", []) or []
    scraper_size_sqft = raw.get("size_sqft")

    carpet_coro = (
        _detect_carpet_guarded(images) if images and CARPET_ENABLED else None
    )
    # Run floorplan vision whenever a usable URL exists, even if size_sqft is
    # already known. The vision pass also returns per-room sizes, balcony /
    # garden flags, compass orientation, and notes, all of which feed scoring
    # components beyond the size-recovery use case.
    floorplan_coro = (
        extract_floorplan_data(floorplan_urls[0])
        if FLOORPLAN_ENABLED and floorplan_urls
        else None
    )

    carpet_res, floorplan_res = await asyncio.gather(
        carpet_coro if carpet_coro is not None else asyncio.sleep(0, result={}),
        floorplan_coro if floorplan_coro is not None else asyncio.sleep(0, result={}),
    )
    enriched["carpet"] = carpet_res or {}
    enriched["floorplan"] = floorplan_res or {}

    # EPC vision OCR rescue (Wave 1.7) when EPC rating still null.
    if enriched["epc"] is None and enriched.get("epc_graph_url"):
        try:
            vision_res = await extract_epc_from_image(enriched["epc_graph_url"])
        except Exception as exc:
            import structlog as _sl
            _sl.get_logger().warning(
                "epc_vision_unhandled_error",
                url=enriched["epc_graph_url"],
                error=str(exc),
            )
            vision_res = {"confidence": "error", "epc_rating": None}
        if (
            vision_res.get("confidence") == "sap_derived"
            and vision_res.get("epc_rating")
        ):
            enriched["epc"] = vision_res["epc_rating"]
            enriched["epc_rating_source"] = "vision_ocr"
            enriched["epc_rating_confidence"] = "sap_derived"

    # Re-merge size with floorplan vision now available.
    floorplan_size = (
        floorplan_res.get("total_size_sqft")
        if floorplan_res and not floorplan_res.get("error")
        else None
    )
    size_merge_result = merge_size_sqft(
        structured=raw.get("size_sqft"),
        nlp=enriched.get("nlp_size"),
        floorplan_vision=floorplan_size,
        epc_interpolated=None,
        epc_direct=enriched["epc_multifield"].get("size_sqft_fallback"),
        bedrooms=raw.get("bedrooms"),
        listing_uid=enriched["uid"],
    )
    enriched["size_sqft"] = size_merge_result["size_sqft"]
    enriched["size_sqft_source"] = size_merge_result["size_sqft_source"]
    enriched["size_sqft_confidence"] = size_merge_result["size_sqft_confidence"]

    return enriched


def _build_partial_listing(enriched: dict) -> Listing:
    """
    Build a Listing row populated with stage-1 fields only.

    Vision-derived columns (carpet_*, floorplan_data) are left null. Score
    and score_breakdown are also null: the score is computed once after
    stage 2 lands so it reflects the full enrichment set. The result is
    suitable for the stage-1 hard filter.
    """
    raw = enriched["raw"]
    tfl = enriched["tfl"]
    epc_mf = enriched["epc_multifield"] or {}
    price_pcm = _parse_price_pcm(raw.get("price"))

    commute_to = tfl.get("commute_to") or {}
    commute_canary = commute_to.get("canary_wharf")
    commute_white = commute_to.get("whitechapel")

    return Listing(
        uid=enriched["uid"],
        portal=enriched.get("portal", Portal.RIGHTMOVE),
        property_id=enriched["property_id"],
        url=enriched["url"],
        address=raw.get("address"),
        postcode=raw.get("postcode"),
        area=raw.get("area"),
        latitude=enriched["lat"],
        longitude=enriched["lng"],
        price=raw.get("price"),
        price_numeric=price_pcm * 100 if price_pcm else None,
        bedrooms=raw.get("bedrooms"),
        bathrooms=raw.get("bathrooms"),
        size_sqft=enriched["size_sqft"],
        size_sqft_source=enriched["size_sqft_source"],
        size_sqft_confidence=enriched["size_sqft_confidence"],
        property_type=PropertyType.FLAT,
        title=raw.get("title"),
        images=json.dumps(raw.get("images", [])) if raw.get("images") else None,
        agent_phone=raw.get("agent_phone"),
        extraction_method=ExtractionMethod.DIRECT_HTTP,
        tube_distance=tfl.get("tube_distance"),
        cycle_canary_wharf=tfl.get("cycle_canary_wharf"),
        commute_canary_wharf=commute_canary,
        commute_whitechapel=commute_white,
        commute_pass_40min=tfl.get("commute_pass_40min"),
        tfl_zone=tfl.get("tfl_zone"),
        nearest_station=tfl.get("nearest_station"),
        epc_rating=enriched["epc"],
        total_floor_area_sqm=epc_mf.get("total_floor_area_sqm"),
        current_energy_efficiency=epc_mf.get("current_energy_efficiency"),
        potential_energy_efficiency=epc_mf.get("potential_energy_efficiency"),
        number_habitable_rooms=epc_mf.get("number_habitable_rooms"),
        number_heated_rooms=epc_mf.get("number_heated_rooms"),
        built_form=epc_mf.get("built_form"),
        property_type_epc=epc_mf.get("property_type_epc"),
        construction_age_band=epc_mf.get("construction_age_band"),
        tenure_epc=epc_mf.get("tenure_epc"),
        windows_description=epc_mf.get("windows_description"),
        windows_energy_eff=epc_mf.get("windows_energy_eff"),
        walls_description=epc_mf.get("walls_description"),
        walls_energy_eff=epc_mf.get("walls_energy_eff"),
        roof_description=epc_mf.get("roof_description"),
        floor_level=epc_mf.get("floor_level"),
        mains_gas_flag=epc_mf.get("mains_gas_flag"),
        heating_cost_current=epc_mf.get("heating_cost_current"),
        epc_lodgement_date=epc_mf.get("epc_lodgement_date"),
        epc_inspection_date=epc_mf.get("epc_inspection_date"),
        # Stage 2 vision fields: still null.
        carpet_detected=None,
        carpet_confidence=None,
        carpet_in_bedroom=None,
        carpet_other_areas=None,
        bedrooms_with_carpet=None,
        has_living_area_carpet=None,
        photo_count=len(raw.get("images") or []),
        floor_level_int=_parse_floor_level(epc_mf.get("floor_level")),
        size_sqft_credibility=None,
        is_let_agreed=_is_let_agreed_title(raw.get("title")),
        floorplan_data=None,
        garden=raw.get("garden"),
        balcony=raw.get("balcony"),
        furnished=raw.get("furnished"),
        bills_included=raw.get("bills_included"),
        council_tax_band=raw.get("council_tax_band"),
        let_available_date=raw.get("let_available_date"),
        region=enriched["region"],
        score=None,
        score_breakdown=None,
        epc_graph_url=enriched.get("epc_graph_url"),
        epc_rating_source=enriched.get("epc_rating_source"),
        epc_rating_confidence=enriched.get("epc_rating_confidence"),
        first_seen=datetime.utcnow(),
        last_scraped=datetime.utcnow(),
        scrape_count=1,
        is_active=True,
        status="active",
    )


def _finalise_listing(partial: Listing, enriched: dict) -> Listing:
    """
    Merge stage-2 results into a partial Listing and compute the final score.

    Stage 2 may have updated size_sqft (via floorplan vision) and epc_rating
    (via vision OCR) on the enriched dict. Carpet vision results land here
    directly. After this call the listing has every column populated and a
    score computed against the full enrichment set.
    """
    carpet = enriched.get("carpet") or {}
    floorplan = enriched.get("floorplan") or {}

    # Stage 2 may have updated size and EPC.
    partial.size_sqft = enriched["size_sqft"]
    partial.size_sqft_source = enriched["size_sqft_source"]
    partial.size_sqft_confidence = enriched["size_sqft_confidence"]
    partial.epc_rating = enriched["epc"]
    partial.epc_rating_source = enriched.get("epc_rating_source")
    partial.epc_rating_confidence = enriched.get("epc_rating_confidence")

    partial.carpet_detected = carpet.get("carpet_detected")
    partial.carpet_confidence = carpet.get("carpet_confidence")
    partial.carpet_in_bedroom = carpet.get("carpet_in_bedroom")
    partial.carpet_other_areas = carpet.get("carpet_other_areas")
    partial.bedrooms_with_carpet = carpet.get("bedrooms_with_carpet")
    partial.has_living_area_carpet = carpet.get("has_living_area_carpet")
    partial.size_sqft_credibility = _derive_size_credibility(
        partial.size_sqft,
        floorplan if floorplan and not floorplan.get("error") else None,
    )
    partial.floorplan_data = (
        json.dumps(floorplan)
        if floorplan and not floorplan.get("error")
        else None
    )

    raw = enriched["raw"]
    tfl = enriched["tfl"]
    commute_to = tfl.get("commute_to") or {}
    commute_canary = commute_to.get("canary_wharf")
    commute_white = commute_to.get("whitechapel")
    primary_commute = (
        min(v for v in (commute_canary, commute_white) if v is not None)
        if (commute_canary is not None or commute_white is not None)
        else None
    )
    price_pcm = (partial.price_numeric // 100) if partial.price_numeric else None

    # Pre-Wave-3 unblocker 2.1: pass the carpet split flags and region so the
    # scorer produces the eight non-region keys plus region_pref. Reads from
    # persisted columns on the partial (which were just populated above) so the
    # call mirrors the post-write state of the row.
    score, breakdown = compute_score_from_db(
        price_pcm=price_pcm,
        bedrooms=partial.bedrooms,
        size_sqft=partial.size_sqft,
        carpet_detected=partial.carpet_detected,
        carpet_confidence=partial.carpet_confidence or 0.0,
        carpet_in_bedroom=partial.carpet_in_bedroom,
        carpet_other_areas=partial.carpet_other_areas,
        bedrooms_with_carpet=partial.bedrooms_with_carpet,
        has_living_area_carpet=partial.has_living_area_carpet,
        epc_rating=partial.epc_rating,
        commute_mins=primary_commute,
        tfl_zone=partial.tfl_zone,
        garden=partial.garden,
        balcony=partial.balcony,
        region=partial.region,
    )
    partial.score = score
    partial.score_breakdown = json.dumps(breakdown)
    return partial


# Freshness gate: default 1.5h to match the 2h n8n scrape cadence (0.5h jitter
# band). Was 23h when the cadence was daily. Override at the env level
# (FRESHNESS_HOURS), per-invocation via --force-rescrape-all / --max-age, or
# float values (env parsed as float) for sub-hour resolution.
FRESHNESS_HOURS = float(os.environ.get("FRESHNESS_HOURS", "1.5"))


def _extract_uid_from_url(url: str) -> Optional[str]:
    """Pull the rightmove property id out of a URL without the scraper context."""
    m = re.search(r"/properties/(\d+)", url)
    if m:
        return f"rightmove:{m.group(1)}"
    return None


def _extract_zoopla_pid(url: str) -> Optional[str]:
    """Pull the zoopla property id out of a URL. Mirror-class helper that
    avoids holding a reference to the ZooplaHTTPScraper class so tests can
    patch ZooplaHTTPScraper safely."""
    m = re.search(r"/to-rent/details/(\d+)", url)
    if m:
        return m.group(1)
    return None


def _load_rejected_uids(db: Database, candidate_uids: list[str]) -> set[str]:
    """Return the subset of candidates already in a terminal-reject state.

    Listings the user has rejected via the lifecycle CRM should not be
    re-enriched on the daily cron: re-running carpet detection and floorplan
    vision on something the user has already binned wastes Ollama time and
    the row will be filtered out anyway. Returning the rejected uids lets
    the caller skip them entirely (or do a cheap touch only).
    """
    if not candidate_uids:
        return set()
    rejected_states = ("triage_reject", "final_reject", "not_contacted")
    with Session(db.engine) as s:
        rows = s.exec(
            select(Listing.uid).where(
                Listing.uid.in_(candidate_uids),
                Listing.current_status.in_(rejected_states),
            )
        ).all()
    return set(rows)


def _load_fresh_uids(
    db: Database,
    candidate_uids: list[str],
    horizon_hours: float,
) -> set[str]:
    """
    Return the subset of candidate UIDs whose last_scraped is within the
    horizon. Empty set when the horizon is 0 or the candidate list is empty.
    """
    if not candidate_uids or horizon_hours <= 0:
        return set()
    cutoff = datetime.utcnow() - timedelta(hours=horizon_hours)
    with Session(db.engine) as s:
        rows = s.exec(
            select(Listing.uid).where(
                Listing.uid.in_(candidate_uids),
                Listing.last_scraped >= cutoff,
            )
        ).all()
    return set(rows)


_REJECTED_LIFECYCLE_STATES = {"triage_reject", "final_reject", "not_contacted"}


def _touch_existing(db: Database, uid: str) -> bool:
    """
    Cheap-touch update: bump last_scraped and scrape_count on an existing row,
    set is_active and status without re-running enrichment.

    For listings the user has CRM-rejected (current_status in
    _REJECTED_LIFECYCLE_STATES), preserve is_active=False so the rejection
    sticks across cron runs. last_scraped is still bumped so the cron knows
    the URL is still live on the portal.

    Returns True if a row was found and updated, False otherwise.
    """
    now = datetime.utcnow()
    with Session(db.engine) as s:
        existing = s.exec(select(Listing).where(Listing.uid == uid)).first()
        if existing is None:
            return False
        existing.last_scraped = now
        existing.scrape_count = (existing.scrape_count or 0) + 1
        if (existing.current_status or "new") not in _REJECTED_LIFECYCLE_STATES:
            existing.is_active = True
            existing.status = "active"
        s.add(existing)
        s.commit()
    return True


def _upsert(db: Database, listing: Listing) -> None:
    """Insert new listing or update enrichment and score fields on an existing one."""
    with Session(db.engine) as s:
        existing = s.exec(select(Listing).where(Listing.uid == listing.uid)).first()
        if existing:
            for col in (
                "price", "price_numeric", "score", "score_breakdown", "size_sqft",
                "carpet_detected", "carpet_confidence",
                "carpet_in_bedroom", "carpet_other_areas",
                "epc_rating", "tube_distance", "cycle_canary_wharf",
                "commute_canary_wharf", "commute_whitechapel", "commute_pass_40min",
                "tfl_zone", "nearest_station",
                "garden", "balcony", "furnished",
                "bills_included", "council_tax_band", "let_available_date",
                "latitude", "longitude", "region",
                "images", "property_metadata",
                "total_floor_area_sqm", "current_energy_efficiency",
                "potential_energy_efficiency", "number_habitable_rooms",
                "number_heated_rooms", "built_form", "property_type_epc",
                "construction_age_band", "tenure_epc", "windows_description",
                "windows_energy_eff", "walls_description", "walls_energy_eff",
                "roof_description", "floor_level", "mains_gas_flag",
                "heating_cost_current", "epc_lodgement_date", "epc_inspection_date",
                "size_sqft_source", "size_sqft_confidence", "floorplan_data",
                "epc_graph_url", "epc_rating_source", "epc_rating_confidence",
            ):
                setattr(existing, col, getattr(listing, col))
            existing.last_scraped = datetime.utcnow()
            existing.scrape_count = (existing.scrape_count or 0) + 1
            existing.is_active = True
            existing.status = "active"
            s.add(existing)
        else:
            s.add(listing)
        s.commit()


# Wave 2.7 Agent M: enabled-portals plumbing.
# Default is rightmove-only so the byte-identical pre-M behaviour is preserved
# unless the operator opts in. When zoopla is present in the list, the Zoopla
# scraper runs alongside Rightmove and dedup runs once after both finish.
def _parse_enabled_portals() -> list[str]:
    """Read ENABLED_PORTALS env var, normalise to a list of lower-case names.

    Default is ["rightmove"] when the env var is unset. Empty entries are
    dropped. Unknown portal names are passed through unchanged so the caller
    can decide whether to error out.
    """
    raw = os.environ.get("ENABLED_PORTALS", "rightmove")
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return parts or ["rightmove"]


async def _run_rightmove_pipeline(
    profiles: list,
    total: dict,
    hard_filters,
    aggregate_drops_s1: dict,
    aggregate_drops_s2: dict,
    horizon_hours: int,
) -> None:
    """Run the Rightmove discovery, two-stage enrichment, and upsert loop.

    Mutates total, aggregate_drops_s1, aggregate_drops_s2 in place. The
    behaviour here is the pre-Wave 2.7 logic, lifted verbatim out of
    run_pipeline so the multi-portal wrapper can compose it with Zoopla.
    """
    db = Database()
    for profile in profiles:
        location = profile.get("location") or profile.get("name", "")
        region_id = profile.get("region_id") or profile.get("location_id")

        urls = await discover_properties(
            location=location,
            location_id=region_id,
            min_price=profile.get("min_price"),
            max_price=profile.get("max_price"),
            min_bedrooms=profile.get("min_bedrooms"),
            max_bedrooms=profile.get("max_bedrooms"),
            radius=float(profile.get("radius", 1.0)),
            max_results=int(profile.get("max_results", 40)),
            page_delay=float(profile.get("page_delay", 2.0)),
        )
        total["discovered"] += len(urls)

        scrape_delay = float(profile.get("scrape_delay", 1.0))
        pool_sem = asyncio.Semaphore(LISTINGS_POOL_SIZE)

        # Phase 0: freshness gate.
        candidate_uids = [
            u for u in (_extract_uid_from_url(url) for url in urls) if u
        ]
        fresh_uids = _load_fresh_uids(db, candidate_uids, horizon_hours)
        rejected_uids = _load_rejected_uids(db, candidate_uids)
        # Listings the user has rejected via the CRM are touched but not
        # re-enriched -- the freshness gate keeps them visible as "still
        # listed" without burning Ollama on every re-discovery.
        fresh_urls = [
            url for url in urls
            if _extract_uid_from_url(url) in fresh_uids
            or _extract_uid_from_url(url) in rejected_uids
        ]
        stale_urls = [
            url for url in urls
            if _extract_uid_from_url(url) not in fresh_uids
            and _extract_uid_from_url(url) not in rejected_uids
        ]
        if rejected_uids:
            import structlog
            structlog.get_logger().info(
                "rescrape_skip_for_rejected",
                location=location,
                count=len(rejected_uids),
            )
        if fresh_urls:
            import structlog
            structlog.get_logger().info(
                "freshness_gate_applied",
                location=location,
                fresh=len(fresh_urls),
                stale=len(stale_urls),
                horizon_hours=horizon_hours,
            )
        for url in fresh_urls:
            uid = _extract_uid_from_url(url)
            if uid:
                async with _DB_WRITE_LOCK:
                    if _touch_existing(db, uid):
                        total["touched"] += 1

        async with DirectHTTPScraper() as scraper:
            async def _stage1_one(url):
                async with pool_sem:
                    try:
                        enriched = await _enrich_stage1(scraper, url)
                        if not enriched:
                            return ("failed", None)
                        partial = _build_partial_listing(enriched)
                        return ("ok", (partial, enriched))
                    except Exception as exc:
                        import structlog
                        structlog.get_logger().warning(
                            "stage1_listing_failed", url=url, error=str(exc)
                        )
                        return ("failed", None)
                    finally:
                        await asyncio.sleep(scrape_delay)

            stage1_outcomes = await asyncio.gather(
                *[_stage1_one(u) for u in stale_urls]
            )

            stage1_pairs = [r for tag, r in stage1_outcomes if tag == "ok"]
            total["failed"] += sum(1 for tag, _ in stage1_outcomes if tag == "failed")

            stage1_listings = [p for p, _ in stage1_pairs]
            survivors_s1, drops_s1 = apply_hard_filters(stage1_listings, hard_filters)
            for k, v in drops_s1.items():
                aggregate_drops_s1[k] = aggregate_drops_s1.get(k, 0) + v
            total["filtered_stage1"] += len(stage1_listings) - len(survivors_s1)
            survivor_uids_s1 = {l.uid for l in survivors_s1}
            survivor_pairs = [
                (p, e) for p, e in stage1_pairs if p.uid in survivor_uids_s1
            ]

            async def _stage2_one(pair):
                partial, enriched = pair
                async with pool_sem:
                    try:
                        await _enrich_stage2(enriched)
                        _finalise_listing(partial, enriched)
                    except Exception as exc:
                        import structlog
                        structlog.get_logger().warning(
                            "stage2_listing_failed",
                            uid=partial.uid,
                            error=str(exc),
                        )

            await asyncio.gather(*[_stage2_one(p) for p in survivor_pairs])

            stage2_listings = [p for p, _ in survivor_pairs]
            survivors_s2, drops_s2 = apply_hard_filters(stage2_listings, hard_filters)
            for k, v in drops_s2.items():
                aggregate_drops_s2[k] = aggregate_drops_s2.get(k, 0) + v
            total["filtered_stage2"] += len(stage2_listings) - len(survivors_s2)

            for listing in survivors_s2:
                async with _DB_WRITE_LOCK:
                    _upsert(db, listing)
                total["saved"] += 1


async def _run_openrent_pipeline(
    profiles: list,
    total: dict,
    hard_filters,
    aggregate_drops_s1: dict,
    aggregate_drops_s2: dict,
    horizon_hours: int,
) -> None:
    """OpenRent discovery + scrape + upsert (pilot scope, no enrichment yet).

    Pilot scope per docs/plans/2026-05-23-openrent-integration-tdd.md: discovery
    via one search GET with Haversine geo-prune to centroids, detail fetch per
    survivor, mapping to PropertyListing, freshness gate, upsert. Two-stage
    enrichment (TfL, EPC, carpet, score, region, cluster_id) is deliberately
    deferred to the daily-cron-merge follow-up plan once the pilot is stable.

    Profile keys:
      search_url   - full OpenRent search URL (default builds from min/max price+bed)
      centroids    - list of (lat, lng); defaults to Angel/Islington
      radius_km    - Haversine prune threshold (default 2.0)
      max_listings - per-profile cap; env OPENRENT_MAX_LISTINGS overrides when unset
    """
    import structlog
    from datetime import datetime, timezone
    from homehunt.scrapers.openrent_discover import discover_openrent_properties
    from homehunt.scrapers.openrent_http import OpenRentHTTP
    from homehunt.scrapers.openrent_extract import extract_detail
    from homehunt.scrapers.openrent_mapper import to_property_listing

    log = structlog.get_logger()
    db = Database()
    env_cap_raw = os.environ.get("OPENRENT_MAX_LISTINGS")
    env_cap = int(env_cap_raw) if env_cap_raw else None

    for profile in profiles:
        search_url = profile.get("search_url") or (
            "https://www.openrent.co.uk/properties-to-rent/london?term=London"
            f"&prices_min={profile.get('min_price', 1500)}"
            f"&prices_max={profile.get('max_price', 2800)}"
            f"&bedrooms_min={profile.get('min_bedrooms', 1)}"
            f"&bedrooms_max={profile.get('max_bedrooms', 2)}"
        )
        centroids = profile.get("centroids") or [(51.5326, -0.1058)]
        radius_km = float(profile.get("radius_km", 2.0))
        cap = profile.get("max_listings") or env_cap

        candidates = await discover_openrent_properties(
            search_url=search_url,
            centroids=centroids,
            radius_km=radius_km,
        )
        if cap is not None:
            candidates = candidates[: int(cap)]
        total["discovered"] += len(candidates)
        log.info("openrent_discover", profile=profile.get("name"), candidates=len(candidates))

        uids = [f"openrent:{pid}" for pid, _, _ in candidates]
        fresh_uids = _load_fresh_uids(db, uids, horizon_hours)
        rejected_uids = _load_rejected_uids(db, uids)

        async with OpenRentHTTP() as client:
            for pid, lat, lng in candidates:
                uid = f"openrent:{pid}"
                if uid in fresh_uids or uid in rejected_uids:
                    total["touched"] += 1
                    continue
                try:
                    status, body = await client.fetch_detail(pid)
                except Exception as exc:
                    log.warning("openrent_detail_error", pid=pid, error=str(exc))
                    total["failed"] += 1
                    continue
                if status != 200:
                    log.warning("openrent_detail_status", pid=pid, status=status)
                    total["failed"] += 1
                    continue
                parsed = extract_detail(body)
                # Skip listings that don't fit the flat-search shape:
                # "Room in a Shared Flat", missing price, etc. Persisting these
                # would leave REQUIRED fields null and break field-coverage.
                if parsed.get("bedrooms") is None or parsed.get("price_monthly") is None:
                    log.info(
                        "openrent_skip_non_flat",
                        pid=pid,
                        title=parsed.get("title"),
                        reason="bedrooms_or_price_null",
                    )
                    total["failed"] += 1
                    continue
                listing = to_property_listing(
                    parsed=parsed,
                    property_id=pid,
                    search_lat=lat,
                    search_lng=lng,
                )

                # Stage-1 enrichment: TfL by lat/lng (no postcode needed), region.
                from homehunt.tfl import enrich_commute_multi_from_latlon
                tfl_res = await enrich_commute_multi_from_latlon(lat, lng)
                region = resolve_region(lat, lng)

                # Apply hard filter. tfl_zone and region are null-permissive by
                # config so the filter uses the listing's own null values here;
                # enriched values land in the DB upsert below.
                if hard_filters:
                    survivors, drops = apply_hard_filters([listing], hard_filters)
                    if not survivors:
                        for reason, n in drops.items():
                            if n:
                                aggregate_drops_s1[reason] = aggregate_drops_s1.get(reason, 0) + n
                        total["filtered_stage1"] += 1
                        continue

                # Stage-2 enrichment: carpet vision on detail-page photos.
                photos = parsed.get("photos") or []
                carpet_res = (
                    await _detect_carpet_guarded(photos)
                    if photos and CARPET_ENABLED
                    else {}
                )

                # Score.
                price_pcm = (listing.price_numeric // 100) if listing.price_numeric else None
                commute_to = tfl_res.get("commute_to") or {}
                commute_canary = commute_to.get("canary_wharf")
                commute_white = commute_to.get("whitechapel")
                primary_commute = (
                    min(v for v in (commute_canary, commute_white) if v is not None)
                    if any(v is not None for v in (commute_canary, commute_white))
                    else None
                )
                score, breakdown = compute_score_from_db(
                    price_pcm=price_pcm,
                    bedrooms=listing.bedrooms,
                    size_sqft=None,
                    carpet_detected=carpet_res.get("carpet_detected"),
                    carpet_confidence=carpet_res.get("carpet_confidence") or 0.0,
                    carpet_in_bedroom=carpet_res.get("carpet_in_bedroom"),
                    carpet_other_areas=carpet_res.get("carpet_other_areas"),
                    bedrooms_with_carpet=carpet_res.get("bedrooms_with_carpet"),
                    has_living_area_carpet=carpet_res.get("has_living_area_carpet"),
                    epc_rating=listing.epc_rating,
                    commute_mins=primary_commute,
                    tfl_zone=tfl_res.get("tfl_zone"),
                    garden=listing.garden,
                    balcony=None,
                    region=region,
                )

                now = datetime.now(timezone.utc)
                async with _DB_WRITE_LOCK:
                    with Session(db.engine) as s:
                        existing = s.exec(
                            select(Listing).where(Listing.uid == uid)
                        ).first()
                        if existing:
                            existing.title = listing.title or existing.title
                            existing.address = listing.address or existing.address
                            existing.area = listing.area or existing.area
                            existing.latitude = lat
                            existing.longitude = lng
                            existing.price = listing.price or existing.price
                            existing.price_numeric = listing.price_numeric or existing.price_numeric
                            existing.bedrooms = listing.bedrooms or existing.bedrooms
                            existing.bathrooms = listing.bathrooms or existing.bathrooms
                            existing.furnished = listing.furnished or existing.furnished
                            if listing.garden is not None:
                                existing.garden = listing.garden
                            if listing.bills_included is not None:
                                existing.bills_included = listing.bills_included
                            existing.let_available_date = listing.let_available_date or existing.let_available_date
                            existing.epc_rating = listing.epc_rating or existing.epc_rating
                            if listing.images:
                                existing.images = json.dumps(listing.images)
                            # Enrichment fields.
                            existing.nearest_station = tfl_res.get("nearest_station")
                            existing.tube_distance = tfl_res.get("tube_distance")
                            existing.tfl_zone = tfl_res.get("tfl_zone")
                            existing.commute_canary_wharf = commute_canary
                            existing.commute_whitechapel = commute_white
                            existing.commute_pass_40min = tfl_res.get("commute_pass_40min")
                            existing.region = region
                            existing.carpet_in_bedroom = carpet_res.get("carpet_in_bedroom")
                            existing.carpet_other_areas = carpet_res.get("carpet_other_areas")
                            existing.carpet_detected = carpet_res.get("carpet_detected")
                            existing.carpet_confidence = carpet_res.get("carpet_confidence")
                            existing.bedrooms_with_carpet = carpet_res.get("bedrooms_with_carpet")
                            existing.has_living_area_carpet = carpet_res.get("has_living_area_carpet")
                            existing.score = score
                            existing.score_breakdown = json.dumps(breakdown)
                            existing.last_scraped = now
                            existing.scrape_count = (existing.scrape_count or 0) + 1
                            existing.is_active = True
                            existing.status = "active"
                        else:
                            new_row = Listing(
                                uid=uid,
                                portal=Portal.OPENRENT,
                                property_id=str(pid),
                                url=listing.url,
                                title=listing.title,
                                address=listing.address,
                                area=listing.area,
                                postcode=None,
                                latitude=lat,
                                longitude=lng,
                                price=listing.price,
                                price_numeric=listing.price_numeric,
                                bedrooms=listing.bedrooms,
                                bathrooms=listing.bathrooms,
                                furnished=listing.furnished,
                                garden=listing.garden,
                                bills_included=listing.bills_included,
                                let_available_date=listing.let_available_date,
                                epc_rating=listing.epc_rating,
                                images=json.dumps(listing.images) if listing.images else None,
                                extraction_method=ExtractionMethod.DIRECT_HTTP,
                                nearest_station=tfl_res.get("nearest_station"),
                                tube_distance=tfl_res.get("tube_distance"),
                                tfl_zone=tfl_res.get("tfl_zone"),
                                commute_canary_wharf=commute_canary,
                                commute_whitechapel=commute_white,
                                commute_pass_40min=tfl_res.get("commute_pass_40min"),
                                region=region,
                                carpet_in_bedroom=carpet_res.get("carpet_in_bedroom"),
                                carpet_other_areas=carpet_res.get("carpet_other_areas"),
                                carpet_detected=carpet_res.get("carpet_detected"),
                                carpet_confidence=carpet_res.get("carpet_confidence"),
                                bedrooms_with_carpet=carpet_res.get("bedrooms_with_carpet"),
                                has_living_area_carpet=carpet_res.get("has_living_area_carpet"),
                                score=score,
                                score_breakdown=json.dumps(breakdown),
                                first_seen=now,
                                last_scraped=now,
                                scrape_count=1,
                                is_active=True,
                                status="active",
                            )
                            s.add(new_row)
                        s.commit()
                total["saved"] += 1


async def _run_zoopla_pipeline(
    profiles: list,
    total: dict,
    hard_filters,
    aggregate_drops_s1: dict,
    aggregate_drops_s2: dict,
    horizon_hours: int,
) -> None:
    """Run Zoopla discovery, two-stage enrichment, hard filter, and upsert.

    Wave 2.7 M.5 scope: same shape as _run_rightmove_pipeline. Zoopla
    listings flow through the portal-agnostic _enrich_common (TfL, EPC API,
    region, NLP) and then through _enrich_stage2 for vision recovery, with
    the floorplan-vision skip honoured when the scraper already produced
    size_sqft. The two-stage hard filter is applied before and after vision.

    Area derivation and agent_phone for Zoopla are deliberately deferred.
    """
    import structlog
    log = structlog.get_logger()
    db = Database()
    for profile in profiles:
        location = profile.get("location") or profile.get("name", "")
        search_url = build_zoopla_search_url(
            location=location,
            min_price=profile.get("min_price"),
            max_price=profile.get("max_price"),
            min_bedrooms=profile.get("min_bedrooms"),
            max_bedrooms=profile.get("max_bedrooms"),
        )

        scrape_delay = float(profile.get("scrape_delay", 1.0))
        pool_sem = asyncio.Semaphore(LISTINGS_POOL_SIZE)

        async with ZooplaHTTPScraper(
            min_request_interval=scrape_delay
        ) as scraper:
            try:
                urls = await scraper.scrape_search_page(search_url)
            except Exception as exc:
                log.warning("zoopla_search_error", url=search_url, error=str(exc))
                urls = []
            total["discovered"] += len(urls)

            # Phase 0: freshness gate (Zoopla uids are zoopla:<pid>).
            candidate_uids: list[str] = []
            for url in urls:
                pid = _extract_zoopla_pid(url)
                if pid:
                    candidate_uids.append(f"zoopla:{pid}")
            fresh_uids = _load_fresh_uids(db, candidate_uids, horizon_hours)
            rejected_uids = _load_rejected_uids(db, candidate_uids)
            fresh_urls = []
            stale_urls = []
            for url in urls:
                pid = _extract_zoopla_pid(url)
                uid = f"zoopla:{pid}" if pid else None
                if uid and (uid in fresh_uids or uid in rejected_uids):
                    fresh_urls.append(url)
                else:
                    stale_urls.append(url)
            if rejected_uids:
                log.info(
                    "rescrape_skip_for_rejected",
                    portal="zoopla",
                    location=location,
                    count=len(rejected_uids),
                )
            if fresh_urls:
                log.info(
                    "freshness_gate_applied",
                    portal="zoopla",
                    location=location,
                    fresh=len(fresh_urls),
                    stale=len(stale_urls),
                    horizon_hours=horizon_hours,
                )
            for url in fresh_urls:
                pid = _extract_zoopla_pid(url)
                if pid:
                    async with _DB_WRITE_LOCK:
                        if _touch_existing(db, f"zoopla:{pid}"):
                            total["touched"] += 1

            async def _stage1_one(url):
                async with pool_sem:
                    try:
                        result = await scraper.scrape_property(url)
                        if not result.success or not result.data:
                            return ("failed", None)
                        pid = result.property_id or _extract_zoopla_pid(url)
                        if not pid:
                            return ("failed", None)
                        enriched = await _enrich_common(
                            Portal.ZOOPLA, pid, url, result.data
                        )
                        if not enriched:
                            return ("failed", None)
                        partial = _build_partial_listing(enriched)
                        return ("ok", (partial, enriched))
                    except Exception as exc:
                        log.warning(
                            "zoopla_stage1_listing_failed", url=url, error=str(exc)
                        )
                        return ("failed", None)
                    finally:
                        await asyncio.sleep(scrape_delay)

            stage1_outcomes = await asyncio.gather(
                *[_stage1_one(u) for u in stale_urls]
            )
            stage1_pairs = [r for tag, r in stage1_outcomes if tag == "ok"]
            total["failed"] += sum(1 for tag, _ in stage1_outcomes if tag == "failed")

            stage1_listings = [p for p, _ in stage1_pairs]
            survivors_s1, drops_s1 = apply_hard_filters(stage1_listings, hard_filters)
            for k, v in drops_s1.items():
                aggregate_drops_s1[k] = aggregate_drops_s1.get(k, 0) + v
            total["filtered_stage1"] += len(stage1_listings) - len(survivors_s1)
            survivor_uids_s1 = {l.uid for l in survivors_s1}
            survivor_pairs = [
                (p, e) for p, e in stage1_pairs if p.uid in survivor_uids_s1
            ]

            async def _stage2_one(pair):
                partial, enriched = pair
                async with pool_sem:
                    try:
                        await _enrich_stage2(enriched)
                        _finalise_listing(partial, enriched)
                    except Exception as exc:
                        log.warning(
                            "zoopla_stage2_listing_failed",
                            uid=partial.uid,
                            error=str(exc),
                        )

            await asyncio.gather(*[_stage2_one(p) for p in survivor_pairs])

            stage2_listings = [p for p, _ in survivor_pairs]
            survivors_s2, drops_s2 = apply_hard_filters(stage2_listings, hard_filters)
            for k, v in drops_s2.items():
                aggregate_drops_s2[k] = aggregate_drops_s2.get(k, 0) + v
            total["filtered_stage2"] += len(stage2_listings) - len(survivors_s2)

            for listing in survivors_s2:
                async with _DB_WRITE_LOCK:
                    _upsert(db, listing)
                total["saved"] += 1


async def _run_cluster_listings(session) -> int:
    """Wrapper around homehunt.dedup.cluster_listings for testability."""
    return await cluster_listings(session)


async def run_pipeline(
    profiles_override: Optional[list] = None,
    force_rescrape_all: bool = False,
    max_age_hours: Optional[int] = None,
    enabled_portals: Optional[list[str]] = None,
) -> dict:
    """
    Main pipeline entry point.

    Phases:
      0. Freshness gate: every discovered URL whose UID is in the DB and
         was scraped within FRESHNESS_HOURS gets a cheap-touch update only.
         The remaining URLs proceed through the full enrichment.
      A. Run stage-1 enrichment on stale URLs in parallel.
      B. Apply hard filter (cheap-signal gate). Drops are counted by reason.
      C. Run stage-2 vision enrichment on stage-1 survivors only.
      D. Apply hard filter again (size_sqft and epc_rating may have changed
         after vision). Drops are counted separately.
      E. Score and upsert final survivors.

    When enabled_portals contains "zoopla" the Zoopla scraper runs alongside
    Rightmove and homehunt.dedup.cluster_listings is invoked once after both
    finish to attach cluster_id to cross-portal duplicates. Default is
    rightmove-only, preserving pre-Wave 2.7 behaviour byte-identically.

    Args:
      profiles_override: optional list of profile dicts; falls back to
        london-search.yaml when unset.
      force_rescrape_all: skip the freshness gate; re-enrich every URL.
      max_age_hours: override the FRESHNESS_HOURS env default. 0 is
        equivalent to force_rescrape_all.
      enabled_portals: optional list of portal names. Defaults to whatever
        _parse_enabled_portals returns (which reads ENABLED_PORTALS env).

    Returns a summary dict with discovered, touched, saved, failed,
    filtered_stage1, filtered_stage2 counts.
    """
    db = Database()
    db.create_tables()

    if profiles_override is not None:
        profiles = profiles_override
    else:
        _here = os.path.dirname(os.path.abspath(__file__))
        yaml_path = os.environ.get(
            "HOMEHUNT_CONFIG",
            os.path.join(_here, "london-search.yaml"),
        )
        profiles = load_search_profiles(yaml_path)

    horizon_hours = 0 if force_rescrape_all else (
        max_age_hours if max_age_hours is not None else FRESHNESS_HOURS
    )

    if enabled_portals is None:
        enabled_portals = _parse_enabled_portals()

    total: dict = {
        "discovered": 0,
        "touched": 0,
        "saved": 0,
        "failed": 0,
        "filtered_stage1": 0,
        "filtered_stage2": 0,
    }

    hard_filters = load_hard_filters(_FILTER_SCORING_CONFIG_PATH)
    aggregate_drops_s1: dict[str, int] = {}
    aggregate_drops_s2: dict[str, int] = {}

    if "rightmove" in enabled_portals:
        await _run_rightmove_pipeline(
            profiles, total, hard_filters,
            aggregate_drops_s1, aggregate_drops_s2, horizon_hours,
        )

    if "zoopla" in enabled_portals:
        await _run_zoopla_pipeline(
            profiles, total, hard_filters,
            aggregate_drops_s1, aggregate_drops_s2, horizon_hours,
        )

    if "openrent" in enabled_portals:
        openrent_profiles = load_openrent_profiles(yaml_path)
        await _run_openrent_pipeline(
            openrent_profiles, total, hard_filters,
            aggregate_drops_s1, aggregate_drops_s2, horizon_hours,
        )

    # Dedup runs once after every portal that opted in has contributed
    # listings. Hoisted out of the zoopla branch (2026-05-23) so OpenRent rows
    # get cluster_id assigned in the same run via the coord-proximity branch
    # in homehunt.dedup._pair_matches.
    multi_portal = sum(1 for p in ("rightmove", "zoopla", "openrent") if p in enabled_portals) >= 2
    if multi_portal:
        async with db.async_session() as session:
            await _run_cluster_listings(session)

    # Single structured log line summarising the two-stage filter run.
    import structlog
    structlog.get_logger().info(
        "two_stage_filter_applied",
        discovered=total["discovered"],
        touched=total["touched"],
        saved=total["saved"],
        failed=total["failed"],
        filtered_stage1=total["filtered_stage1"],
        filtered_stage2=total["filtered_stage2"],
        drops_stage1=aggregate_drops_s1,
        drops_stage2=aggregate_drops_s2,
        horizon_hours=horizon_hours,
        enabled_portals=enabled_portals,
    )
    print(
        f"Two-stage filter: discovered={total['discovered']}, "
        f"touched={total['touched']}, saved={total['saved']}, "
        f"failed={total['failed']}, "
        f"filtered_stage1={total['filtered_stage1']} {aggregate_drops_s1}, "
        f"filtered_stage2={total['filtered_stage2']} {aggregate_drops_s2}"
    )

    return total


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run the rental-engine end-to-end pipeline."
    )
    p.add_argument(
        "--force-rescrape-all",
        action="store_true",
        help="Skip the freshness gate and re-enrich every discovered URL.",
    )
    p.add_argument(
        "--max-age",
        type=int,
        default=None,
        help=(
            "Override the freshness horizon in hours. Default is the "
            "FRESHNESS_HOURS env var (default 23). 0 is equivalent to "
            "--force-rescrape-all."
        ),
    )
    return p


if __name__ == "__main__":
    args = _build_arg_parser().parse_args()
    result = asyncio.run(
        run_pipeline(
            force_rescrape_all=args.force_rescrape_all,
            max_age_hours=args.max_age,
        )
    )
    print(result)
