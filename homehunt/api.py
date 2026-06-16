"""
FastAPI web UI for browsing scored property listings.

Reads directly from the homehunt SQLite database and renders a score-sorted
HTML table. Colour-coded: green >= 80, amber 60-79, red < 60.

Usage:
    uvicorn homehunt.api:app --host 127.0.0.1 --port 8000 --reload

Endpoints:
    GET /          -- Score-sorted HTML table, auto-refreshes every 60s
    GET /viewer    -- Card-list viewer with sort, filter, paginate, EPC panel
    GET /api/listings -- JSON listing data
    POST /run      -- Trigger a scrape cycle (runs homehunt CLI in subprocess)
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from homehunt.viewer_query import (
    EPC_BANDS,
    SORT_OPTIONS,
    TAG_FILTER_COLUMNS,
    get_listing,
    list_listings,
)
from homehunt.latent_tags import LATENT_TAG_AXES

app = FastAPI(title="HomeHunt", description="Rental listing scorer")

# (value, label) pairs for the viewer newness dropdown. Values must match the
# keys of homehunt.viewer_query.NEWNESS_HOURS.
NEWNESS_OPTIONS = [
    ("3h", "last 3 hours"),
    ("6h", "last 6 hours"),
    ("12h", "last 12 hours"),
    ("24h", "last 24 hours"),
    ("3d", "last 3 days"),
    ("1w", "last week"),
    ("1m", "last month"),
]

TAG_FILTER_DESCRIPTION = "comma-separated tag values; OR within, AND with other tag axes"
TAG_AXIS_ORDER = [
    "window_style",
    "flooring",
    "building_era",
    "exposed_brick",
    "open_plan",
    "outdoor_access",
    "natural_light",
    "wall_palette",
    "kitchen_finish",
    "view",
    "exposed_beams_or_ducts",
    "ceiling_height",
    "bathroom_finish",
]
TAG_OPTIONS = {
    axis: [value for value in LATENT_TAG_AXES[axis] if value not in {"unclear", "not_shown"}]
    for axis in TAG_AXIS_ORDER
}

# Project root is one directory above homehunt/.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = _PROJECT_ROOT / "templates"
_STATIC_DIR = _PROJECT_ROOT / "static"

# Mount static assets (CSS only at present). Skip mounting in unusual layouts
# where the directory does not exist; the viewer will still render with
# unstyled fallbacks rather than 500.
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

DB_PATH = os.environ.get("HOMEHUNT_DB", "homehunt.db")
ALERT_THRESHOLD = int(os.environ.get("ALERT_THRESHOLD", "60"))


def _get_db_rows() -> list[dict]:
    """Read listings from SQLite directly (synchronous, lightweight)."""
    import sqlite3

    db_path = Path(DB_PATH)
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            """
            SELECT
                uid, portal, url, title, price, price_numeric, bedrooms,
                property_type, area, postcode, address,
                garden, balcony,
                tube_distance, cycle_canary_wharf,
                commute_canary_wharf, commute_whitechapel, commute_pass_40min,
                size_sqft,
                epc_rating, carpet_detected, carpet_confidence,
                carpet_in_bedroom, carpet_other_areas,
                score, property_metadata,
                first_seen, last_scraped, is_active
            FROM listing
            WHERE is_active = 1
            ORDER BY score DESC NULLS LAST, price_numeric ASC
            """
        )
        return [dict(row) for row in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def _score_colour(score: Optional[int]) -> str:
    if score is None:
        return "#888"
    if score >= 80:
        return "#2e7d32"  # green
    if score >= ALERT_THRESHOLD:
        return "#f57c00"  # amber
    return "#c62828"  # red


def _score_bg(score: Optional[int]) -> str:
    if score is None:
        return "#f5f5f5"
    if score >= 80:
        return "#e8f5e9"
    if score >= ALERT_THRESHOLD:
        return "#fff3e0"
    return "#ffebee"


def _format_price(price_numeric: Optional[int]) -> str:
    if price_numeric is None:
        return "—"
    return f"£{price_numeric // 100:,}/mo"


def _parse_breakdown(row: dict) -> dict:
    """Extract score breakdown from property_metadata JSON blob."""
    metadata_raw = row.get("property_metadata")
    if not metadata_raw:
        return {}
    try:
        metadata = json.loads(metadata_raw)
        return metadata.get("score_breakdown") or {}
    except Exception:
        return {}


@app.get("/", response_class=HTMLResponse)
async def index():
    """Score-sorted property table with auto-refresh."""
    rows = _get_db_rows()
    total = len(rows)
    above_threshold = sum(1 for r in rows if r.get("score") is not None and r["score"] >= ALERT_THRESHOLD)
    last_updated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    table_rows = []
    for row in rows:
        score = row.get("score")
        breakdown = _parse_breakdown(row)
        colour = _score_colour(score)
        bg = _score_bg(score)

        score_display = f'<span style="font-weight:bold;color:{colour}">{score if score is not None else "—"}</span>'

        breakdown_html = ""
        if breakdown:
            parts = [f"{k}:{v}" for k, v in breakdown.items()]
            breakdown_html = f'<div style="font-size:0.75em;color:#666">{" | ".join(parts)}</div>'

        cw = row.get("commute_canary_wharf")
        wc = row.get("commute_whitechapel")
        primary = min(v for v in (cw, wc) if v is not None) if (cw or wc) else None
        commute_str = f"{primary}m" if primary else "-"

        epc = row.get("epc_rating") or "—"
        carpet = row.get("carpet_detected")
        carpet_str = "Yes" if carpet is True else ("No" if carpet is False else "—")

        portal = (row.get("portal") or "").title()
        url = row.get("url") or "#"
        title = row.get("title") or row.get("address") or "Unknown"
        area = row.get("area") or row.get("postcode") or "—"
        beds = row.get("bedrooms") or "—"
        price_str = _format_price(row.get("price_numeric"))

        features = []
        if row.get("garden"):
            features.append("garden")
        if row.get("balcony"):
            features.append("balcony")
        features_str = ", ".join(features) if features else "-"

        table_rows.append(f"""
        <tr style="background:{bg}">
          <td style="text-align:center">{score_display}{breakdown_html}</td>
          <td><a href="{url}" target="_blank">{title[:60]}</a><br><small style="color:#666">{portal}</small></td>
          <td>{area}</td>
          <td style="text-align:center">{beds}</td>
          <td style="text-align:right">{price_str}</td>
          <td style="text-align:center">{commute_str}</td>
          <td style="text-align:center">{epc}</td>
          <td style="text-align:center">{carpet_str}</td>
          <td>{features_str}</td>
        </tr>
        """)

    table_body = "\n".join(table_rows) if table_rows else '<tr><td colspan="9" style="text-align:center;padding:2em">No listings yet. Click "Run Now" to scrape.</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>HomeHunt</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; padding: 1em 2em; background: #fafafa; }}
    h1 {{ margin-bottom: 0.25em; }}
    .meta {{ color: #666; font-size: 0.9em; margin-bottom: 1em; }}
    .actions {{ margin-bottom: 1.5em; }}
    .btn {{ padding: 0.5em 1.2em; background: #1565c0; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 0.95em; }}
    .btn:hover {{ background: #0d47a1; }}
    table {{ border-collapse: collapse; width: 100%; background: #fff; border-radius: 6px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    th {{ background: #1565c0; color: #fff; padding: 0.7em 0.8em; text-align: left; font-size: 0.85em; }}
    td {{ padding: 0.6em 0.8em; border-bottom: 1px solid #e0e0e0; font-size: 0.88em; vertical-align: top; }}
    tr:last-child td {{ border-bottom: none; }}
    a {{ color: #1565c0; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    #run-status {{ margin-left: 1em; color: #666; font-size: 0.9em; }}
  </style>
</head>
<body>
  <h1>HomeHunt</h1>
  <div class="meta">{total} listings &mdash; {above_threshold} above threshold &mdash; Last updated: {last_updated} (auto-refresh 60s)</div>
  <div class="actions">
    <button class="btn" onclick="triggerRun()">Run Now</button>
    <span id="run-status"></span>
  </div>
  <table>
    <thead>
      <tr>
        <th style="width:90px">Score</th>
        <th>Listing</th>
        <th>Area</th>
        <th>Beds</th>
        <th>Price</th>
        <th>Commute</th>
        <th>EPC</th>
        <th>Carpet</th>
        <th>Features</th>
      </tr>
    </thead>
    <tbody>
      {table_body}
    </tbody>
  </table>
  <script>
    async function triggerRun() {{
      const status = document.getElementById('run-status');
      status.textContent = 'Starting scrape...';
      try {{
        const r = await fetch('/run', {{method: 'POST'}});
        const d = await r.json();
        status.textContent = d.message || 'Done.';
      }} catch(e) {{
        status.textContent = 'Error: ' + e.message;
      }}
    }}
  </script>
</body>
</html>"""

    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# /viewer (Wave 2 Agent G)
