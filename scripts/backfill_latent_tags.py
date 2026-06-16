"""
One-shot latent visual tag backfill.

Walks recent active listings with photos where tags_extracted_at is still NULL,
calls homehunt.latent_tags.tag_listing(), and writes the 13 latent tag columns
plus tag_raw_captions and tags_extracted_at.

Run:
  python scripts/backfill_latent_tags.py
  python scripts/backfill_latent_tags.py --since-days 14 --limit 25
  python scripts/backfill_latent_tags.py --dry-run --db data/homehunt.db
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

if not os.environ.get("HOMEHUNT_DB"):
    os.environ["HOMEHUNT_DB"] = str(_PROJECT_ROOT / "data" / "homehunt.db")

from sqlmodel import Session, select

from homehunt.core.db import Database, Listing
from homehunt.latent_tags import tag_listing


TAG_KEYS = [
    "window_style",
    "ceiling_height",
    "exposed_brick",
    "exposed_beams_or_ducts",
    "building_era",
    "kitchen_finish",
    "bathroom_finish",
    "flooring",
    "natural_light",
    "wall_palette",
    "open_plan",
    "view",
    "outdoor_access",
]

TAG_COLUMNS = [f"tag_{key}" for key in TAG_KEYS]
VISION_CALL_LOG = "POST http://127.0.0.1:11434/api/generate model=qwen3-vl:2b-instruct-q8_0"


def _load_image_urls(raw_images: str | None) -> list[str]:
    if not raw_images:
        return []
    try:
        parsed = json.loads(raw_images)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [url for url in parsed if isinstance(url, str)]


def _select_candidates(db: Database, *, since_days: int, limit: int | None) -> list[tuple[str, list[str]]]:
    cutoff = datetime.utcnow() - timedelta(days=since_days)
    statement = (
        select(Listing.uid, Listing.images)
        .where(text("tags_extracted_at IS NULL"))
        .where(Listing.is_active.is_(True))
        .where(Listing.first_seen > cutoff)
        .where(Listing.images.is_not(None))
        .where(Listing.images != "[]")
        .order_by(Listing.first_seen.desc())
    )
    if limit is not None:
        statement = statement.limit(limit)

    candidates: list[tuple[str, list[str]]] = []
    with Session(db.engine) as session:
        for row in session.exec(statement).all():
            urls = _load_image_urls(row.images)
            if urls:
                candidates.append((row.uid, urls))
    return candidates


def _persist_tags(db: Database, uid: str, result: dict[str, Any]) -> None:
    values: dict[str, Any] = {column: result.get(column.removeprefix("tag_")) for column in TAG_COLUMNS}
    values["tag_raw_captions"] = json.dumps(result.get("raw_captions", []), separators=(",", ":"))
    values["uid"] = uid

    assignments = ", ".join(f"{column} = :{column}" for column in TAG_COLUMNS)
    statement = text(
        f"""
        UPDATE listing
        SET {assignments},
            tag_raw_captions = :tag_raw_captions,
            tags_extracted_at = CURRENT_TIMESTAMP
        WHERE uid = :uid
        """
    )

    with Session(db.engine) as session:
        session.exec(statement, params=values)
        session.commit()


def _is_preflight_failure(exc: BaseException) -> bool:
    message = str(exc)
    return "Ollama is not reachable" in message or "Ollama model(s) not installed" in message


def run(*, since_days: int, limit: int | None, dry_run: bool, db_path: str | None, max_images: int) -> int:
    if db_path:
        os.environ["HOMEHUNT_DB"] = db_path

    db = Database()
    db.create_tables()
    candidates = _select_candidates(db, since_days=since_days, limit=limit)

    print(
        f"Latent tag backfill: {len(candidates)} listings "
        f"since_days={since_days} limit={limit} dry_run={dry_run} max_images={max_images}"
    )
    for uid, _urls in candidates:
        print(uid)

    if dry_run or not candidates:
        return 0

    logs_dir = _PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"backfill_latent_tags_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.log"

    processed = 0
    failed = 0
    started = time.perf_counter()
    with log_path.open("a", encoding="utf-8") as audit:
        for uid, urls in candidates:
            image_count = min(max_images, len(urls))
            listing_started = time.perf_counter()
            for _ in range(image_count):
                audit.write(f"{datetime.utcnow().isoformat()} uid={uid} {VISION_CALL_LOG}\n")
            audit.flush()

            try:
                result = tag_listing(uid, urls, max_images=max_images)
            except Exception as exc:
                if _is_preflight_failure(exc):
                    raise
                failed += 1
                latency = time.perf_counter() - listing_started
                audit.write(
                    f"{datetime.utcnow().isoformat()} uid={uid} failed={type(exc).__name__} "
                    f"total_latency={latency:.3f} image_count={image_count} error={exc}\n"
                )
                audit.flush()
                print(f"FAILED uid={uid}: {exc}", file=sys.stderr)
                continue

            _persist_tags(db, uid, result)
            processed += 1
            latency = time.perf_counter() - listing_started
            audit.write(
                f"{datetime.utcnow().isoformat()} uid={uid} total_latency={latency:.3f} "
                f"image_count={image_count}\n"
            )
            audit.flush()
            print(f"UPDATED uid={uid} total_latency={latency:.3f} image_count={image_count}")

    elapsed = time.perf_counter() - started
    print(f"Done processed={processed} failed={failed} elapsed={elapsed:.1f}s log={log_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since-days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", type=str, default=None)
    parser.add_argument("--max-images", type=int, default=3)
    args = parser.parse_args()

    return run(
        since_days=args.since_days,
        limit=args.limit,
        dry_run=args.dry_run,
        db_path=args.db,
        max_images=args.max_images,
    )


if __name__ == "__main__":
    sys.exit(main())
