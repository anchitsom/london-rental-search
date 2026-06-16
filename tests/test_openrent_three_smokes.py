"""
OpenRent Integration - Task 11: three consecutive ad-hoc runs.

Expected behaviour:
- Run 1: discovers candidates, fetches new ones, saves rows.
- Runs 2-3: same candidates within freshness horizon, touched not re-saved.

Asserts: row count growth on run 1 may be 0 (if everything is fresh from prior
smokes) or positive (new listings). Subsequent runs must not grow the count
beyond +1 per run (allows for natural listing churn during the test).
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("HOMEHUNT_DB", str(PROJECT_ROOT / "data" / "homehunt.db"))


def db_count() -> int:
    from sqlmodel import Session, select, func
    from homehunt.core.db import Database, Listing
    from homehunt.core.models import Portal
    db = Database()
    with Session(db.engine) as s:
        return s.exec(select(func.count(Listing.uid)).where(Listing.portal == Portal.OPENRENT)).one()


def trigger(label: str) -> int:
    env = os.environ.copy()
    env["OPENRENT_MAX_LISTINGS"] = "5"
    print(f"--- triggering smoke {label} ---")
    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "openrent-adhoc-run.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-1000:])
        raise AssertionError(f"{label} exited {result.returncode}")
    # Extract the summary dict from the last line for visibility
    for line in reversed(result.stdout.splitlines()):
        if "openrent-adhoc complete:" in line:
            print(f"  {line.strip()}")
            break
    return db_count()


def main() -> int:
    n0 = db_count()
    n1 = trigger("run-1")
    n2 = trigger("run-2")
    n3 = trigger("run-3")
    print(f"\nDB OpenRent rows: before={n0} after-1={n1} after-2={n2} after-3={n3}")
    delta_1, delta_2, delta_3 = n1 - n0, n2 - n1, n3 - n2
    # Run 1 may add new rows (if any candidates were stale) OR add zero (if all
    # candidates were within the 23h freshness horizon from prior smokes).
    # Runs 2 and 3 must show the freshness gate is working: at most +1 per run
    # (covers natural OpenRent listing churn during the ~30s between runs).
    assert delta_2 <= 1, f"Run 2 added {delta_2} rows; freshness gate broken"
    assert delta_3 <= 1, f"Run 3 added {delta_3} rows; freshness gate broken"
    print(f"PASS - three smokes idempotent (deltas {delta_1}/{delta_2}/{delta_3}; gate held)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