# ---------------------------------------------------------------------------

# Sort aliases. Accept the implementation plan's underscore form as the
# canonical query value, but also accept compact variants.
_SORT_ALIASES = {
    "score": "score_desc",
    "score_desc": "score_desc",
    "price": "price_asc",
    "price_asc": "price_asc",
    "sqft": "sqft_desc",
    "sqft_desc": "sqft_desc",
    "first_seen": "first_seen_desc",
    "first_seen_desc": "first_seen_desc",
    "newest": "first_seen_desc",
    "transition_asc": "transition_asc",
    "transition_desc": "transition_desc",
}


def _normalise_sort(raw: Optional[str]) -> str:
    if not raw:
        return "score_desc"
    return _SORT_ALIASES.get(raw.lower(), "score_desc")


def _build_query_url(base_filters: dict, sort: str, page: int, **overrides) -> str:
    """Construct a /viewer query string with the given filters plus overrides.

    Used by the pagination links in the template so each page link preserves
    the active filters and sort.
    """
    merged = dict(base_filters)
    merged["sort"] = sort
    merged["page"] = page
    merged.update({k: v for k, v in overrides.items() if v is not None})
    cleaned = {
        k: v for k, v in merged.items()
        if v not in (None, "", "score_desc")
        # keep page=1 in the URL for clarity even though it is the default
    }
    if not cleaned:
        return "/viewer"
    return "/viewer?" + urlencode(cleaned, doseq=True)


