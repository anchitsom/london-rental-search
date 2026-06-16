"""
Pre-Wave-3 unblocker S4: cluster member-count chip on viewer cards.

When a listing belongs to a cluster (cluster_id non-null) with 2 or more
members, the card renders a small chip near the portal badge showing the
extra member count. Solo listings (cluster_id null OR cluster of one) get
no chip.

Project convention: plain script, NOT pytest. Each test asserts and prints
PASS / FAIL; run via .venv/bin/python tests/test_viewer_cluster_chip.py.

Tests:
  1. Two-member cluster (Rightmove + Zoopla, same postcode + beds + price):
     both clustered cards on /viewer show '+1 more'. The solo card does not.
  2. Three-member cluster: each clustered card shows '+2 more'.
  3. Cluster of size 1 (orphaned cluster_id with member_count=1) is treated
     as solo and shows no chip.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES_DIR = ROOT / "tests" / "fixtures"


def _assert(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL [{label}]: {detail}")
    print(f"  PASS [{label}]")


def _build_db(path: Path, scenario: str) -> None:
    """Build a fixture DB for the given scenario.

    scenario:
      "two_member"   -- two listings in cluster 1, one solo (cluster_id null)
      "three_member" -- three listings in cluster 1, all on different portals
      "solo_orphan"  -- one listing with cluster_id=1 but member_count=1
    """
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)

    from sqlalchemy import create_engine
    from sqlmodel import Session, SQLModel
    import homehunt.core.db  # registers Listing
    from homehunt.core.db import Listing
    from homehunt.core.models import ExtractionMethod, Portal, PropertyType
    from homehunt.dedup import ensure_cluster_schema, listing_cluster_table

    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        ensure_cluster_schema(conn)

    now = datetime.utcnow()

    def _listing(uid: str, portal: Portal, *, cluster_id: int | None,
                 title: str, price_pence: int = 220000, beds: int = 2,
                 postcode: str = "N5 1AB") -> Listing:
        return Listing(
            uid=uid,
            portal=portal,
            property_id=uid.split(":")[1],
            url=f"https://example.com/{uid}",
            address="1 Test Street, London",
            postcode=postcode,
            title=title,
            price=f"£{price_pence // 100:,} pcm",
            price_numeric=price_pence,
            bedrooms=beds,
            property_type=PropertyType.FLAT,
            extraction_method=ExtractionMethod.DIRECT_HTTP,
            region="Highbury, North London",
            score=70,
            cluster_id=cluster_id,
            first_seen=now,
            last_scraped=now,
            scrape_count=1,
            is_active=True,
            status="active",
        )

    rows: list[Listing] = []
    cluster_rows: list[dict] = []

    if scenario == "two_member":
        rows.append(_listing("rightmove:1001", Portal.RIGHTMOVE, cluster_id=1,
                             title="Two-bed flat (RM)"))
        rows.append(_listing("zoopla:2001", Portal.ZOOPLA, cluster_id=1,
                             title="Two-bed flat (Z)"))
        rows.append(_listing("rightmove:1002", Portal.RIGHTMOVE, cluster_id=None,
                             title="Solo other flat", postcode="N7 8NN"))
        cluster_rows.append({
            "cluster_id": 1,
            "centroid_lat": None,
            "centroid_lng": None,
            "bedrooms": 2,
            "price_pcm_min": 2200,
            "price_pcm_max": 2200,
            "member_count": 2,
            "primary_uid": "rightmove:1001",
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
        })
    elif scenario == "three_member":
        rows.append(_listing("rightmove:3001", Portal.RIGHTMOVE, cluster_id=1,
                             title="Three-way A"))
        rows.append(_listing("zoopla:3002", Portal.ZOOPLA, cluster_id=1,
                             title="Three-way B"))
        rows.append(_listing("rightmove:3003", Portal.RIGHTMOVE, cluster_id=1,
                             title="Three-way C"))
        cluster_rows.append({
            "cluster_id": 1,
            "centroid_lat": None,
            "centroid_lng": None,
            "bedrooms": 2,
            "price_pcm_min": 2200,
            "price_pcm_max": 2200,
            "member_count": 3,
            "primary_uid": "rightmove:3001",
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
        })
    elif scenario == "solo_orphan":
        # cluster_id is set but member_count is 1. The chip should not render.
        rows.append(_listing("rightmove:5001", Portal.RIGHTMOVE, cluster_id=1,
                             title="Lonely orphan"))
        cluster_rows.append({
            "cluster_id": 1,
            "centroid_lat": None,
            "centroid_lng": None,
            "bedrooms": 2,
            "price_pcm_min": 2200,
            "price_pcm_max": 2200,
            "member_count": 1,
            "primary_uid": "rightmove:5001",
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
        })
    else:
        raise ValueError(f"unknown scenario: {scenario}")

    with Session(engine) as s:
        for r in rows:
            s.add(r)
        s.commit()

    # Insert cluster rows directly via the Table object to bypass any model
    # autocreation surprises. The dedup module already exposes the table.
    with engine.begin() as conn:
        for cr in cluster_rows:
            conn.execute(listing_cluster_table.insert().values(**cr))

    engine.dispose()


def _client_for(scenario: str):
    """Configure the API to use a fresh fixture DB and return a TestClient."""
    db_path = FIXTURES_DIR / f"viewer_cluster_chip_{scenario}.db"
    _build_db(db_path, scenario)
    os.environ["HOMEHUNT_DB"] = str(db_path)

    # Force a clean import so the new HOMEHUNT_DB is picked up.
    for mod in [
        "homehunt.api",
        "homehunt.viewer_query",
    ]:
        sys.modules.pop(mod, None)
    from fastapi.testclient import TestClient
    from homehunt import api as api_module
    return TestClient(api_module.app)


def test_two_member_cluster_renders_chip():
    print("Test 1: two-member cluster renders '+1 more' on both clustered cards, none on solo")
    client = _client_for("two_member")
    r = client.get("/viewer")
    _assert("status_200", r.status_code == 200, f"got {r.status_code}")
    body = r.text

    # The chip text appears for the two clustered listings -- once per card.
    chip_count = body.count("+1 more")
    _assert(
        "chip_appears_twice",
        chip_count == 2,
        f"expected 2 occurrences of '+1 more', got {chip_count}",
    )

    # Solo card should not contain the chip text. We can scope to the solo
    # listing's data-uid by checking it does not appear in the same card.
    # Cheap heuristic: each card has a `<li class="card" data-uid=...>` open.
    # Split on that and verify the solo card slice has no chip.
    solo_marker = 'data-uid="rightmove:1002"'
    _assert("solo_present", solo_marker in body, "solo card not rendered")
    solo_idx = body.index(solo_marker)
    # The solo card runs from its data-uid up to the next data-uid OR end of body.
    next_card = body.find('data-uid="', solo_idx + len(solo_marker))
    solo_slice = body[solo_idx: next_card if next_card != -1 else len(body)]
    _assert(
        "no_chip_on_solo",
        "+1 more" not in solo_slice and "more)" not in solo_slice,
        "solo card incorrectly contains a cluster chip",
    )


def test_three_member_cluster_renders_chip_with_count():
    print("Test 2: three-member cluster renders '+2 more' on each clustered card")
    client = _client_for("three_member")
    r = client.get("/viewer")
    _assert("status_200", r.status_code == 200)
    body = r.text
    count = body.count("+2 more")
    _assert("three_chip", count == 3, f"expected 3 '+2 more' chips, got {count}")
    # And no '+1 more' should appear (we're a 3-cluster, not a 2-cluster).
    _assert("no_off_by_one", "+1 more" not in body, "stray '+1 more' present")


def test_orphan_single_member_cluster_renders_no_chip():
    print("Test 3: cluster of size 1 (orphan) renders no chip")
    client = _client_for("solo_orphan")
    r = client.get("/viewer")
    _assert("status_200", r.status_code == 200)
    body = r.text
    _assert("no_plus_more", "+1 more" not in body and "+0 more" not in body,
            "chip rendered on a single-member cluster")
    _assert("no_chip_class", "chip--cluster" not in body,
            "cluster chip class rendered for single-member cluster")


def main():
    test_two_member_cluster_renders_chip()
    test_three_member_cluster_renders_chip_with_count()
    test_orphan_single_member_cluster_renders_no_chip()
    print("\n" + "=" * 60)
    print("ALL 3 viewer cluster-chip test groups passed.")


if __name__ == "__main__":
    main()
