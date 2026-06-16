"""
Pre-Wave-3 unblocker 2.3 (optional test): backfill rewrites legacy
seven-key score_breakdown rows into the post-fix nine-key shape.

Constructs a tiny fixture DB with three active listings carrying the
legacy single-`carpet` shape, runs the backfill subprocess, then asserts
every row now has the new shape (including region_pref, carpet_bedroom,
carpet_other_areas) and the legacy `carpet` key is gone.

Run:
  ./.venv/bin/python \
    tests/test_backfill_score_breakdown.py
"""

import json
import os
import subprocess
import sys
import sqlite3
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(PROJECT_ROOT, "alembic.ini")
PYTHON_BIN = sys.executable
BACKFILL_SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "backfill_score_breakdown.py")

EXPECTED_KEYS = {
    "price",
    "size",
    "bedrooms",
    "epc",
    "transport",
    "outside",
    "carpet_bedroom",
    "carpet_other_areas",
    "region_pref",
}


def _bootstrap_fixture_db(db_path: str) -> None:
    """Bring up a clean DB schema and stamp at the new seed revision."""
    from homehunt.core.db import Database

    db = Database(database_url=f"sqlite:///{db_path}")
    db.create_tables()
    db.engine.dispose()

    env = os.environ.copy()
    env["HOMEHUNT_DB"] = db_path
    upgrade = subprocess.run(
        [PYTHON_BIN, "-m", "alembic", "-c", ALEMBIC_INI, "stamp", "1777920000"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
    )
    if upgrade.returncode != 0:
        raise AssertionError(
            f"alembic stamp failed: {upgrade.stderr}"
        )
    upgrade = subprocess.run(
        [PYTHON_BIN, "-m", "alembic", "-c", ALEMBIC_INI, "upgrade", "head"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
    )
    if upgrade.returncode != 0:
        raise AssertionError(
            f"alembic upgrade head failed: {upgrade.stderr}"
        )


def _seed_listings_with_legacy_breakdown(db_path: str) -> int:
    """
    Insert three minimal active listings into the fixture DB. Each carries
    the legacy seven-key score_breakdown and persisted carpet split flags
    so the rescore can produce the nine-key shape.
    """
    legacy_breakdown = json.dumps({
        "price": 80,
        "carpet": 50,
        "size": 90,
        "bedrooms": 100,
        "epc": 100,
        "transport": 80,
        "outside": 60,
    })
    rows = [
        {
            "uid": "rightmove:test-1",
            "portal": "RIGHTMOVE",
            "property_id": "test-1",
            "url": "https://example.test/properties/test-1",
            "extraction_method": "DIRECT_HTTP",
            "price_numeric": 240000,
            "bedrooms": 2,
            "size_sqft": 720,
            "epc_rating": "B",
            "tfl_zone": 2,
            "garden": False,
            "balcony": True,
            "carpet_in_bedroom": False,
            "carpet_other_areas": False,
            "commute_canary_wharf": 25,
            "region": "Highbury, North London",
            "score": 85,
            "score_breakdown": legacy_breakdown,
        },
        {
            "uid": "rightmove:test-2",
            "portal": "RIGHTMOVE",
            "property_id": "test-2",
            "url": "https://example.test/properties/test-2",
            "extraction_method": "DIRECT_HTTP",
            "price_numeric": 260000,
            "bedrooms": 1,
            "size_sqft": 480,
            "epc_rating": "C",
            "tfl_zone": 2,
            "garden": True,
            "balcony": False,
            "carpet_in_bedroom": True,
            "carpet_other_areas": None,
            "commute_canary_wharf": 32,
            "region": "Clapton, East London",
            "score": 70,
            "score_breakdown": legacy_breakdown,
        },
        {
            "uid": "rightmove:test-3",
            "portal": "RIGHTMOVE",
            "property_id": "test-3",
            "url": "https://example.test/properties/test-3",
            "extraction_method": "DIRECT_HTTP",
            "price_numeric": 220000,
            "bedrooms": 2,
            "size_sqft": 700,
            "epc_rating": "D",
            "tfl_zone": 3,
            "garden": False,
            "balcony": False,
            "carpet_in_bedroom": None,
            "carpet_other_areas": None,
            "commute_canary_wharf": 40,
            "region": None,  # null region path
            "score": 60,
            "score_breakdown": legacy_breakdown,
        },
    ]

    conn = sqlite3.connect(db_path)
    try:
        for row in rows:
            cols = list(row.keys()) + [
                "first_seen", "last_scraped", "scrape_count",
                "is_active", "status",
            ]
            placeholders = ", ".join(["?"] * len(cols))
            now = datetime.now(timezone.utc).isoformat()
            values = list(row.values()) + [now, now, 1, 1, "active"]
            conn.execute(
                f"INSERT INTO listing ({', '.join(cols)}) VALUES ({placeholders})",
                values,
            )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def test_backfill_rewrites_legacy_breakdown_into_new_shape():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as fh:
        db_path = fh.name
    try:
        _bootstrap_fixture_db(db_path)
        n_inserted = _seed_listings_with_legacy_breakdown(db_path)
        if n_inserted != 3:
            raise AssertionError(f"FAIL: expected 3 seed rows, got {n_inserted}")

        # Confirm the legacy shape is what we just wrote.
        conn = sqlite3.connect(db_path)
        before = conn.execute(
            "SELECT COUNT(*) FROM listing "
            "WHERE json_extract(score_breakdown, '$.carpet') IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        if before != 3:
            raise AssertionError(
                f"FAIL: pre-backfill expected 3 rows with legacy 'carpet' key, got {before}"
            )

        env = os.environ.copy()
        env["HOMEHUNT_DB"] = db_path
        result = subprocess.run(
            [PYTHON_BIN, BACKFILL_SCRIPT],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"FAIL: backfill exited nonzero ({result.returncode})\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        print(result.stdout)

        conn = sqlite3.connect(db_path)
        try:
            # Every row must now carry all nine expected keys.
            for key in EXPECTED_KEYS:
                count = conn.execute(
                    f"SELECT COUNT(*) FROM listing "
                    f"WHERE json_extract(score_breakdown, '$.{key}') IS NOT NULL"
                ).fetchone()[0]
                if count != 3:
                    raise AssertionError(
                        f"FAIL: expected key '{key}' on all 3 rows, got {count}"
                    )

            # Legacy single-`carpet` key must be gone.
            legacy = conn.execute(
                "SELECT COUNT(*) FROM listing "
                "WHERE json_extract(score_breakdown, '$.carpet') IS NOT NULL"
            ).fetchone()[0]
            if legacy != 0:
                raise AssertionError(
                    f"FAIL: legacy 'carpet' key still present on {legacy} rows"
                )

            # Region-pref behaviour: tier-1 listing scores 100, null region scores 0.
            row1 = json.loads(conn.execute(
                "SELECT score_breakdown FROM listing WHERE uid = 'rightmove:test-1'"
            ).fetchone()[0])
            row3 = json.loads(conn.execute(
                "SELECT score_breakdown FROM listing WHERE uid = 'rightmove:test-3'"
            ).fetchone()[0])
            if row1["region_pref"] != 100:
                raise AssertionError(
                    f"FAIL: tier-1 region_pref expected 100, got {row1['region_pref']}"
                )
            if row3["region_pref"] != 0:
                raise AssertionError(
                    f"FAIL: null region region_pref expected 0, got {row3['region_pref']}"
                )

            print("  PASS: all 3 rows now carry the nine-key shape")
            print(f"  PASS: row1 region_pref = {row1['region_pref']} (Highbury, tier 1)")
            print(f"  PASS: row3 region_pref = {row3['region_pref']} (null region)")
        finally:
            conn.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


if __name__ == "__main__":
    failures = []
    print("\n=== test_backfill_rewrites_legacy_breakdown_into_new_shape ===")
    try:
        test_backfill_rewrites_legacy_breakdown_into_new_shape()
    except AssertionError as exc:
        print(f"  {exc}")
        failures.append("test_backfill_rewrites_legacy_breakdown_into_new_shape")
    except Exception as exc:
        import traceback
        traceback.print_exc()
        failures.append("test_backfill_rewrites_legacy_breakdown_into_new_shape")

    print(f"\n{'=' * 60}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print("ALL 1 test passed.")
