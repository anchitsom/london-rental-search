"""
Backfill: re-extract floorplan data for Zoopla listings that have null
floorplan_data.

Cause: until 2026-05-09 commit 1447e65, the floorplan vision module's host
allowlist accepted only Rightmove media CDNs and rejected lc.zoocdn.com URLs
with `non_rightmove_url_skipped`. The Zoopla scraper extracted the URLs
correctly but the vision call short-circuited. Wave 2.7's 247 Zoopla rows
all carry floorplan_data=NULL as a result.

This script does NOT re-scrape from scratch. It fetches each Zoopla detail
page once to recover the floorplan URL (because property_metadata is never
written to disk, so the URL doesn't persist between runs), then calls
extract_floorplan_data with the fixed allowlist, then writes
floorplan_data back.

Usage:
  HOMEHUNT_DB=data/homehunt.db .venv/bin/python scripts/backfill_zoopla_floorplan.py
  HOMEHUNT_DB=data/homehunt.db .venv/bin/python scripts/backfill_zoopla_floorplan.py --limit 5
  HOMEHUNT_DB=data/homehunt.db .venv/bin/python scripts/backfill_zoopla_floorplan.py --dry-run
"""
import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

if not os.environ.get("HOMEHUNT_DB"):
    os.environ["HOMEHUNT_DB"] = str(_PROJECT_ROOT / "data" / "homehunt.db")

from sqlmodel import Session, select

from homehunt.core.db import Database, Listing
from homehunt.floorplan_vision import extract_floorplan_data
from homehunt.scrapers.zoopla_http import ZooplaHTTPScraper
from homehunt.scrapers.zoopla_url_builder import build_zoopla_search_url


_FLOORPLAN_SEM = asyncio.Semaphore(int(os.environ.get("FLOORPLAN_MAX_CONCURRENT", "2")))


async def _process_one(scraper, uid: str, url: str) -> tuple[str, dict]:
    try:
        result = await scraper.scrape_property(url)
        if not result.success or not result.data:
            return uid, {"error": f"scrape_failed: {result.error or 'no_data'}"}
        floorplan_urls = result.data.get("floorplan_urls") or []
        if not floorplan_urls:
            return uid, {"error": "no_floorplan_url"}
        async with _FLOORPLAN_SEM:
            floorplan = await extract_floorplan_data(floorplan_urls[0])
        return uid, floorplan or {"error": "vision_returned_empty"}
    except Exception as exc:
        return uid, {"error": f"{type(exc).__name__}: {exc}"}


async def run(limit: int | None, dry_run: bool) -> int:
    db = Database()
    db.create_tables()

    with Session(db.engine) as s:
        rows = s.exec(
            select(Listing.uid, Listing.url)
            .where(Listing.portal == "ZOOPLA")
            .where(Listing.is_active.is_(True))
            .where(Listing.floorplan_data.is_(None))
        ).all()

    candidates = [(r.uid, r.url) for r in rows if r.url]
    if limit is not None:
        candidates = candidates[:limit]

    total = len(candidates)
    print(f"Zoopla floorplan backfill: {total} candidates. dry_run={dry_run}")

    if total == 0 or dry_run:
        return 0

    # Seed the Zoopla session via one search-page fetch. Any active Zoopla
    # search URL works; we use Highbury because it reliably returns >= 1
    # listing.
    seed_url = build_zoopla_search_url(
        location="Highbury, North London",
        min_price=1600,
        max_price=2800,
        min_bedrooms=1,
        max_bedrooms=2,
        page=1,
    )

    outcomes = Counter()
    started = time.time()

    async with ZooplaHTTPScraper() as scraper:
        print(f"Seeding session via {seed_url}")
        await scraper.scrape_search_page(seed_url)

        tasks = [
            asyncio.create_task(_process_one(scraper, uid, url))
            for uid, url in candidates
        ]

        completed = 0
        for fut in asyncio.as_completed(tasks):
            uid, result = await fut
            err = result.get("error")
            if err:
                outcomes[f"err:{err.split(':')[0]}"] += 1
            else:
                outcomes["written"] += 1
                if not dry_run:
                    with Session(db.engine) as s:
                        listing = s.get(Listing, uid)
                        if listing is not None:
                            listing.floorplan_data = json.dumps(result)
                            s.add(listing)
                            s.commit()

            completed += 1
            if completed % 10 == 0 or completed == total:
                elapsed = time.time() - started
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (total - completed) / rate if rate > 0 else 0
                print(f"  [{completed}/{total}] elapsed={elapsed:.0f}s rate={rate:.2f}/s eta={eta:.0f}s")

    elapsed = time.time() - started
    print(f"\nDone in {elapsed:.0f}s")
    print("\nOutcomes:")
    for k, v in sorted(outcomes.items(), key=lambda x: -x[1]):
        print(f"  {k:30s} {v}")

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    rc = asyncio.run(run(limit=args.limit, dry_run=args.dry_run))
    return rc


if __name__ == "__main__":
    sys.exit(main())
