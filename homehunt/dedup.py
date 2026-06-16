"""
Wave 2.7 Agent L: cross-portal deduplication.

Problem: the same physical flat may be scraped from both Rightmove and Zoopla
(or twice from one portal under different listing IDs) and produce two or more
PropertyListing rows. The naive (portal, property_id) UID does not deduplicate
them. This module computes a separate cluster identity over Listing rows.

Cluster predicate (strict, primary):
    full UK postcode equality
    + bedrooms equality (both non-null, equal)
    + price_pcm within +/- 2 percent (abs(a-b) / min(a, b) <= 0.02)

Geo agreement is a secondary tiebreaker that reinforces the primary match
when both portals report coordinates within 50 metres. Geo disagreement does
NOT split a cluster: Rightmove's stored lat/lng is unreliable per the Wave 2.7
Phase 1 closeout (some Rightmove rows are 1 to 2.5 km off the actual property),
so disagreement just means the secondary signal abstains.

Cluster contract: transitive closure. If A pairs with B and B pairs with C,
A and C end up in the same cluster, even if A and C do not pair directly.

Public surface:
  cluster_listings(session) -> int
      Run clustering across all rows in the listing table and return the
      total cluster count after the run.

  find_or_create_cluster(listing, session) -> int
      Idempotent assignment for one listing. Returns its cluster_id.

  choose_primary(members) -> str
      Pick the canonical uid for a set of cluster members.

  get_cluster_assignments(session) -> dict[str, int]
      Helper for tests and the viewer; maps uid -> cluster_id.

  ensure_cluster_schema(connection)
      Idempotent DDL helper used by tests; production runs the alembic
      migration in alembic/versions/ instead.

Out of scope (deliberately not implemented):
  - Photo perceptual-hash tiebreaker (Approach C in the strategy doc).
    The strategy gates that on a precision shortfall on the labelled set.
  - Address text embeddings (Approach D).
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

# Match-predicate threshold. Tightened from the strategy doc's 5 percent band
# in two stages: first to 3 percent because Rightmove geo is unreliable and
# cannot serve as a tiebreaker, then to 2 percent after a labelled-fixture
# spot-check exposed a false positive on N19 3JQ at the 2.63 percent gap (two
# distinct units in adjacent Holloway Road buildings, see fixture pair 9).
# Trade-off: false negatives on real cross-portal duplicates whose prices
# legitimately drift more than 2 percent. Strategy prioritises precision over
# recall; if production data shows recall pain, revisit with a photo or
# address tiebreaker rather than relaxing this constant.
PRICE_BAND_TOLERANCE = 0.02

# Secondary tiebreaker. Geo within this radius reinforces a match; geo
# disagreement does not split.
GEO_AGREEMENT_RADIUS_M = 50.0

# Earth radius used by the haversine helper.
_EARTH_RADIUS_M = 6_371_000.0

# A separate metadata registry so that listing_cluster is NOT auto-created by
# SQLModel.metadata.create_all() in production code paths that do not run the
# alembic migration. Tests call ensure_cluster_schema explicitly.
_dedup_metadata = MetaData()

listing_cluster_table = Table(
    "listing_cluster",
    _dedup_metadata,
    Column("cluster_id", Integer, primary_key=True, autoincrement=True),
    Column("centroid_lat", Float, nullable=True),
    Column("centroid_lng", Float, nullable=True),
    Column("bedrooms", Integer, nullable=False),
    Column("price_pcm_min", Integer, nullable=False),
    Column("price_pcm_max", Integer, nullable=False),
    Column("member_count", Integer, nullable=False, default=1),
    Column("primary_uid", String, nullable=False),
    Column("first_seen", String, nullable=False),
    Column("last_seen", String, nullable=False),
)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def ensure_cluster_schema(connection: Connection) -> None:
    """
    Create the listing_cluster table and add listing.cluster_id if missing.
    Idempotent; safe to call repeatedly. Used by tests against in-memory
    SQLite. Production runs the alembic migration in alembic/versions/.
    """
    inspector = inspect(connection)
    if "listing_cluster" not in inspector.get_table_names():
        listing_cluster_table.create(connection)

    if "listing" in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns("listing")}
        if "cluster_id" not in existing_cols:
            connection.execute(
                text("ALTER TABLE listing ADD COLUMN cluster_id INTEGER")
            )


# ---------------------------------------------------------------------------
# Predicate helpers
# ---------------------------------------------------------------------------

_POSTCODE_RE = re.compile(
    r"^([A-Z]{1,2}[0-9][A-Z0-9]?) ?([0-9][A-Z]{2})$"
)


def _normalise_postcode(value: Any) -> str | None:
    """Canonicalise a UK postcode to upper case with a single internal space."""
    if value is None:
        return None
    s = str(value).strip().upper().replace("  ", " ")
    if not s:
        return None
    m = _POSTCODE_RE.match(s.replace(" ", ""))
    if m:
        outward, inward = m.group(1), m.group(2)
        return f"{outward} {inward}"
    return None


def _price_pcm(listing: Any) -> int | None:
    """
    Read the per-month rent in pounds from a Listing row. The DB stores
    price_numeric in pence; tests pass price_numeric directly.
    """
    raw = getattr(listing, "price_numeric", None)
    if raw is None:
        return None
    try:
        return int(raw) // 100
    except (TypeError, ValueError):
        return None


def _within_price_band(a_pcm: int, b_pcm: int) -> bool:
    if a_pcm <= 0 or b_pcm <= 0:
        return False
    lo = min(a_pcm, b_pcm)
    return abs(a_pcm - b_pcm) / lo <= PRICE_BAND_TOLERANCE


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _geo_distance_m(a: Any, b: Any) -> float | None:
    la, lo_a = getattr(a, "latitude", None), getattr(a, "longitude", None)
    lb, lo_b = getattr(b, "latitude", None), getattr(b, "longitude", None)
    if None in (la, lo_a, lb, lo_b):
        return None
    return _haversine_m(la, lo_a, lb, lo_b)


def _pair_matches(a: Any, b: Any) -> bool:
    """The cluster predicate. See module docstring.

    Default key: full-postcode equality + bedrooms equal + price within 2%.
    OpenRent branch: when either side has portal='openrent' OR either side lacks
    a postcode, fall back to Haversine <= 50m + bedrooms equal + price within 2%.
    OpenRent only exposes the outcode in its title so postcode equality is
    structurally impossible across portals for OpenRent rows.
    """
    ba = getattr(a, "bedrooms", None)
    bb = getattr(b, "bedrooms", None)
    if ba is None or bb is None or ba != bb:
        return False

    pra = _price_pcm(a)
    prb = _price_pcm(b)
    if pra is None or prb is None or not _within_price_band(pra, prb):
        return False

    portal_a = getattr(a, "portal", None)
    portal_b = getattr(b, "portal", None)
    pa = _normalise_postcode(getattr(a, "postcode", None))
    pb = _normalise_postcode(getattr(b, "postcode", None))

    use_coord_branch = (
        portal_a == "openrent" or portal_b == "openrent" or not pa or not pb
    )
    if use_coord_branch:
        dist = _geo_distance_m(a, b)
        return dist is not None and dist <= 50.0

    return pa == pb


# ---------------------------------------------------------------------------
# choose_primary
# ---------------------------------------------------------------------------

_ENRICHMENT_FIELDS = (
    "size_sqft",
    "epc_rating",
    "floorplan_data",
    "tube_distance",
    "commute_canary_wharf",
)


def _enrichment_count(listing: Any) -> int:
    return sum(
        1 for f in _ENRICHMENT_FIELDS if getattr(listing, f, None) not in (None, "")
    )


def _portal_name(listing: Any) -> str:
    portal = getattr(listing, "portal", None)
    if portal is None:
        return ""
    return getattr(portal, "value", str(portal))


def choose_primary(members: Sequence[Any]) -> str:
    """
    Pick the canonical uid for a cluster.

    Tiebreak order:
      1. higher count of populated enrichment fields wins
      2. more recent first_seen wins
      3. rightmove wins (its data dictionary is more battle-tested at write time)
      4. lexical uid as a final stable fallback
    """
    if not members:
        raise ValueError("choose_primary called with no members")

    def _key(m: Any):
        first_seen = getattr(m, "first_seen", None) or datetime.min
        return (
            -_enrichment_count(m),
            -first_seen.timestamp() if isinstance(first_seen, datetime) else 0,
            0 if _portal_name(m) == "rightmove" else 1,
            getattr(m, "uid", "") or "",
        )

    return sorted(members, key=_key)[0].uid


# ---------------------------------------------------------------------------
# Union-find over a batch
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self, items: Iterable[str]):
        self._parent: dict[str, str] = {x: x for x in items}

    def find(self, x: str) -> str:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, x: str, y: str) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self._parent[rx] = ry

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for x in self._parent:
            r = self.find(x)
            out.setdefault(r, []).append(x)
        return out


# ---------------------------------------------------------------------------
# Cluster row helpers
# ---------------------------------------------------------------------------

def _centroid_or_null(members: Sequence[Any]) -> tuple[float | None, float | None]:
    """
    Return the mean lat/lng of members whose coordinates agree pairwise
    within GEO_AGREEMENT_RADIUS_M. If any member disagrees with another by
    more than that radius (Rightmove's stored coords can be wildly off), the
    centroid is null and the cluster row records no centroid.
    """
    coords = [
        (getattr(m, "latitude", None), getattr(m, "longitude", None))
        for m in members
        if getattr(m, "latitude", None) is not None
        and getattr(m, "longitude", None) is not None
    ]
    if not coords:
        return None, None

    for i in range(len(coords)):
        for j in range(i + 1, len(coords)):
            d = _haversine_m(coords[i][0], coords[i][1], coords[j][0], coords[j][1])
            if d > GEO_AGREEMENT_RADIUS_M:
                return None, None

    lat = sum(c[0] for c in coords) / len(coords)
    lng = sum(c[1] for c in coords) / len(coords)
    return lat, lng


def _to_iso(dt: Any) -> str:
    if isinstance(dt, datetime):
        return dt.isoformat()
    if dt is None:
        return datetime.utcnow().isoformat()
    return str(dt)


# ---------------------------------------------------------------------------
# Async DB driver
# ---------------------------------------------------------------------------

async def _load_all_listings(session: AsyncSession) -> list[Any]:
    from homehunt.core.db import Listing

    result = await session.execute(select(Listing))
    return list(result.scalars().all())


async def cluster_listings(session: AsyncSession) -> int:
    """
    Cluster every row in the listing table.

    Idempotent: re-running on a clustered table reproduces the same
    cluster_id assignment. Listings already attached to a cluster keep that
    cluster_id when the predicate continues to hold among current members;
    the rebuild rewrites cluster rows from scratch each time so that
    centroid, member_count, price band, and primary_uid stay in sync with
    the membership.
    """
    listings = await _load_all_listings(session)
    if not listings:
        return 0

    # Stable order so that test assertions on assignment ids are deterministic
    # given the same input data.
    listings.sort(key=lambda x: x.uid)

    by_uid = {m.uid: m for m in listings}
    uf = _UnionFind(by_uid)

    n = len(listings)
    for i in range(n):
        for j in range(i + 1, n):
            if _pair_matches(listings[i], listings[j]):
                uf.union(listings[i].uid, listings[j].uid)

    groups = uf.groups()

    # Build cluster rows in deterministic order so that cluster_id values
    # match across re-runs.
    ordered_groups = sorted(
        groups.values(), key=lambda g: min(by_uid[u].uid for u in g)
    )

    # Drop and rewrite cluster rows. Cheaper than diffing membership
    # graphs for the volumes we care about (a few thousand listings/day),
    # and it keeps centroid and price-band fields trivially in sync.
    await session.execute(text("UPDATE listing SET cluster_id = NULL"))
    await session.execute(text("DELETE FROM listing_cluster"))

    cluster_count = 0
    for members_uids in ordered_groups:
        members = [by_uid[u] for u in members_uids]
        primary_uid = choose_primary(members)

        prices = [
            _price_pcm(m) for m in members if _price_pcm(m) is not None
        ]
        if not prices:
            # Predicate already excludes price-null pairs from merging, so
            # any clustered group has at least one priced member. Singletons
            # can be price-null though.
            price_min = price_max = 0
        else:
            price_min, price_max = min(prices), max(prices)

        bedrooms = next((m.bedrooms for m in members if m.bedrooms is not None), 0) or 0

        first_seen_vals = [
            getattr(m, "first_seen", None) for m in members
            if getattr(m, "first_seen", None) is not None
        ]
        last_seen_vals = [
            getattr(m, "last_scraped", None) for m in members
            if getattr(m, "last_scraped", None) is not None
        ]
        first_seen_iso = _to_iso(min(first_seen_vals) if first_seen_vals else None)
        last_seen_iso = _to_iso(max(last_seen_vals) if last_seen_vals else None)

        centroid_lat, centroid_lng = _centroid_or_null(members)

        result = await session.execute(
            listing_cluster_table.insert().values(
                centroid_lat=centroid_lat,
                centroid_lng=centroid_lng,
                bedrooms=bedrooms,
                price_pcm_min=price_min,
                price_pcm_max=price_max,
                member_count=len(members),
                primary_uid=primary_uid,
                first_seen=first_seen_iso,
                last_seen=last_seen_iso,
            )
        )
        cluster_id = result.inserted_primary_key[0]
        cluster_count += 1

        for uid in members_uids:
            await session.execute(
                text("UPDATE listing SET cluster_id = :cid WHERE uid = :uid").bindparams(
                    cid=cluster_id, uid=uid
                )
            )

    await session.commit()
    return cluster_count


async def find_or_create_cluster(listing: Any, session: AsyncSession) -> int:
    """
    Assign a single listing to a cluster. Looks for any existing member that
    matches the predicate; if found, joins that cluster and updates the
    cluster row. Otherwise creates a fresh cluster row.

    Note: this entry point is for stream-time use, where listings arrive one
    at a time. It does NOT recompute transitive closure across the whole
    table; for that, call cluster_listings.
    """
    from homehunt.core.db import Listing

    result = await session.execute(select(Listing))
    candidates = list(result.scalars().all())

    matched_cluster_ids: set[int] = set()
    for other in candidates:
        if other.uid == listing.uid:
            continue
        if other.cluster_id is None:
            continue
        if _pair_matches(listing, other):
            matched_cluster_ids.add(other.cluster_id)

    if matched_cluster_ids:
        cluster_id = min(matched_cluster_ids)
        members = [c for c in candidates if c.cluster_id == cluster_id]
        members.append(listing)
        primary_uid = choose_primary(members)
        prices = [_price_pcm(m) for m in members if _price_pcm(m) is not None]
        price_min = min(prices) if prices else 0
        price_max = max(prices) if prices else 0
        centroid_lat, centroid_lng = _centroid_or_null(members)
        await session.execute(
            listing_cluster_table.update()
            .where(listing_cluster_table.c.cluster_id == cluster_id)
            .values(
                primary_uid=primary_uid,
                price_pcm_min=price_min,
                price_pcm_max=price_max,
                member_count=len(members),
                centroid_lat=centroid_lat,
                centroid_lng=centroid_lng,
                last_seen=_to_iso(max(
                    [getattr(m, "last_scraped", None) for m in members
                     if getattr(m, "last_scraped", None) is not None]
                    or [datetime.utcnow()]
                )),
            )
        )
    else:
        bedrooms = listing.bedrooms or 0
        price = _price_pcm(listing) or 0
        result = await session.execute(
            listing_cluster_table.insert().values(
                centroid_lat=getattr(listing, "latitude", None),
                centroid_lng=getattr(listing, "longitude", None),
                bedrooms=bedrooms,
                price_pcm_min=price,
                price_pcm_max=price,
                member_count=1,
                primary_uid=listing.uid,
                first_seen=_to_iso(getattr(listing, "first_seen", None)),
                last_seen=_to_iso(getattr(listing, "last_scraped", None)),
            )
        )
        cluster_id = result.inserted_primary_key[0]

    await session.execute(
        text("UPDATE listing SET cluster_id = :cid WHERE uid = :uid").bindparams(
            cid=cluster_id, uid=listing.uid
        )
    )
    await session.commit()
    return cluster_id


async def get_cluster_assignments(session: AsyncSession) -> dict[str, int]:
    """uid -> cluster_id map, used by tests and the viewer."""
    result = await session.execute(
        text("SELECT uid, cluster_id FROM listing WHERE cluster_id IS NOT NULL")
    )
    return {row[0]: row[1] for row in result.fetchall()}
