"""
Read-only query helpers for the /viewer route.

Reads the homehunt SQLite database directly via sqlite3 (the same lightweight
pattern used by the existing index route in homehunt/api.py). Pure functions:
no FastAPI imports, no globals other than the env var that locates the db.

Layout:
  list_listings(filters, sort, page, page_size) -> {listings, total, page, total_pages, distinct}

The 'listings' values are plain dicts pre-decorated for template rendering
(price_display formatted, images parsed from JSON, score_breakdown parsed,
floor area sqft derived from sqm, has_epc_panel flag computed). Templates
do no further data shaping beyond conditional formatting.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Optional

from homehunt.latent_tags import LATENT_TAG_AXES


SORT_OPTIONS = [
    {"value": "score_desc", "label": "score (high to low)"},
    {"value": "price_asc", "label": "price (low to high)"},
    {"value": "sqft_desc", "label": "size (high to low)"},
    {"value": "first_seen_desc", "label": "newest first"},
]

EPC_BANDS = ["A", "B", "C", "D", "E", "F", "G"]

TAG_FILTER_COLUMNS = [f"tag_{axis}" for axis in LATENT_TAG_AXES]
TAG_FILTER_EXCLUDED_VALUES = {"unclear", "not_shown"}

# Viewer newness filter: map the user-facing bucket key to a horizon in hours.
# Compared against first_seen (the listing's first-discovery timestamp). Used by
# _build_where. Keys are the only accepted query values; anything else is ignored.
NEWNESS_HOURS = {
    "3h": 3,
    "6h": 6,
    "12h": 12,
    "24h": 24,
    "3d": 72,
    "1w": 168,
    "1m": 720,
}

# Columns we read from the listing table. Keep this list explicit so the
# rendered card has a stable contract regardless of future schema changes.
_COLUMNS = [
    "uid",
    "portal",
    "url",
    "title",
    "address",
    "postcode",
    "area",
    "price",
    "price_numeric",
    "bedrooms",
    "size_sqft",
    "epc_rating",
    "garden",
    "balcony",
    "images",
    "first_seen",
    "last_scraped",
    "is_active",
    "score",
    "score_breakdown",
    "region",
    "tube_distance",
    "nearest_station",
    "tfl_zone",
    "commute_canary_wharf",
    "commute_whitechapel",
    "commute_pass_40min",
    "carpet_in_bedroom",
    "carpet_other_areas",
    "bedrooms_with_carpet",
    "has_living_area_carpet",
    "current_status",
    "current_status_reason",
    # Wave 2.7 dedup -- cross-portal cluster identity. The viewer renders a
    # tiny chip on cards belonging to a multi-member cluster so the user
    # can spot dupes during sift; the detail page renders a one-line note
    # naming the other portal(s) carrying the same physical flat.
    "cluster_id",
    # EPC multi-field
    "total_floor_area_sqm",
    "current_energy_efficiency",
    "potential_energy_efficiency",
    "number_habitable_rooms",
    "number_heated_rooms",
    "built_form",
    "property_type_epc",
    "construction_age_band",
    "tenure_epc",
    "windows_description",
    "windows_energy_eff",
    "walls_description",
    "walls_energy_eff",
    "roof_description",
    "floor_level",
    "mains_gas_flag",
    "heating_cost_current",
    "epc_lodgement_date",
    "epc_inspection_date",
    # Provenance + extras
    "size_sqft_source",
    "size_sqft_confidence",
    "floorplan_data",
    "epc_graph_url",
    "epc_rating_source",
    "epc_rating_confidence",
    "furnished",
    "council_tax_band",
    "let_available_date",
    "let_available_iso",
    # Latent visual tags (Block B1/B2). These flow through unchanged so the
    # viewer and downstream JSON consumers can render/filter without re-querying.
    *TAG_FILTER_COLUMNS,
    "tags_extracted_at",
    "tag_raw_captions",
]


def _db_path() -> str:
    return os.environ.get("HOMEHUNT_DB", "homehunt.db")


def _open() -> Optional[sqlite3.Connection]:
    path = _db_path()
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _existing_columns(conn: sqlite3.Connection) -> set[str]:
    """Return the set of column names actually present on the listing table.

    Older databases that pre-date later wave migrations are missing some
    columns (e.g. epc_graph_url from Wave 1.7). The viewer must render
    against any of these snapshots without 500ing, so we filter the SELECT
    list to columns that actually exist.
    """
    try:
        rows = conn.execute("PRAGMA table_info(listing)").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {row[1] for row in rows}


def _has_cluster_table(conn: sqlite3.Connection) -> bool:
    """True when the listing_cluster table exists on this DB.

    The viewer must continue to render against pre-Wave-2.7 snapshots that
    have no cluster table; we just skip the chip in that case.
    """
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'listing_cluster'"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _cluster_member_counts(conn: sqlite3.Connection,
                            cluster_ids: list[int]) -> dict[int, int]:
    """Return {cluster_id: member_count} for the given ids.

    Reads the canonical ``member_count`` column on listing_cluster (kept in
    sync by homehunt.dedup). Falls back to a COUNT(*) over the listing table
    when the row is missing (e.g. fixture inconsistencies).
    """
    if not cluster_ids:
        return {}
    if not _has_cluster_table(conn):
        return {}
    placeholders = ",".join("?" for _ in cluster_ids)
    try:
        rows = conn.execute(
            f"SELECT cluster_id, member_count FROM listing_cluster "
            f"WHERE cluster_id IN ({placeholders})",
            cluster_ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {row[0]: int(row[1]) for row in rows}
    # Backstop: any cluster_id not in listing_cluster gets a fresh COUNT.
    missing = [cid for cid in cluster_ids if cid not in out]
    if missing:
        ph2 = ",".join("?" for _ in missing)
        try:
            rows = conn.execute(
                f"SELECT cluster_id, COUNT(*) FROM listing "
                f"WHERE cluster_id IN ({ph2}) AND is_active = 1 "
                f"GROUP BY cluster_id",
                missing,
            ).fetchall()
            for row in rows:
                out[row[0]] = int(row[1])
        except sqlite3.OperationalError:
            pass
    return out


def _cluster_other_members(conn: sqlite3.Connection,
                            cluster_id: int,
                            self_uid: str) -> list[dict]:
    """Return the other live members of ``cluster_id`` as [{uid, portal}, ...].

    Used by the listing detail page to render the cross-portal note. Filters
    out the listing itself by uid. Empty list when the cluster has only one
    member or the table is absent.
    """
    if cluster_id is None:
        return []
    try:
        rows = conn.execute(
            "SELECT uid, portal FROM listing "
            "WHERE cluster_id = ? AND uid != ? AND is_active = 1 "
            "ORDER BY portal, uid",
            [cluster_id, self_uid],
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"uid": r[0], "portal": r[1]} for r in rows]


def _format_price(price_numeric: Optional[int], price_text: Optional[str]) -> str:
    """Format the stored pence-ish int as a human price string.

    Many existing rows store rent in pence (£2,500 -> 250000). Some early-
    pipeline rows stored the pound amount directly. Detect by magnitude:
    > 50000 means pence and we divide; otherwise treat as pounds.
    """
    if price_numeric is None:
        if price_text:
            return price_text
        return "--"
    pounds = price_numeric // 100 if price_numeric > 50000 else int(price_numeric)
    return f"£{pounds:,} pcm"


def _parse_images(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, list):
        return [str(x) for x in parsed if x]
    return []


def _parse_breakdown(raw: Any) -> dict[str, int]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in parsed.items():
        try:
            out[str(k)] = int(round(float(v)))
        except (TypeError, ValueError):
            continue
    return out


def _parse_floorplan(raw: Any) -> Optional[dict]:
    """Decode the floorplan_data JSON column into a dict for templates.

    Returns None when the column is null, empty, or unparseable. The dict
    structure follows homehunt/floorplan_vision.py: total_size_sqft,
    total_size_sqm, rooms (list), room_count (dict), has_balcony,
    has_garden, compass, raw_notes. The error key, if present, is a
    string; templates can show it for debugging.
    """
    if raw is None or raw == "":
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict) or parsed.get("error"):
        return None
    return parsed


def _decorate(row: dict) -> dict:
    """Convert a raw sqlite row dict into a template-ready dict.

    Keeps the input keys intact and adds derived fields:
      price_display, images (list), score_breakdown (dict),
      floorplan (dict or None), total_floor_area_sqft, has_epc_panel.
    """
    out = dict(row)
    out["price_display"] = _format_price(row.get("price_numeric"), row.get("price"))
    out["images"] = _parse_images(row.get("images"))
    out["score_breakdown"] = _parse_breakdown(row.get("score_breakdown"))
    out["floorplan"] = _parse_floorplan(row.get("floorplan_data"))
    sqm = row.get("total_floor_area_sqm")
    out["total_floor_area_sqft"] = round(sqm * 10.7639) if sqm else None
    # EPC panel renders when at least one of the 20 EPC-derived columns is
    # populated. Conservative: rating alone is enough because that signals an
    # API hit even when other fields are NULL on older rows.
    epc_signals = [
        row.get("epc_rating"),
        row.get("total_floor_area_sqm"),
        row.get("current_energy_efficiency"),
        row.get("built_form"),
        row.get("windows_description"),
        row.get("walls_description"),
    ]
    out["has_epc_panel"] = any(v not in (None, "") for v in epc_signals)
    # Cluster fields default to a single-member sentinel when the upstream
    # query did not attach cluster info (older callers; pre-2.7 DBs).
    if "cluster_member_count" not in out:
        out["cluster_member_count"] = 1
    out["cluster_other_count"] = max(0, int(out.get("cluster_member_count", 1)) - 1)
    out["is_cluster_dupe"] = out["cluster_other_count"] >= 1
    # Other-portal members for the detail-page cross-portal note. Producers
    # populate ``cluster_other_members`` (list of {uid, portal} dicts).
    if "cluster_other_members" not in out:
        out["cluster_other_members"] = []
    return out


def _coerce_bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        if v.lower() in ("1", "true", "t", "yes", "y"):
            return True
        if v.lower() in ("0", "false", "f", "no", "n"):
            return False
    return None


_TAB_STATUS_FILTERS: dict[str, tuple[str, list]] = {
    # main: untouched listings only -- new or null current_status, still active.
    "main": ("is_active = 1 AND (current_status IS NULL OR current_status = 'new')", []),
    # queued: waiting on the user to reach out / agent to reply, pre-contact.
    "queued": ("is_active = 1 AND current_status = 'contact_queued'", []),
    # contacted: agent replied / viewing booked, awaiting outcome decision.
    "contacted": ("is_active = 1 AND current_status = 'contacted'", []),
    # shortlisted: working set the user is actively viewing.
    "shortlisted": ("is_active = 1 AND current_status = 'shortlisted'", []),
    # rejected: terminal-reject states (is_active is 0 for these by construction).
    "rejected": ("current_status IN ('triage_reject', 'final_reject', 'not_contacted')", []),
}


def tab_counts(db_path: Optional[str] = None) -> dict[str, int]:
    """Return per-tab counts for the viewer header.

    Cheap: four SELECT COUNT(*) queries against the production DB.
    """
    db_path = db_path or _db_path()
    out: dict[str, int] = {}
    con = sqlite3.connect(db_path)
    try:
        for tab, (clause, params) in _TAB_STATUS_FILTERS.items():
            cur = con.execute(f"SELECT COUNT(*) FROM listing WHERE {clause}", params)
            out[tab] = int(cur.fetchone()[0])
    finally:
        con.close()
    return out


def _build_where(filters: dict, present: Optional[set[str]] = None) -> tuple[str, list]:
    tab = (filters.get("tab") or "main").lower()
    base_clause, base_params = _TAB_STATUS_FILTERS.get(tab, _TAB_STATUS_FILTERS["main"])
    where = [base_clause]
    params: list = list(base_params)
    has = present if present is not None else set(_COLUMNS)

    source = filters.get("source")
    if source and "portal" in has:
        # DB stores portal as the enum NAME (uppercase: 'RIGHTMOVE', 'ZOOPLA',
        # 'OPENRENT'). User-facing query params arrive lowercase. Compare
        # uppercase on both sides for a stable match.
        where.append("upper(portal) = ?")
        params.append(source.upper())

    region = filters.get("region")
    if region and "region" in has:
        where.append("lower(region) = lower(?)")
        params.append(region)

    beds = filters.get("beds")
    if beds and "bedrooms" in has:
        try:
            where.append("bedrooms = ?")
            params.append(int(beds))
        except (TypeError, ValueError):
            pass

    epc = filters.get("epc")
    if epc and "epc_rating" in has:
        where.append("upper(epc_rating) = ?")
        params.append(str(epc).upper())

    max_price = filters.get("max_price")
    if max_price and "price_numeric" in has:
        try:
            cap_pence = int(max_price) * 100
            where.append("(price_numeric IS NULL OR price_numeric <= ?)")
            params.append(cap_pence)
        except (TypeError, ValueError):
            pass

    min_sqft = filters.get("min_sqft")
    if min_sqft and "size_sqft" in has:
        try:
            where.append("(size_sqft IS NULL OR size_sqft >= ?)")
            params.append(int(min_sqft))
        except (TypeError, ValueError):
            pass

    glazing = filters.get("glazing")
    if glazing and "windows_description" in has:
        where.append("(windows_description IS NOT NULL AND lower(windows_description) LIKE ?)")
        params.append(f"%{glazing.lower()}%")

    built_form = filters.get("built_form")
    if built_form and "built_form" in has:
        where.append("lower(built_form) = ?")
        params.append(built_form.lower())

    min_score = filters.get("min_score")
    if min_score and "score" in has:
        try:
            where.append("(score IS NOT NULL AND score >= ?)")
            params.append(int(min_score))
        except (TypeError, ValueError):
            pass

    available_from = filters.get("available_from")
    if available_from and "let_available_iso" in has:
        if filters.get("include_unknown"):
            where.append("(let_available_iso >= ? OR let_available_iso IS NULL OR let_available_iso = '')")
        else:
            where.append("let_available_iso >= ?")
        params.append(available_from)

    newness = filters.get("newness")
    if newness and "first_seen" in has:
        hours = NEWNESS_HOURS.get(str(newness))
        if hours is not None:
            # first_seen is naive-UTC "YYYY-MM-DD HH:MM:SS.ffffff"; datetime('now')
            # is UTC, so this prefix-comparison is correct. NULL first_seen excluded
            # (a listing with no discovery timestamp is not meaningfully "new").
            where.append("(first_seen IS NOT NULL AND first_seen >= datetime('now', ?))")
            params.append(f"-{hours} hours")

    for column in TAG_FILTER_COLUMNS:
        if column not in has:
            continue
        raw_value = filters.get(column)
        if not raw_value:
            continue
        values = [
            item.strip()
            for item in str(raw_value).split(",")
            if item.strip() and item.strip() not in TAG_FILTER_EXCLUDED_VALUES
        ]
        if not values:
            continue
        placeholders = ", ".join("?" for _ in values)
        where.append(f"{column} IN ({placeholders})")
        params.extend(values)

    return " AND ".join(where), params


def _build_order_by(sort: str) -> str:
    """Map the user-facing sort key to a deterministic ORDER BY clause.

    Always include uid as a tie-breaker so pagination is stable.
    """
    if sort == "price_asc":
        return "price_numeric ASC NULLS LAST, uid ASC"
    if sort == "sqft_desc":
        return "size_sqft DESC NULLS LAST, uid ASC"
    if sort == "first_seen_desc":
        return "first_seen DESC NULLS LAST, uid ASC"
    if sort == "transition_desc":
        return "last_transition_at DESC NULLS LAST, uid ASC"
    if sort == "transition_asc":
        return "last_transition_at ASC NULLS LAST, uid ASC"
    # default: score desc
    return "score DESC NULLS LAST, uid ASC"


def list_listings(
    filters: Optional[dict] = None,
    sort: str = "score_desc",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Run the listing query.

    Returns a dict with:
      listings: list of decorated dicts (one per surviving row, paginated)
      total: total count after filters
      page: clamped to >= 1 and <= total_pages (or 1 when total_pages is 0)
      total_pages: max(1, ceil(total / page_size))
      distinct: dict of {portals: [...], regions: [...], glazings: [...], built_forms: [...]}
                used to populate filter dropdowns
    """
    filters = filters or {}
    page = max(1, int(page) if page else 1)
    page_size = max(1, int(page_size))

    conn = _open()
    if conn is None:
        return {
            "listings": [],
            "total": 0,
            "page": 1,
            "total_pages": 1,
            "distinct": {"portals": [], "regions": [], "glazings": [], "built_forms": []},
        }

    try:
        present = _existing_columns(conn)
        # Only request columns the listing table actually has. Missing columns
        # on older db snapshots default to None so the templates degrade
        # gracefully instead of raising sqlite3.OperationalError.
        cols = [c for c in _COLUMNS if c in present]
        missing = [c for c in _COLUMNS if c not in present]

        where_sql, params = _build_where(filters, present=present)
        order_sql = _build_order_by(sort)

        count_sql = f"SELECT COUNT(*) FROM listing WHERE {where_sql}"
        total = conn.execute(count_sql, params).fetchone()[0]
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            page = total_pages

        offset = (page - 1) * page_size

        cols_sql = ", ".join(cols) if cols else "uid"
        rows_sql = (
            f"SELECT {cols_sql} FROM listing "
            f"WHERE {where_sql} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?"
        )
        cur = conn.execute(rows_sql, params + [page_size, offset])
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            for col in missing:
                d.setdefault(col, None)
            rows.append(d)

        # Coerce common nullable bools so Jinja conditions read cleanly.
        for r in rows:
            for k in ("garden", "balcony", "carpet_in_bedroom", "carpet_other_areas",
                      "has_living_area_carpet", "commute_pass_40min"):
                if k in r:
                    r[k] = _coerce_bool(r[k])

        # Resolve cluster member counts for the page's listings. Single
        # round-trip even when the page has many clusters; restricted to
        # cluster ids actually on the page so this stays cheap.
        cluster_ids = sorted({
            int(r["cluster_id"]) for r in rows
            if r.get("cluster_id") is not None
        })
        member_counts = _cluster_member_counts(conn, cluster_ids)
        for r in rows:
            cid = r.get("cluster_id")
            if cid is None:
                r["cluster_member_count"] = 1
            else:
                r["cluster_member_count"] = int(member_counts.get(cid, 1))

        decorated = [_decorate(r) for r in rows]

        # Distinct dropdown values come from the unfiltered active set so the
        # UI does not jitter between requests. Each query is gated on the
        # column existing on the current schema.
        def _distinct(col: str) -> list:
            if col not in present:
                return []
            try:
                rows = conn.execute(
                    f"SELECT DISTINCT {col} FROM listing "
                    f"WHERE is_active = 1 AND {col} IS NOT NULL ORDER BY {col}"
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            return [r[0] for r in rows]

        distinct = {
            "portals": _distinct("portal"),
            "regions": _distinct("region"),
            "glazings": _distinct("windows_description"),
            "built_forms": _distinct("built_form"),
        }

        return {
            "listings": decorated,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "distinct": distinct,
        }
    finally:
        conn.close()


def get_listing(uid: str) -> Optional[dict]:
    """Fetch a single decorated listing by uid.

    Returns the decorated dict (same shape as a row from list_listings) or
    None when the uid is unknown or the DB cannot be opened. Used by the
    /listing/{uid} detail route.
    """
    if not uid:
        return None
    conn = _open()
    if conn is None:
        return None
    try:
        present = _existing_columns(conn)
        cols = [c for c in _COLUMNS if c in present]
        missing = [c for c in _COLUMNS if c not in present]
        cols_sql = ", ".join(cols) if cols else "uid"
        row = conn.execute(
            f"SELECT {cols_sql} FROM listing WHERE uid = ?",
            [uid],
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        for col in missing:
            d.setdefault(col, None)
        for k in ("garden", "balcony", "carpet_in_bedroom", "carpet_other_areas",
                  "commute_pass_40min"):
            if k in d:
                d[k] = _coerce_bool(d[k])

        # Cross-portal cluster info for the detail-page note.
        cid = d.get("cluster_id")
        if cid is None:
            d["cluster_member_count"] = 1
            d["cluster_other_members"] = []
        else:
            counts = _cluster_member_counts(conn, [int(cid)])
            d["cluster_member_count"] = int(counts.get(int(cid), 1))
            d["cluster_other_members"] = _cluster_other_members(
                conn, int(cid), d.get("uid", "")
            )

        return _decorate(d)
    finally:
        conn.close()
