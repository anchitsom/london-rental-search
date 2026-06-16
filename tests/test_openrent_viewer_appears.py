"""
OpenRent Viewer Integration - Task 1: /viewer surfaces OpenRent rows.

Hermetic test: build a fixture DB with 3 Rightmove, 3 Zoopla, and 3 OpenRent
rows. Hit GET /viewer with high per-page and sort=last_scraped_desc so the
score-null OpenRent rows can't be hidden by score-based ordering. Assert the
rendered HTML contains the chip 'OPENRENT' at least three times.

Pattern: set HOMEHUNT_DB before any homehunt import (process-global SQLModel
metadata cannot be reset).
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURE_DB = PROJECT_ROOT / "tests" / "fixtures" / "openrent_viewer.db"
FIXTURE_DB.parent.mkdir(parents=True, exist_ok=True)
if FIXTURE_DB.exists():
    FIXTURE_DB.unlink()
os.environ["HOMEHUNT_DB"] = str(FIXTURE_DB)

from datetime import datetime, timezone
from sqlmodel import Session, SQLModel
from sqlalchemy import create_engine
from homehunt.core.db import Listing
from homehunt.core.models import ExtractionMethod, Portal


def build_fixture_db(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    # listing_lifecycle is created by alembic migration 1778470000_lifecycle_crm,
    # not by SQLModel.metadata. Create it inline so /listing/{uid} doesn't 500.
    import sqlite3
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS listing_lifecycle (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          uid VARCHAR NOT NULL,
          from_status VARCHAR,
          to_status VARCHAR NOT NULL,
          reason VARCHAR,
          notes VARCHAR,
          transitioned_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
          transitioned_by VARCHAR
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_listing_lifecycle_uid ON listing_lifecycle(uid)"
    )
    con.commit()
    con.close()
    now = datetime.now(timezone.utc)

    def mk(portal: Portal, pid: str, price_p: int, beds: int, area: str, score: int | None) -> Listing:
        return Listing(
            uid=f"{portal.value}:{pid}",
            portal=portal,
            property_id=pid,
            url=f"https://example.test/{portal.value}/{pid}",
            title=f"{beds} Bed Flat, {area}",
            address=f"{beds} Bed Flat, {area}",
            area=area,
            latitude=51.5326,
            longitude=-0.1058,
            postcode="N1 7TT" if portal != Portal.OPENRENT else None,
            price=f"£{price_p // 100:,} pcm",
            price_numeric=price_p,
            bedrooms=beds,
            bathrooms=1,
            furnished="Furnished",
            extraction_method=ExtractionMethod.DIRECT_HTTP,
            score=score,
            first_seen=now,
            last_scraped=now,
            is_active=True,
            status="active",
            current_status="new",
        )

    rows = [
        mk(Portal.RIGHTMOVE, "rm1", 220000, 1, "Islington", 78),
        mk(Portal.RIGHTMOVE, "rm2", 240000, 2, "Islington", 81),
        mk(Portal.RIGHTMOVE, "rm3", 260000, 2, "Camden", 73),
        mk(Portal.ZOOPLA, "zp1", 230000, 1, "Hackney", 75),
        mk(Portal.ZOOPLA, "zp2", 245000, 2, "Camden", 80),
        mk(Portal.ZOOPLA, "zp3", 255000, 2, "Islington", 77),
        mk(Portal.OPENRENT, "or1", 235000, 1, "WC2N", None),
        mk(Portal.OPENRENT, "or2", 250000, 2, "WC2N", None),
        mk(Portal.OPENRENT, "or3", 270000, 2, "WC2R", None),
    ]
    with Session(engine) as s:
        for r in rows:
            s.add(r)
        s.commit()


def main() -> int:
    build_fixture_db(FIXTURE_DB)
    from fastapi.testclient import TestClient
    from homehunt import api as api_module
    client = TestClient(api_module.app)
    resp = client.get("/viewer?per_page=50&sort=last_scraped_desc")
    assert resp.status_code == 200, f"status {resp.status_code}; body {resp.text[:500]}"
    html = resp.text
    openrent_chip_count = html.count("OPENRENT")
    assert openrent_chip_count >= 3, (
        f"/viewer returned {openrent_chip_count} OPENRENT chips, expected >=3. "
        f"OpenRent rows are not surfacing through the viewer query."
    )
    print(f"PASS - /viewer surfaces {openrent_chip_count} OPENRENT chips")
    return 0


if __name__ == "__main__":
    sys.exit(main())
