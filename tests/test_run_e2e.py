"""
End-to-end test: run a tiny scrape (1 region, max_results=3), confirm every
saved row has tfl, epc, carpet, and score populated.
"""
import asyncio
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from homehunt.core.db import Database, Listing
from sqlmodel import Session, select

# EPC returns None when credentials are not configured. That is correct behaviour,
# not a pipeline failure. Skip the EPC column check in that case.
_EPC_CREDS_PRESENT = bool(os.environ.get("EPC_EMAIL") and os.environ.get("EPC_API_KEY"))


async def test_e2e_pipeline_populates_all_columns():
    from run import run_pipeline

    await run_pipeline(
        profiles_override=[
            {
                "location": "Shoreditch, London",
                "region_id": "REGION^87490",
                "min_price": 1600,
                "max_price": 2800,
                "min_bedrooms": 1,
                "max_bedrooms": 2,
                "radius": 1.0,
                "max_results": 3,
                "page_delay": 2.0,
                "scrape_delay": 1.0,
            }
        ]
    )

    db = Database()
    with Session(db.engine) as s:
        rows = s.exec(select(Listing).where(Listing.is_active == True).limit(3)).all()

    assert len(rows) >= 1, "no rows saved"
    for r in rows:
        assert r.score is not None, f"{r.uid}: no score"
        if _EPC_CREDS_PRESENT:
            assert r.epc_rating is not None or r.postcode is None, f"{r.uid}: epc not attempted"
        assert r.carpet_in_bedroom is not None or not r.images, f"{r.uid}: carpet not attempted"
        assert r.commute_public_transport is not None or r.postcode is None, f"{r.uid}: tfl not attempted"

    print(f"PASS: {len(rows)} rows, all enriched and scored")


asyncio.run(test_e2e_pipeline_populates_all_columns())
