"""
OpenRent Integration - Task 7: _run_openrent_pipeline writes new rows with
required fields populated into a temp DB on a small candidate set.

Set HOMEHUNT_DB before importing run / homehunt.core.db so SQLModel registers
the Listing table against the temp file rather than the production DB.
"""

import os
import sys
import tempfile
from pathlib import Path


def _make_temp_db_env() -> str:
    fd, path = tempfile.mkstemp(prefix="openrent-pipeline-", suffix=".db")
    os.close(fd)
    os.remove(path)
    os.environ["HOMEHUNT_DB"] = path
    return path


_DB_PATH = _make_temp_db_env()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import asyncio
import run as _run
from homehunt.core.db import Database, Listing
from sqlmodel import select


async def main() -> int:
    db = Database()
    db.create_tables()

    profile = {
        "name": "openrent-smoke",
        "search_url": (
            "https://www.openrent.co.uk/properties-to-rent/london?term=London"
            "&prices_min=1500&prices_max=2800&bedrooms_min=1&bedrooms_max=2"
        ),
        "centroids": [(51.5326, -0.1058)],
        "radius_km": 1.0,
        "max_listings": 3,
    }
    total: dict = {
        "discovered": 0, "touched": 0, "saved": 0, "failed": 0,
        "filtered_stage1": 0, "filtered_stage2": 0,
    }
    aggregate_drops_s1: dict[str, int] = {}
    aggregate_drops_s2: dict[str, int] = {}

    await _run._run_openrent_pipeline(
        [profile], total, None, aggregate_drops_s1, aggregate_drops_s2, horizon_hours=0,
    )

    from homehunt.core.models import Portal
    from sqlmodel import Session
    with Session(db.engine) as s:
        rows = s.exec(select(Listing).where(Listing.portal == Portal.OPENRENT)).all()
    assert 1 <= len(rows) <= 3, f"expected 1-3 OpenRent rows, got {len(rows)}"
    sample = rows[0]
    for f in ("uid", "portal", "property_id", "url", "title", "price_numeric", "bedrooms", "area", "latitude", "longitude"):
        v = getattr(sample, f)
        assert v not in (None, ""), f"REQUIRED field {f!r} empty on row uid={sample.uid}"

    print(f"PASS - _run_openrent_pipeline saved {len(rows)} rows; sample uid={sample.uid} price={sample.price_numeric}p beds={sample.bedrooms}")
    Path(_DB_PATH).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