def _split_tag_values(values: Optional[List[str]]) -> list[str]:
    """Normalize repeated and comma-separated tag query params to a flat list."""
    if not values:
        return []
    out: list[str] = []
    for value in values:
        for item in str(value).split(","):
            item = item.strip()
            if item:
                out.append(item)
    return out


def _tag_filter_value(values: Optional[List[str]]) -> Optional[str]:
    selected = _split_tag_values(values)
    return ",".join(selected) if selected else None


def _tag_filter_values_by_key(**tag_values: Optional[List[str]]) -> tuple[dict[str, str], dict[str, list[str]]]:
    query_filters: dict[str, str] = {}
    selected_tags: dict[str, list[str]] = {}
    for axis in TAG_AXIS_ORDER:
        key = f"tag_{axis}"
        selected = _split_tag_values(tag_values.get(key))
        selected_tags[axis] = selected
        if selected:
            query_filters[key] = ",".join(selected)
    return query_filters, selected_tags


@app.get("/api/listings")
async def api_listings(
    tag_window_style: Optional[List[str]] = Query(None, alias="tag_window_style", description=TAG_FILTER_DESCRIPTION),
    tag_ceiling_height: Optional[List[str]] = Query(None, alias="tag_ceiling_height", description=TAG_FILTER_DESCRIPTION),
    tag_exposed_brick: Optional[List[str]] = Query(None, alias="tag_exposed_brick", description=TAG_FILTER_DESCRIPTION),
    tag_exposed_beams_or_ducts: Optional[List[str]] = Query(None, alias="tag_exposed_beams_or_ducts", description=TAG_FILTER_DESCRIPTION),
    tag_building_era: Optional[List[str]] = Query(None, alias="tag_building_era", description=TAG_FILTER_DESCRIPTION),
    tag_kitchen_finish: Optional[List[str]] = Query(None, alias="tag_kitchen_finish", description=TAG_FILTER_DESCRIPTION),
    tag_bathroom_finish: Optional[List[str]] = Query(None, alias="tag_bathroom_finish", description=TAG_FILTER_DESCRIPTION),
    tag_flooring: Optional[List[str]] = Query(None, alias="tag_flooring", description=TAG_FILTER_DESCRIPTION),
    tag_natural_light: Optional[List[str]] = Query(None, alias="tag_natural_light", description=TAG_FILTER_DESCRIPTION),
    tag_wall_palette: Optional[List[str]] = Query(None, alias="tag_wall_palette", description=TAG_FILTER_DESCRIPTION),
    tag_open_plan: Optional[List[str]] = Query(None, alias="tag_open_plan", description=TAG_FILTER_DESCRIPTION),
    tag_view: Optional[List[str]] = Query(None, alias="tag_view", description=TAG_FILTER_DESCRIPTION),
    tag_outdoor_access: Optional[List[str]] = Query(None, alias="tag_outdoor_access", description=TAG_FILTER_DESCRIPTION),
):
    """JSON endpoint returning active viewer listings, including tag filters."""
    filters, _ = _tag_filter_values_by_key(**{
        "tag_window_style": tag_window_style,
        "tag_ceiling_height": tag_ceiling_height,
        "tag_exposed_brick": tag_exposed_brick,
        "tag_exposed_beams_or_ducts": tag_exposed_beams_or_ducts,
        "tag_building_era": tag_building_era,
        "tag_kitchen_finish": tag_kitchen_finish,
        "tag_bathroom_finish": tag_bathroom_finish,
        "tag_flooring": tag_flooring,
        "tag_natural_light": tag_natural_light,
        "tag_wall_palette": tag_wall_palette,
        "tag_open_plan": tag_open_plan,
        "tag_view": tag_view,
        "tag_outdoor_access": tag_outdoor_access,
    })
    result = list_listings(filters=filters, sort="score_desc", page=1, page_size=1000)
    return JSONResponse(content=result["listings"])


