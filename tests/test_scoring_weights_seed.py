"""
Pre-Wave-3 unblocker 2.2: alembic migration for scoring_weights table.

The legacy migration `homehunt/migrations/20260504041646_*.py` was never
applied to data/homehunt.db because it lived outside the alembic chain.
A fresh alembic revision creates the table and seeds it from
filter-scoring-config.yaml.

Tests:
  1. Upgrade head on a clean fixture DB creates scoring_weights with the
     expected columns.
  2. The seed row has source = 'seed' and weights_json matching the yaml's
     scoring.components.*.weight values.
  3. Downgrade removes the table cleanly.

Run:
  ./.venv/bin/python \
    tests/test_scoring_weights_seed.py
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(PROJECT_ROOT, "alembic.ini")
CONFIG_YAML = os.path.join(PROJECT_ROOT, "filter-scoring-config.yaml")
# The venv lives at the canonical project root, not necessarily in this
# worktree. Resolve sys.executable so the test works in any worktree.
PYTHON_BIN = sys.executable
SEED_REVISION = "1777930000"


def _run_alembic(db_path: str, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOMEHUNT_DB"] = db_path
    cmd = [PYTHON_BIN, "-m", "alembic", "-c", ALEMBIC_INI, *args]
    return subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _bootstrap_db_at_wave27(db_path: str) -> None:
    """
    Bring a fresh fixture DB to the same alembic state as production
    immediately before this revision. The pre-existing alembic chain assumes
    `listing` already exists (its first revision adds a region column to it),
    so a from-scratch upgrade fails. Production was bootstrapped via the
    Database class (SQLModel.create_all) and then stamped at the latest
    revision. Mirror that here: Database.create_tables + stamp 1777920000.
    """
    from homehunt.core.db import Database

    db = Database(database_url=f"sqlite:///{db_path}")
    db.create_tables()
    db.engine.dispose()

    # Need a foreign key from listing.cluster_id to listing_cluster.cluster_id;
    # SQLModel.metadata creates listing_cluster as well, so the FK works.
    stamp = _run_alembic(db_path, "stamp", "1777920000")
    if stamp.returncode != 0:
        raise AssertionError(
            f"FAIL: alembic stamp 1777920000 nonzero:\n"
            f"stdout: {stamp.stdout}\nstderr: {stamp.stderr}"
        )


def _yaml_seed_weights() -> dict:
    with open(CONFIG_YAML, "r") as fh:
        cfg = yaml.safe_load(fh) or {}
    components = (cfg.get("scoring") or {}).get("components") or {}
    return {k: v.get("weight", 0.0) for k, v in components.items()}


def test_upgrade_creates_table_and_seeds_row():
    """
    A fresh DB at HEAD has the scoring_weights table with the expected
    columns and a single seed row.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as fh:
        db_path = fh.name
    try:
        _bootstrap_db_at_wave27(db_path)
        result = _run_alembic(db_path, "upgrade", "head")
        if result.returncode != 0:
            raise AssertionError(
                f"FAIL alembic upgrade nonzero ({result.returncode}):\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )

        conn = sqlite3.connect(db_path)
        try:
            schema_row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'scoring_weights'"
            ).fetchone()
            if not schema_row:
                raise AssertionError(
                    "FAIL: scoring_weights table missing after upgrade"
                )

            cols = [
                row[1]
                for row in conn.execute("PRAGMA table_info(scoring_weights)").fetchall()
            ]
            expected_cols = {
                "id",
                "created_at",
                "source",
                "weights_json",
                "fit_metrics_json",
                "notes",
            }
            if set(cols) != expected_cols:
                raise AssertionError(
                    f"FAIL: column set mismatch. "
                    f"expected {expected_cols}, got {set(cols)}"
                )

            row = conn.execute(
                "SELECT source, weights_json FROM scoring_weights "
                "WHERE source = 'seed'"
            ).fetchone()
            if not row:
                raise AssertionError("FAIL: seed row missing")
            if row[0] != "seed":
                raise AssertionError(
                    f"FAIL: expected source='seed', got '{row[0]}'"
                )

            persisted = json.loads(row[1])
            yaml_seed = _yaml_seed_weights()
            if persisted != yaml_seed:
                raise AssertionError(
                    f"FAIL: weights_json mismatch.\n"
                    f"  yaml:      {yaml_seed}\n"
                    f"  persisted: {persisted}"
                )

            count = conn.execute(
                "SELECT COUNT(*) FROM scoring_weights"
            ).fetchone()[0]
            if count != 1:
                raise AssertionError(
                    f"FAIL: expected exactly one row in scoring_weights, got {count}"
                )

            print(f"  PASS: scoring_weights table + seed row from yaml ({len(persisted)} keys)")
        finally:
            conn.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_downgrade_drops_table():
    """downgrade -1 from the seed revision drops the scoring_weights table."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as fh:
        db_path = fh.name
    try:
        _bootstrap_db_at_wave27(db_path)
        up = _run_alembic(db_path, "upgrade", "head")
        if up.returncode != 0:
            raise AssertionError(
                f"FAIL: upgrade failed before downgrade test: {up.stderr}"
            )

        # Step down off the seed revision so the table goes away.
        down = _run_alembic(db_path, "downgrade", "-1")
        if down.returncode != 0:
            raise AssertionError(
                f"FAIL alembic downgrade nonzero ({down.returncode}):\n"
                f"stdout: {down.stdout}\nstderr: {down.stderr}"
            )

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'scoring_weights'"
            ).fetchone()
            if row:
                raise AssertionError(
                    "FAIL: scoring_weights table still present after downgrade"
                )
            print("  PASS: downgrade dropped scoring_weights table")
        finally:
            conn.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_revision_is_in_chain_after_wave27_cluster():
    """
    The new revision must be sequenced after the Wave 2.7 cluster migration
    (1777920000). Read alembic history and assert the new revision's
    down_revision is 1777920000.
    """
    result = subprocess.run(
        [PYTHON_BIN, "-m", "alembic", "-c", ALEMBIC_INI, "history"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "HOMEHUNT_DB": "/tmp/__noop.db"},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"FAIL: alembic history nonzero ({result.returncode}):\n"
            f"stderr: {result.stderr}"
        )
    text = result.stdout
    # Look for an arrow line: 1777920000 -> <new>, ...
    if "1777920000 -> " not in text:
        raise AssertionError(
            f"FAIL: no revision sequenced after 1777920000 in history:\n{text}"
        )
    if SEED_REVISION not in text:
        raise AssertionError(
            f"FAIL: expected revision id {SEED_REVISION} in history\n{text}"
        )
    print(f"  PASS: revision {SEED_REVISION} sequenced after 1777920000")


if __name__ == "__main__":
    tests = [
        ("revision_is_in_chain_after_wave27_cluster", test_revision_is_in_chain_after_wave27_cluster),
        ("upgrade_creates_table_and_seeds_row", test_upgrade_creates_table_and_seeds_row),
        ("downgrade_drops_table", test_downgrade_drops_table),
    ]

    failures = []
    for name, fn in tests:
        print(f"\n=== {name} ===")
        try:
            fn()
        except AssertionError as exc:
            print(f"  {exc}")
            failures.append(name)
        except Exception as exc:
            import traceback
            print(f"  ERROR in {name}: {exc}")
            traceback.print_exc()
            failures.append(name)

    print(f"\n{'=' * 60}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print(f"ALL {len(tests)} tests passed.")
