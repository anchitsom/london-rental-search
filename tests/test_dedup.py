"""
Wave 2.7 Agent L: cross-portal deduplication tests.

Project convention: plain async script, NOT pytest. Run via
.venv/bin/python tests/test_dedup.py from the project root.

Test cases:
  Synthetic (predicate behaviour, in-memory DB)
    1. Same postcode + beds + price within 2% form one cluster
    2. Same postcode but different beds form two clusters
    3. Same lat/lng but different beds form two clusters
       (geo agreement does not override the bedroom predicate)
    4. Idempotent: a second invocation does not create new cluster rows
    5. choose_primary picks the listing with more populated enrichment fields
    6. Transitive closure: A clusters with B, B clusters with C
       implies A and C end up in the same cluster

  Fixture (labelled set)
    7. Labelled-pair contract holds: 3 duplicate pairs share a cluster,
       5 distinct pairs do not
    8. Precision >= 0.85 and recall >= 0.85 on the labelled fixture
"""

import asyncio
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"
LISTINGS_FIXTURE = FIXTURES / "dedup_labelled_listings.json"
PAIRS_FIXTURE = FIXTURES / "dedup_labelled_pairs.csv"


def _assert(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL [{label}]: {detail}")
    print(f"  PASS [{label}]")


# ---------------------------------------------------------------------------
# DB scaffolding shared by every test
# ---------------------------------------------------------------------------

async def _make_engine_and_seed(rows):
    """
    Build an in-memory async SQLite, create the listing table from the
    SQLModel metadata, apply the dedup migration shape, and insert rows.
    Returns (engine, async_sessionmaker).
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlmodel import SQLModel
    import homehunt.core.db  # registers Listing
    from homehunt.dedup import ensure_cluster_schema

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.create_all(c))
        await conn.run_sync(lambda c: ensure_cluster_schema(c))

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        from homehunt.core.db import Listing
        for row in rows:
            session.add(Listing(**row))
        await session.commit()

    return engine, async_session


def _row(uid, postcode, bedrooms, price_pcm, **extra):
    """Minimal Listing kwargs sufficient for clustering tests."""
    base = {
        "uid": uid,
        "portal": uid.split(":")[0],
        "property_id": uid.split(":")[1],
        "url": f"https://example.com/{uid}",
        "postcode": postcode,
        "bedrooms": bedrooms,
        "price_numeric": price_pcm * 100,
        "extraction_method": "direct_http",
        "first_seen": datetime.utcnow(),
        "last_scraped": datetime.utcnow(),
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Synthetic tests
# ---------------------------------------------------------------------------

async def test_same_postcode_beds_price_clusters():
    print("Test 1: same postcode + beds + price within 2% form one cluster")
    rows = [
        _row("rightmove:1", "NW3 1QH", 1, 2300),
        _row("zoopla:2", "NW3 1QH", 1, 2330),  # 1.30% apart, inside 2%
    ]
    engine, async_session = await _make_engine_and_seed(rows)
    try:
        from homehunt.dedup import cluster_listings, get_cluster_assignments

        async with async_session() as session:
            cluster_count = await cluster_listings(session)

        async with async_session() as session:
            assignments = await get_cluster_assignments(session)

        _assert("two_listings_one_cluster", cluster_count == 1, f"got {cluster_count}")
        _assert(
            "shared_cluster_id",
            assignments["rightmove:1"] == assignments["zoopla:2"],
            f"{assignments}",
        )
    finally:
        await engine.dispose()


async def test_same_postcode_different_beds_split():
    print("Test 2: same postcode but different beds form two clusters")
    rows = [
        _row("rightmove:10", "N19 3JQ", 1, 1900),
        _row("zoopla:11", "N19 3JQ", 2, 1900),
    ]
    engine, async_session = await _make_engine_and_seed(rows)
    try:
        from homehunt.dedup import cluster_listings, get_cluster_assignments

        async with async_session() as session:
            cluster_count = await cluster_listings(session)
        async with async_session() as session:
            assignments = await get_cluster_assignments(session)

        _assert("two_clusters", cluster_count == 2, f"got {cluster_count}")
        _assert(
            "different_cluster_ids",
            assignments["rightmove:10"] != assignments["zoopla:11"],
            f"{assignments}",
        )
    finally:
        await engine.dispose()


async def test_same_geo_different_beds_split():
    print("Test 3: same lat/lng but different beds still split")
    rows = [
        _row("rightmove:20", "E14 9SH", 1, 2200, latitude=51.5044, longitude=-0.0193),
        _row("zoopla:21", "E14 9SH", 2, 2200, latitude=51.5044, longitude=-0.0193),
    ]
    engine, async_session = await _make_engine_and_seed(rows)
    try:
        from homehunt.dedup import cluster_listings, get_cluster_assignments

        async with async_session() as session:
            await cluster_listings(session)
        async with async_session() as session:
            assignments = await get_cluster_assignments(session)

        _assert(
            "geo_does_not_override_beds",
            assignments["rightmove:20"] != assignments["zoopla:21"],
            f"{assignments}",
        )
    finally:
        await engine.dispose()


async def test_idempotent_second_run():
    print("Test 4: a second invocation does not create new cluster rows")
    rows = [
        _row("rightmove:30", "NW3 1QH", 1, 2400),
        _row("zoopla:31", "NW3 1QH", 1, 2410),
    ]
    engine, async_session = await _make_engine_and_seed(rows)
    try:
        from homehunt.dedup import cluster_listings, get_cluster_assignments

        async with async_session() as session:
            count_first = await cluster_listings(session)
        async with async_session() as session:
            assignments_first = await get_cluster_assignments(session)
        async with async_session() as session:
            count_second = await cluster_listings(session)
        async with async_session() as session:
            assignments_second = await get_cluster_assignments(session)

        _assert("count_stable", count_first == count_second, f"{count_first} vs {count_second}")
        _assert("assignments_stable", assignments_first == assignments_second,
                f"{assignments_first} vs {assignments_second}")

        # Cluster rows must not multiply: count via raw query.
        from sqlalchemy import text
        async with async_session() as session:
            result = await session.execute(text("SELECT COUNT(*) FROM listing_cluster"))
            row_count = result.scalar()
        _assert("cluster_table_row_count_matches", row_count == count_first,
                f"table has {row_count}, cluster_count is {count_first}")
    finally:
        await engine.dispose()


async def test_choose_primary_prefers_more_enrichment():
    print("Test 5: choose_primary picks the listing with more populated enrichment")
    from homehunt.dedup import choose_primary

    class _Stub:
        def __init__(self, uid, portal, **kw):
            self.uid = uid
            self.portal = portal
            self.size_sqft = kw.get("size_sqft")
            self.epc_rating = kw.get("epc_rating")
            self.floorplan_data = kw.get("floorplan_data")
            self.tube_distance = kw.get("tube_distance")
            self.commute_canary_wharf = kw.get("commute_canary_wharf")
            self.first_seen = kw.get("first_seen", datetime(2026, 5, 1))

    # zoopla member has 4 enrichment fields populated; rightmove has 1.
    rich = _Stub(
        "zoopla:40", "zoopla",
        size_sqft=520, epc_rating="C",
        tube_distance=4, commute_canary_wharf=22,
    )
    sparse = _Stub("rightmove:41", "rightmove", epc_rating="D")

    chosen = choose_primary([sparse, rich])
    _assert("rich_wins", chosen == "zoopla:40", f"chose {chosen}")

    # Tie on enrichment count, pick more recent first_seen.
    a = _Stub("zoopla:42", "zoopla", epc_rating="C", first_seen=datetime(2026, 5, 1))
    b = _Stub("zoopla:43", "zoopla", epc_rating="D", first_seen=datetime(2026, 5, 5))
    chosen = choose_primary([a, b])
    _assert("recent_first_seen_tiebreak", chosen == "zoopla:43", f"chose {chosen}")

    # Tie on enrichment count and on first_seen, prefer rightmove.
    a = _Stub("zoopla:44", "zoopla", epc_rating="C", first_seen=datetime(2026, 5, 5))
    b = _Stub("rightmove:45", "rightmove", epc_rating="D", first_seen=datetime(2026, 5, 5))
    chosen = choose_primary([a, b])
    _assert("rightmove_tiebreak", chosen == "rightmove:45", f"chose {chosen}")


async def test_transitive_closure():
    print("Test 6: A clusters with B, B clusters with C, A and C share a cluster")
    # Three listings where A-B match (within 2%), B-C match (within 2%),
    # but A-C alone differ by more than 2%. Transitive closure must merge.
    rows = [
        _row("rightmove:50", "NW3 1QH", 1, 2300),
        _row("zoopla:51", "NW3 1QH", 1, 2340),  # 1.74% from A
        _row("zoopla:52", "NW3 1QH", 1, 2380),  # 1.71% from B, 3.48% from A
    ]
    engine, async_session = await _make_engine_and_seed(rows)
    try:
        from homehunt.dedup import cluster_listings, get_cluster_assignments

        async with async_session() as session:
            cluster_count = await cluster_listings(session)
        async with async_session() as session:
            assignments = await get_cluster_assignments(session)

        _assert("one_cluster", cluster_count == 1, f"got {cluster_count}")
        ids = {assignments["rightmove:50"], assignments["zoopla:51"], assignments["zoopla:52"]}
        _assert("all_three_share_cluster", len(ids) == 1, f"ids={ids}")
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Fixture-based tests
# ---------------------------------------------------------------------------

def _load_fixture():
    listings = json.loads(LISTINGS_FIXTURE.read_text())
    pairs = []
    with PAIRS_FIXTURE.open() as f:
        reader = csv.DictReader(f)
        for r in reader:
            pairs.append((r["uid_a"], r["uid_b"], r["label"].strip()))
    return listings, pairs


def _to_db_row(d):
    return _row(
        d["uid"],
        d["postcode"],
        d["bedrooms"],
        d["price_pcm"],
        latitude=d.get("latitude"),
        longitude=d.get("longitude"),
        size_sqft=d.get("size_sqft"),
        epc_rating=d.get("epc_rating"),
        address=d.get("address"),
    )


async def test_labelled_pair_contract():
    print("Test 7: labelled-pair contract holds on the 8-listing fixture")
    listings, pairs = _load_fixture()
    rows = [_to_db_row(d) for d in listings]
    engine, async_session = await _make_engine_and_seed(rows)
    try:
        from homehunt.dedup import cluster_listings, get_cluster_assignments

        async with async_session() as session:
            cluster_count = await cluster_listings(session)
        async with async_session() as session:
            assignments = await get_cluster_assignments(session)

        for uid_a, uid_b, label in pairs:
            same = assignments[uid_a] == assignments[uid_b]
            if label == "duplicate":
                _assert(
                    f"duplicate_pair_clusters[{uid_a}|{uid_b}]",
                    same,
                    f"a={assignments[uid_a]} b={assignments[uid_b]}",
                )
            elif label == "distinct":
                _assert(
                    f"distinct_pair_split[{uid_a}|{uid_b}]",
                    not same,
                    f"a={assignments[uid_a]} b={assignments[uid_b]}",
                )
            else:
                raise AssertionError(f"unknown label {label!r}")

        # Sanity: the duplicate trio shares one cluster id.
        trio = {
            assignments["rightmove:88044570"],
            assignments["zoopla:73100696"],
            assignments["zoopla:73109367"],
        }
        _assert("duplicate_trio_one_cluster", len(trio) == 1, f"trio ids={trio}")
        print(f"    cluster_count={cluster_count}, assignments={assignments}")
    finally:
        await engine.dispose()


async def test_precision_recall_on_labelled_set():
    print("Test 8: precision >= 0.85 and recall >= 0.85 on the labelled fixture")
    listings, pairs = _load_fixture()
    rows = [_to_db_row(d) for d in listings]
    engine, async_session = await _make_engine_and_seed(rows)
    try:
        from homehunt.dedup import cluster_listings, get_cluster_assignments

        async with async_session() as session:
            await cluster_listings(session)
        async with async_session() as session:
            assignments = await get_cluster_assignments(session)

        # Evaluate strictly on the labelled pairs.
        true_pos = 0  # algo merged AND labelled duplicate
        false_pos = 0  # algo merged AND labelled distinct
        false_neg = 0  # algo did not merge AND labelled duplicate
        true_neg = 0  # algo did not merge AND labelled distinct
        for uid_a, uid_b, label in pairs:
            algo_merged = assignments[uid_a] == assignments[uid_b]
            if label == "duplicate" and algo_merged:
                true_pos += 1
            elif label == "duplicate" and not algo_merged:
                false_neg += 1
            elif label == "distinct" and algo_merged:
                false_pos += 1
            elif label == "distinct" and not algo_merged:
                true_neg += 1

        precision = true_pos / (true_pos + false_pos) if (true_pos + false_pos) else 0.0
        recall = true_pos / (true_pos + false_neg) if (true_pos + false_neg) else 0.0
        print(
            f"    tp={true_pos} fp={false_pos} fn={false_neg} tn={true_neg} "
            f"precision={precision:.3f} recall={recall:.3f}"
        )
        _assert("precision_above_threshold", precision >= 0.85, f"precision={precision:.3f}")
        _assert("recall_above_threshold", recall >= 0.85, f"recall={recall:.3f}")
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def _run():
    tests = [
        ("same_postcode_beds_price_clusters", test_same_postcode_beds_price_clusters),
        ("same_postcode_different_beds_split", test_same_postcode_different_beds_split),
        ("same_geo_different_beds_split", test_same_geo_different_beds_split),
        ("idempotent_second_run", test_idempotent_second_run),
        ("choose_primary_prefers_more_enrichment", test_choose_primary_prefers_more_enrichment),
        ("transitive_closure", test_transitive_closure),
        ("labelled_pair_contract", test_labelled_pair_contract),
        ("precision_recall_on_labelled_set", test_precision_recall_on_labelled_set),
    ]

    failures = []
    for name, fn in tests:
        print(f"\n=== {name} ===")
        try:
            await fn()
        except AssertionError as exc:
            print(f"  {exc}")
            failures.append(name)
        except Exception as exc:
            import traceback
            print(f"  ERROR in {name}: {exc}")
            traceback.print_exc()
            failures.append(name)

    print(f"\n{'=' * 60}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print(f"ALL {len(tests)} test groups passed.")


if __name__ == "__main__":
    asyncio.run(_run())