@app.get("/viewer", response_class=HTMLResponse)
async def viewer(
    request: Request,
    tab: Optional[str] = Query(None, description="main | queued | contacted | shortlisted | rejected (default main)"),
    sort: Optional[str] = Query(None, description="score_desc, price_asc, sqft_desc, first_seen_desc, transition_asc, transition_desc"),
    source: Optional[str] = Query(None, description="rightmove, zoopla, openrent"),
    region: Optional[str] = Query(None, description="substring match against region column"),
    beds: Optional[str] = Query(None, description="exact bedroom count"),
    epc: Optional[str] = Query(None, description="single EPC band letter A-G"),
    max_price: Optional[str] = Query(None, description="cap on price_pcm in pounds (parsed to int; empty string is treated as no filter)"),
    min_sqft: Optional[str] = Query(None, description="floor on size_sqft (parsed to int; empty string is treated as no filter)"),
    glazing: Optional[str] = Query(None, description="substring match against windows_description"),
    built_form: Optional[str] = Query(None, description="exact built_form match"),
    min_score: Optional[str] = Query(None, description="minimum score (0-100); listings below are excluded"),
    available_from: Optional[str] = Query(None, description="ISO YYYY-MM-DD floor on let_available_iso"),
    include_unknown: Optional[str] = Query(None, description="when truthy, also include listings with null/empty let_available_iso"),
    newness: Optional[str] = Query(None, description="recency bucket on first_seen: 3h, 6h, 12h, 24h, 3d, 1w, 1m"),
    tag_window_style: Optional[List[str]] = Query(None, alias="tag_window_style", description=TAG_FILTER_DESCRIPTION),
    tag_ceiling_height: Optional[List[str]] = Query(None, alias="tag_ceiling_height", description=TAG_FILTER_DESCRIPTION),
    tag_exposed_brick: Optional[List[str]] = Query(None, alias="tag_exposed_brick", description=TAG_FILTER_DESCRIPTION),
    tag_exposed_beams_or_ducts: Optional[List[str]] = Query(None, alias="tag_exposed_beams_or_ducts", description=TAG_FILTER_DESCRIPTION),
    tag_building_era: Optional[List[str]] = Query(None, alias="tag_building_era", description=TAG_FILTER_DESCRIPTION),
    tag_kitchen_finish: Optional[List[str]] = Query(None, alias="tag_kitchen_finish", description=TAG_FILTER_DESCRIPTION),
    tag_bathroom_finish: Optional[List[str]] = Query(None, alias="tag_bathroom_finish", description=TAG_FILTER_DESCRIPTION),
    tag_flooring: Optional[List[str]] = Query(None, alias="tag_flooring", description=TAG_FILTER_DESCRIPTION),
    tag_natural_light: Optional[List[str]] = Query(None, alias="tag_natural_light", description=TAG_FILTER_DESCRIPTION),
    tag_wall_palette: Optional[List[str]] = Query(None, alias="tag_wall_palette", description=TAG_FILTER_DESCRIPTION),
    tag_open_plan: Optional[List[str]] = Query(None, alias="tag_open_plan", description=TAG_FILTER_DESCRIPTION),
    tag_view: Optional[List[str]] = Query(None, alias="tag_view", description=TAG_FILTER_DESCRIPTION),
    tag_outdoor_access: Optional[List[str]] = Query(None, alias="tag_outdoor_access", description=TAG_FILTER_DESCRIPTION),
    page: int = Query(1, ge=1, description="page number (1-indexed)"),
):
    """Read-only card-list viewer.

    Per docs/viewer-and-calibration-loop.md section 5 and DESIGN.md tokens.
    """
    page_size = 20
    valid_tabs = {"main", "queued", "contacted", "shortlisted", "rejected"}
    tab = (tab or "main").lower()
    if tab not in valid_tabs:
        tab = "main"

    # Default sort changes per tab so the tab opens at the most useful order.
    default_sort_per_tab = {
        "main": "score_desc",
        "queued": "transition_asc",       # oldest queued surfaces first (chase agents)
        "contacted": "transition_asc",    # oldest contacted surfaces first (chase reply)
        "shortlisted": "transition_asc",  # working-set; oldest viewing surfaces first
        "rejected": "transition_desc",    # recently rejected surfaces first
    }
    canonical_sort = _normalise_sort(sort) if sort else default_sort_per_tab[tab]

    raw_scalar_filters = {
        "tab": tab,
        "source": source,
        "region": region,
        "beds": beds,
        "epc": epc,
        "max_price": max_price,
        "min_sqft": min_sqft,
        "glazing": glazing,
        "built_form": built_form,
        "min_score": min_score,
        "available_from": available_from,
        "include_unknown": include_unknown,
        "newness": newness,
    }
    tag_query_filters, selected_tags = _tag_filter_values_by_key(**{
        "tag_window_style": tag_window_style,
        "tag_ceiling_height": tag_ceiling_height,
        "tag_exposed_brick": tag_exposed_brick,
        "tag_exposed_beams_or_ducts": tag_exposed_beams_or_ducts,
        "tag_building_era": tag_building_era,
        "tag_kitchen_finish": tag_kitchen_finish,
        "tag_bathroom_finish": tag_bathroom_finish,
        "tag_flooring": tag_flooring,
        "tag_natural_light": tag_natural_light,
        "tag_wall_palette": tag_wall_palette,
        "tag_open_plan": tag_open_plan,
        "tag_view": tag_view,
        "tag_outdoor_access": tag_outdoor_access,
    })
    # Drop empty values for the listing query and for URL re-building.
    scalar_filters = {k: v for k, v in raw_scalar_filters.items() if v not in (None, "")}
    filters = {**scalar_filters, **tag_query_filters}
    url_filters = {
        **scalar_filters,
        **{f"tag_{axis}": values for axis, values in selected_tags.items() if values},
    }

    result = list_listings(
        filters=filters,
        sort=canonical_sort,
        page=page,
        page_size=page_size,
    )

    distinct = result["distinct"]

    def build_query(*, page: int) -> str:
        return _build_query_url(url_filters, canonical_sort, page=page)

    from homehunt.lifecycle import load_reasons as _load_lifecycle_reasons
    from homehunt.viewer_query import tab_counts as _tab_counts
    reasons_yaml = _load_lifecycle_reasons()
    reject_reasons = reasons_yaml.get("triage_reject", [])
    not_contacted_reasons = reasons_yaml.get("not_contacted", [])
    final_reject_reasons = reasons_yaml.get("final_reject", [])

    context = {
        "request": request,
        "listings": result["listings"],
        "total_listings": result["total"],
        "shown_listings": len(result["listings"]),
        "page": result["page"],
        "total_pages": result["total_pages"],
        "tab": tab,
        "tab_counts": _tab_counts(),
        "sort": canonical_sort,
        "source": source,
        "region": region,
        "beds": beds,
        "epc": epc,
        "max_price": max_price,
        "min_sqft": min_sqft,
        "glazing": glazing,
        "built_form": built_form,
        "min_score": min_score,
        "available_from": available_from,
        "include_unknown": include_unknown,
        "newness": newness,
        "tag_window_style": _tag_filter_value(tag_window_style),
        "tag_ceiling_height": _tag_filter_value(tag_ceiling_height),
        "tag_exposed_brick": _tag_filter_value(tag_exposed_brick),
        "tag_exposed_beams_or_ducts": _tag_filter_value(tag_exposed_beams_or_ducts),
        "tag_building_era": _tag_filter_value(tag_building_era),
        "tag_kitchen_finish": _tag_filter_value(tag_kitchen_finish),
        "tag_bathroom_finish": _tag_filter_value(tag_bathroom_finish),
        "tag_flooring": _tag_filter_value(tag_flooring),
        "tag_natural_light": _tag_filter_value(tag_natural_light),
        "tag_wall_palette": _tag_filter_value(tag_wall_palette),
        "tag_open_plan": _tag_filter_value(tag_open_plan),
        "tag_view": _tag_filter_value(tag_view),
        "tag_outdoor_access": _tag_filter_value(tag_outdoor_access),
        "tag_filter_columns": TAG_FILTER_COLUMNS,
        "tag_options": TAG_OPTIONS,
        "selected_tags": selected_tags,
        "sort_options": SORT_OPTIONS,
        "source_options": distinct["portals"],
        "region_options": distinct["regions"],
        "beds_options": [0, 1, 2, 3, 4],
        "epc_options": EPC_BANDS,
        "newness_options": NEWNESS_OPTIONS,
        "glazing_options": distinct["glazings"],
        "built_form_options": distinct["built_forms"],
        "build_query": build_query,
        "viewer_redirect_to": _build_query_url(url_filters, canonical_sort, page=page),
        "reject_reasons": reject_reasons,
        "not_contacted_reasons": not_contacted_reasons,
        "final_reject_reasons": final_reject_reasons,
    }

    return templates.TemplateResponse("viewer.html", context)


