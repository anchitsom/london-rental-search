"""
Backfill: re-run carpet detection on all active listings using the live
VISION_MODEL (qwen3-vl:2b after S3 swap, 2026-05-09).

Walks the listing table where images IS NOT NULL AND is_active = true.
For each row, calls detect_carpet_two_signal against the persisted photo
URLs and writes the new carpet_in_bedroom / carpet_other_areas. After
the carpet pass, invokes the score_breakdown backfill to refresh the
nine-key breakdown with the new carpet scores.

No HTTP fetches to portals. Only Ollama vision calls and the local DB.

Run:
  HOMEHUNT_DB=data/homehunt.db .venv/bin/python scripts/backfill_carpet_detection.py
  HOMEHUNT_DB=data/homehunt.db .venv/bin/python scripts/backfill_carpet_detection.py --limit 5
  HOMEHUNT_DB=data/homehunt.db .venv/bin/python scripts/backfill_carpet_detection.py --dry-run
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

from homehunt.carpet import detect_carpet_two_signal
from homehunt.core.db import Database, Listing


_CARPET_SEM = asyncio.Semaphore(int(os.environ.get("CARPET_MAX_CONCURRENT", "2")))


async def _process_one(uid: str, photo_urls: list[str]) -> tuple[str, dict]:
    async with _CARPET_SEM:
        result = await detect_carpet_two_signal(photo_urls)
    return uid, result


def _classify_flip(old: bool | None, new: bool | None) -> str:
    if old == new:
        return "unchanged"
    if old is None and new is True:
        return "none_to_true"
    if old is None and new is False:
        return "none_to_false"
    if old is True and new is False:
        return "true_to_false"
    if old is False and new is True:
        return "false_to_true"
    if new is None:
        return f"to_none_from_{old}"
    return f"{old}_to_{new}"


async def run(limit: int | None, dry_run: bool) -> int:
    db = Database()
    db.create_tables()

    with Session(db.engine) as s:
        rows = s.exec(
            select(Listing.uid, Listing.images,
                   Listing.carpet_in_bedroom, Listing.carpet_other_areas)
            .where(Listing.is_active.is_(True))
            .where(Listing.images.is_not(None))
        ).all()

    candidates = []
    for r in rows:
        try:
            urls = json.loads(r.images) if r.images else []
        except (TypeError, ValueError):
            urls = []
        if not urls:
            continue
        candidates.append((r.uid, urls, r.carpet_in_bedroom, r.carpet_other_areas))

    if limit is not None:
        candidates = candidates[:limit]

    total = len(candidates)
    print(f"Carpet backfill: {total} active listings with photos. dry_run={dry_run}")
    print(f"Model: {os.environ.get('VISION_MODEL', 'default')}")
    print(f"Concurrency: {_CARPET_SEM._value}")

    if total == 0 or dry_run:
        return 0

    bedroom_flips = Counter()
    other_flips = Counter()
    started = time.time()
    completed = 0

    tasks = [
        asyncio.create_task(_process_one(uid, urls))
        for (uid, urls, _ob, _oo) in candidates
    ]
    old_lookup = {uid: (ob, oo) for (uid, _urls, ob, oo) in candidates}

    for fut in asyncio.as_completed(tasks):
        uid, result = await fut
        old_b, old_o = old_lookup[uid]
        new_b = result.get("carpet_in_bedroom")
        new_o = result.get("carpet_other_areas")

        bedroom_flips[_classify_flip(old_b, new_b)] += 1
        other_flips[_classify_flip(old_o, new_o)] += 1

        if not dry_run:
            with Session(db.engine) as s:
                listing = s.get(Listing, uid)
                if listing is not None:
                    listing.carpet_in_bedroom = new_b
                    listing.carpet_other_areas = new_o
                    s.add(listing)
                    s.commit()

        completed += 1
        if completed % 20 == 0 or completed == total:
            elapsed = time.time() - started
            rate = completed / elapsed if elapsed > 0 else 0
            eta = (total - completed) / rate if rate > 0 else 0
            print(f"  [{completed}/{total}] elapsed={elapsed:.0f}s rate={rate:.2f}/s eta={eta:.0f}s")

    elapsed = time.time() - started
    print(f"\nDone in {elapsed:.0f}s ({total / elapsed:.2f} listings/sec)")
    print(f"\nBedroom carpet flips:")
    for k, v in sorted(bedroom_flips.items(), key=lambda x: -x[1]):
        print(f"  {k:20s} {v}")
    print(f"\nOther-areas carpet flips:")
    for k, v in sorted(other_flips.items(), key=lambda x: -x[1]):
        print(f"  {k:20s} {v}")

    # Re-run score breakdown so the new carpet flags propagate into score_breakdown.
    if not dry_run:
        print("\nRefreshing score_breakdown with new carpet flags...")
        import subprocess
        result = subprocess.run(
            [sys.executable, str(_PROJECT_ROOT / "scripts" / "backfill_score_breakdown.py")],
            env={**os.environ},
            check=False,
        )
        if result.returncode != 0:
            print(f"  score_breakdown backfill exited rc={result.returncode}")
            return result.returncode

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
