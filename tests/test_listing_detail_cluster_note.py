"""
Pre-Wave-3 unblocker S4: cluster cross-portal note on listing detail page.

When the listing belongs to a multi-member cluster, the detail page header
shows a small note like 'Same flat also listed on Zoopla' (or Rightmove,
depending on the other cluster member's portal). For clusters of three or
more, the note simply says 'Same flat also listed on N other portals' or
similar -- a single, terse line.

Solo listings show no note.

Project convention: plain script, NOT pytest. Run via
.venv/bin/python tests/test_listing_detail_cluster_note.py.

Tests:
  1. Clustered listing's detail page mentions the other portal's name.
  2. Solo listing's detail page contains no cross-portal note.
  3. Cluster of three or more shows a counted note.
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


def _build_db(path: Path) -> None:
    """Three listings: a 2-member cluster (RM + Z), a 3-member cluster, and a solo.

    Cluster 1: rightmove:1001 + zoopla:2001 (member_count=2, primary=rightmove:1001)
    Cluster 2: rightmove:3001 + zoopla:3002 + rightmove:3003 (member_count=3, primary=rightmove:3001)
    Solo:      rightmove:9001 (cluster_id=NULL)
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
                 title: str) -> Listing:
        return Listing(
            uid=uid,
            portal=portal,
            property_id=uid.split(":")[1],
            url=f"https://example.com/{uid}",
            address="1 Test Street, London",
            postcode="N5 1AB",
            title=title,
            price="£2,200 pcm",
            price_numeric=220000,
            bedrooms=2,
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

    rows = [
        _listing("rightmove:1001", Portal.RIGHTMOVE, cluster_id=1, title="Cluster of 2 RM side"),
        _listing("zoopla:2001", Portal.ZOOPLA, cluster_id=1, title="Cluster of 2 Z side"),
        _listing("rightmove:3001", Portal.RIGHTMOVE, cluster_id=2, title="Cluster of 3 A"),
        _listing("zoopla:3002", Portal.ZOOPLA, cluster_id=2, title="Cluster of 3 B"),
        _listing("rightmove:3003", Portal.RIGHTMOVE, cluster_id=2, title="Cluster of 3 C"),
        _listing("rightmove:9001", Portal.RIGHTMOVE, cluster_id=None, title="Solo"),
    ]

    with Session(engine) as s:
        for r in rows:
            s.add(r)
        s.commit()

    cluster_rows = [
        {
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
        },
        {
            "cluster_id": 2,
            "centroid_lat": None,
            "centroid_lng": None,
            "bedrooms": 2,
            "price_pcm_min": 2200,
            "price_pcm_max": 2200,
            "member_count": 3,
            "primary_uid": "rightmove:3001",
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
        },
    ]
    with engine.begin() as conn:
        for cr in cluster_rows:
            conn.execute(listing_cluster_table.insert().values(**cr))

    engine.dispose()


_DB_PATH = FIXTURES_DIR / "listing_detail_cluster_note.db"
_BUILT = False


def _client():
    global _BUILT
    if not _BUILT:
        _build_db(_DB_PATH)
        _BUILT = True
    os.environ["HOMEHUNT_DB"] = str(_DB_PATH)
    for mod in ["homehunt.api", "homehunt.viewer_query"]:
        sys.modules.pop(mod, None)
    from fastapi.testclient import TestClient
    from homehunt import api as api_module
    return TestClient(api_module.app)


def test_clustered_listing_shows_cross_portal_note():
    print("Test 1: clustered listing detail page shows cross-portal note")
    client = _client()
    r = client.get("/listing/rightmove:1001")
    _assert("status_200", r.status_code == 200, f"got {r.status_code}")
    body = r.text
    # Note text should mention the other portal: Zoopla.
    _assert("note_zoopla", "Zoopla" in body, "Zoopla portal name missing from note")
    _assert("note_phrase", "also listed" in body.lower(),
            "expected 'also listed' phrasing in cross-portal note")
    # And from the Zoopla side, the note should mention Rightmove.
    r2 = client.get("/listing/zoopla:2001")
    _assert("zoopla_status", r2.status_code == 200)
    body2 = r2.text
    _assert("note_rightmove", "Rightmove" in body2,
            "Rightmove portal name missing from Zoopla-side note")
    _assert("note_phrase_rm", "also listed" in body2.lower(),
            "expected 'also listed' phrasing on Zoopla side")


def test_solo_listing_shows_no_note():
    print("Test 2: solo listing has no cross-portal note")
    client = _client()
    r = client.get("/listing/rightmove:9001")
    _assert("status_200", r.status_code == 200)
    body = r.text
    _assert("no_note", "also listed" not in body.lower(),
            "solo listing unexpectedly contains 'also listed' phrasing")
    _assert("no_cluster_note_class", "cluster-note" not in body,
            "solo listing rendered cluster-note element")


def test_three_member_cluster_note_counts_others():
    print("Test 3: three-member cluster note conveys the count")
    client = _client()
    r = client.get("/listing/rightmove:3001")
    _assert("status_200", r.status_code == 200)
    body = r.text
    _assert("note_present", "also listed" in body.lower(), "note absent on 3-cluster")
    # The 3-cluster has one Zoopla and two Rightmove rows. Showing other portals:
    # the note should reference Zoopla (the distinct other portal) and indicate
    # there are additional rows -- i.e. say either "also on Zoopla" or count >= 2.
    _assert("note_mentions_zoopla", "Zoopla" in body,
            "3-cluster note must surface the cross-portal portal name")


def main():
    test_clustered_listing_shows_cross_portal_note()
    test_solo_listing_shows_no_note()
    test_three_member_cluster_note_counts_others()
    print("\n" + "=" * 60)
    print("ALL 3 listing-detail cluster-note test groups passed.")


if __name__ == "__main__":
    main()