@app.post("/listing/{uid}/transition")
async def listing_transition(
    uid: str,
    to_status: str = Form(...),
    reason: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    redirect_to: str = Form("/viewer"),
):
    """Transition a listing to a new lifecycle state.

    Form-driven (not JSON) so the viewer's per-card form posts directly
    without JavaScript. Validation is enforced inside lifecycle.transition;
    illegal transitions return 400.
    """
    from homehunt.lifecycle import IllegalTransition, transition as do_transition

    try:
        do_transition(
            uid=uid,
            to_status=to_status,
            reason=(reason or None),
            notes=(notes or None),
            transitioned_by="user",
        )
    except IllegalTransition as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return RedirectResponse(url=redirect_to, status_code=303)


@app.get("/listing/{uid}", response_class=HTMLResponse)
async def listing_detail(
    request: Request,
    uid: str,
    from_tab: Optional[str] = Query(None, description="tab the user came from (main | queued | contacted | shortlisted | rejected)"),
    return_to: Optional[str] = Query(None, description="full viewer URL to redirect to after a transition; preserves filter state"),
):
    """Per-listing detail page (Wave 2.6).

    Renders the full surface for one listing: photo carousel, score
    breakdown bars expanded by default, per-room floorplan extraction,
    EPC panel with all 19 fields, commute info, and a 'View on Rightmove'
    CTA at the bottom. The lifecycle action panel (reject / contact /
    shortlist / final-reject etc) lives at the bottom of the page; the
    outer card view only shows a status badge.

    `from_tab` round-trips the originating tab so the redirect after a
    transition (and the back link) lands on the right tab.
    """
    listing = get_listing(uid)
    if listing is None:
        raise HTTPException(status_code=404, detail=f"Listing {uid} not found")

    valid_tabs = {"main", "queued", "contacted", "shortlisted", "rejected"}
    from_tab = (from_tab or "main").lower()
    if from_tab not in valid_tabs:
        from_tab = "main"

    from homehunt.lifecycle import get_history as _lifecycle_history
    from homehunt.lifecycle import load_reasons as _load_lifecycle_reasons
    reasons_yaml = _load_lifecycle_reasons()

    return templates.TemplateResponse(
        "listing.html",
        {
            "request": request,
            "listing": listing,
            "from_tab": from_tab,
            "return_to": return_to,
            "lifecycle_history": _lifecycle_history(uid, db_path=os.environ.get("HOMEHUNT_DB", "homehunt.db")),
            "reject_reasons": reasons_yaml.get("triage_reject", []),
            "not_contacted_reasons": reasons_yaml.get("not_contacted", []),
            "final_reject_reasons": reasons_yaml.get("final_reject", []),
        },
    )


@app.post("/run")
async def run_scrape():
    """
    Trigger a homehunt scrape cycle in the background.
    Runs: python -m homehunt run-config london-search.yaml
    """
    config_file = os.environ.get("HOMEHUNT_CONFIG", "london-search.yaml")

    try:
        # Resolve the pipeline entry point relative to this package so the
        # endpoint works from any checkout. Use the current interpreter (the one
        # running the API), which is the project venv in any sane deployment.
        project_root = Path(__file__).resolve().parent.parent
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(project_root / "run.py"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        # Fire-and-forget — return immediately, let it run in background
        asyncio.create_task(_wait_proc(proc))
        return {"message": f"Scrape started (config: {config_file})"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


async def _wait_proc(proc):
    """Background task: stream subprocess output to container logs."""
    async for line in proc.stdout:
        print(f"[scrape] {line.decode().rstrip()}", flush=True)
    await proc.wait()
    print(f"[scrape] finished (rc={proc.returncode})", flush=True)
