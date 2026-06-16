"""
Backfill: recompute score and score_breakdown for active listings.

Pre-Wave-3 unblocker 2.3 (2026-05-08): the run.py call site has been fixed
to pass carpet_in_bedroom, carpet_other_areas, and region through the
scorer. Existing rows still carry the legacy seven-key breakdown
(`carpet`, `price`, `size`, `bedrooms`, `epc`, `transport`, `outside`)
because they were written before the call-site fix. This script re-runs
the scorer over each row's persisted columns and writes the new
eight-key shape back:
    price, size, bedrooms, epc, transport, outside,
    carpet_bedroom, carpet_other_areas, region_pref

Pure local computation. No vision calls, no HTTP fetches.

Run:
  HOMEHUNT_DB=data/homehunt.db .venv/bin/python scripts/backfill_score_breakdown.py
  HOMEHUNT_DB=data/homehunt.db .venv/bin/python scripts/backfill_score_breakdown.py --dry-run
  HOMEHUNT_DB=data/homehunt.db .venv/bin/python scripts/backfill_score_breakdown.py --missing-only

Default scope is every active row. Use --missing-only to skip rows that
already have a non-null score_breakdown (the original Wave 2 behaviour).

Idempotent. Safe to re-run.
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

if not os.environ.get("HOMEHUNT_DB"):
    os.environ["HOMEHUNT_DB"] = str(_PROJECT_ROOT / "data" / "homehunt.db")

from sqlmodel import Session, select

from homehunt.core.db import Database, Listing
from homehunt.scorer import score_listing


# The post-fix breakdown shape that every active row should carry after
# this script runs. Used for the histogram below.
EXPECTED_KEYS = (
    "price",
    "size",
    "bedrooms",
    "epc",
    "transport",
    "outside",
    "carpet_bedroom",
    "carpet_other_areas",
    "region_pref",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute breakdowns but do not write them back. Reports counts.",
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help=(
            "Only recompute rows whose score_breakdown is currently NULL. "
            "Default is every active row, which rewrites the legacy seven-key "
            "shape into the post-fix eight-key shape."
        ),
    )
    args = parser.parse_args()

    db = Database()
    # Defensive: ensure tables exist on a brand-new DB. No-op on production.
    db.create_tables()

    with Session(db.engine) as session:
        # "active" per the existing definition: is_active = True. The status
        # column also flips to "inactive" on disappearance; the existing Wave 2
        # backfill already filtered by is_active alone, so keep that exact
        # filter for consistency with the production data we want to update.
        if args.missing_only:
            stmt = select(Listing).where(
                Listing.is_active == True,  # noqa: E712 sqlalchemy idiom
                Listing.score_breakdown.is_(None),
            )
        else:
            stmt = select(Listing).where(Listing.is_active == True)  # noqa: E712
        rows = session.exec(stmt).all()

    print(f"Candidate rows: {len(rows)}")
    if not rows:
        print("Nothing to do.")
        return 0

    backfilled = 0
    skipped = 0
    histogram: Counter[str] = Counter()

    for r in rows:
        try:
            total, breakdown = score_listing(r)
        except Exception as exc:
            skipped += 1
            print(f"  SKIP {r.uid}: {exc}")
            continue

        if total is None or not breakdown:
            skipped += 1
            continue

        for key in breakdown:
            histogram[key] += 1

        if args.dry_run:
            backfilled += 1
            continue

        with Session(db.engine) as session:
            row = session.exec(select(Listing).where(Listing.uid == r.uid)).first()
            if row is None:
                skipped += 1
                continue
            row.score = total
            row.score_breakdown = json.dumps(breakdown)
            session.add(row)
            session.commit()
        backfilled += 1

    verb = "would update" if args.dry_run else "updated"
    print(f"\nDone. {verb} {backfilled} rows; skipped {skipped}.")

    print("\nBreakdown-key histogram (count of rows that emitted each key):")
    for key in EXPECTED_KEYS:
        count = histogram.get(key, 0)
        marker = "  " if count == backfilled else "!!"
        print(f"  {marker} {key:<22s} {count}")

    # Surface any keys that appeared but are not in the expected eight + region
    # set. The legacy 'carpet' key in particular should be ZERO after this run.
    extra = {k: v for k, v in histogram.items() if k not in EXPECTED_KEYS}
    if extra:
        print("\nUnexpected keys present (should be empty after the call-site fix):")
        for key, count in sorted(extra.items()):
            print(f"  !! {key:<22s} {count}")

    if backfilled and all(histogram.get(k, 0) == backfilled for k in EXPECTED_KEYS) and not extra:
        print("\nUniformity check: PASS (every row carries the nine expected keys).")
    elif backfilled:
        print("\nUniformity check: FAIL (see histogram).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
