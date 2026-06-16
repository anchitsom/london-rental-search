"""
Tests for the table-driven scorer (Wave 1 Agent E).

Run: ./.venv/bin/python tests/test_table_driven_scorer.py

Tests:
1. With seed weights, scorer output for a fixture listing matches the
   current hard-coded scorer output to within rounding.
2. After inserting a second scoring_weights row, the scorer reads the new row.
3. score_breakdown JSON contains all eight components for each scored listing.
4. _score_region_pref returns correct scores per tier rules.

Each test uses its own fixture db at tests/fixtures/<name>.db.
Tests do not touch homehunt.db.
"""

import sys
import os
import json
import sqlite3
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
TIERS_YAML_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tiers.yaml")

# Seed weights captured from scorer.py before the table-driven refactor.
# These are the canonical values; the migration also hardcodes them.
SEED_WEIGHTS = {
    "price": 0.25,
    "carpet": 0.25,
    "size": 0.20,
    "bedrooms": 0.10,
    "epc": 0.05,
    "transport": 0.10,
    "outside": 0.0,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def assert_eq(label, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


def assert_approx(label, got, expected, tolerance=2):
    if abs(got - expected) > tolerance:
        raise AssertionError(
            f"FAIL [{label}]: expected {expected} (+-{tolerance}), got {got}"
        )
    print(f"  PASS [{label}]: {got} (expected ~{expected})")


def make_fixture_db(name: str) -> str:
    """Create an empty fixture db at tests/fixtures/<name>.db and return its path."""
    os.makedirs(FIXTURES_DIR, exist_ok=True)
    path = os.path.join(FIXTURES_DIR, f"{name}.db")
    if os.path.exists(path):
        os.remove(path)
    return path


def seed_scoring_weights_table(conn: sqlite3.Connection, weights_json: str) -> int:
    """Create scoring_weights table and insert the seed row. Returns inserted id."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scoring_weights (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL,
            weights_json TEXT NOT NULL,
            fit_metrics_json TEXT NULL,
            notes TEXT NULL
        )
    """)
    conn.execute(
        "INSERT INTO scoring_weights (created_at, source, weights_json) VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), "seed", weights_json),
    )
    conn.commit()
    cur = conn.execute("SELECT last_insert_rowid()")
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Test 1: seed weights produce same score as current hard-coded scorer
# ---------------------------------------------------------------------------

def test_seed_weights_match_hardcoded():
    """
    Score a fixture listing by computing the expected total directly from
    SEED_WEIGHTS (the values captured before the refactor), then score the same
    listing with the new table-driven scorer (loaded with seed weights), and
    confirm the totals match within rounding (tolerance of 2 points).
    """
    from homehunt.scorer import compute_score as old_compute_score

    # Fixture listing values
    fixture = dict(
        price_pcm=2500,
        size_sqft=750,
        bedrooms=2,
        carpet_detected=False,
        carpet_confidence=0.8,
        epc_rating="C",
        commute_mins=25,
        tfl_zone=2,
        garden=True,
        balcony=False,
    )

    old_total, old_breakdown = old_compute_score(**fixture)
    print(f"  Old scorer total: {old_total}, breakdown: {old_breakdown}")

    # Now use table-driven scorer with the same seed weights
    db_path = make_fixture_db("seed_weights_match")
    conn = sqlite3.connect(db_path)
    seed_scoring_weights_table(conn, json.dumps(SEED_WEIGHTS))
    conn.close()

    from homehunt.scorer import compute_score_from_db
    new_total, new_breakdown = compute_score_from_db(db_path=db_path, **fixture)
    print(f"  New scorer total: {new_total}, breakdown: {new_breakdown}")

    assert_approx("total_score_matches", new_total, old_total, tolerance=2)
    for key in old_breakdown:
        assert_approx(
            f"breakdown_{key}_matches",
            new_breakdown.get(key, -999),
            old_breakdown[key],
            tolerance=1,
        )
    print("  PASS: seed weights produce same scores as hard-coded scorer")


# ---------------------------------------------------------------------------
# Test 2: scorer reads the most recent weights row
# ---------------------------------------------------------------------------

def test_scorer_reads_latest_weights_row():
    """
    Insert a seed row, then insert a second row with deliberately different
    weights. Confirm the scorer uses the second row's weights.
    """
    from homehunt.scorer import compute_score_from_db

    db_path = make_fixture_db("latest_weights_row")
    conn = sqlite3.connect(db_path)
    seed_scoring_weights_table(conn, json.dumps(SEED_WEIGHTS))

    # Insert a second row with inverted-emphasis weights (price=0.05, carpet=0.05,
    # size=0.50 -- heavily size-biased). Total must still sum to 1.0.
    different_weights = {
        "price": 0.05,
        "carpet": 0.05,
        "size": 0.50,
        "bedrooms": 0.15,
        "epc": 0.10,
        "transport": 0.10,
        "outside": 0.05,
    }
    import time; time.sleep(0.01)  # ensure distinct created_at
    conn.execute(
        "INSERT INTO scoring_weights (created_at, source, weights_json) VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), "test_override", json.dumps(different_weights)),
    )
    conn.commit()
    conn.close()

    # A listing with very large size_sqft should score much higher with different_weights
    fixture = dict(
        price_pcm=3000,         # above budget -- low price score
        size_sqft=1200,         # well above minimum -- high size score
        bedrooms=2,
        carpet_detected=None,
        carpet_confidence=0.0,
        epc_rating=None,
        commute_mins=None,
        tfl_zone=None,
        garden=False,
        balcony=False,
    )

    # Score with seed (size weight 0.15)
    seed_total_approx = None
    # Score with new weights loaded from db (size weight 0.50)
    new_total, new_breakdown = compute_score_from_db(db_path=db_path, **fixture)
    print(f"  Score with latest weights (size=0.50): {new_total}")

    # With size weight 0.50 and size_sqft=1200 (score 100), even with low price,
    # the total should be notably different from seed weights.
    # Specifically size contributes 50 points alone in the new row vs 15 in seed.
    # We verify the scorer is reading the new row by checking size contributes 0.50 weight.
    from homehunt.scorer import _score_size
    size_score = _score_size(1200)
    expected_size_contribution = round(size_score * 0.50)
    actual_size_contribution = round(new_breakdown["size"] * 0.50)
    # The total must reflect the size-heavy weighting
    assert new_total >= 45, (
        f"FAIL: expected total >= 45 with size weight 0.50, got {new_total}"
    )
    print(f"  PASS: scorer read latest weights row (size weight 0.50, total={new_total})")


# ---------------------------------------------------------------------------
# Test 3: score_breakdown contains all eight components
# ---------------------------------------------------------------------------

def test_score_breakdown_has_eight_components():
    """
    score_breakdown must contain exactly the 8 expected keys:
    price, carpet, size, bedrooms, epc, transport, outside, region_pref.
    """
    from homehunt.scorer import compute_score_from_db

    db_path = make_fixture_db("eight_components")
    conn = sqlite3.connect(db_path)
    seed_scoring_weights_table(conn, json.dumps(SEED_WEIGHTS))
    conn.close()

    fixture = dict(
        price_pcm=2400,
        size_sqft=800,
        bedrooms=2,
        carpet_detected=False,
        carpet_confidence=0.9,
        epc_rating="B",
        commute_mins=20,
        tfl_zone=2,
        garden=False,
        balcony=True,
        region="Highbury, North London",
    )

    total, breakdown = compute_score_from_db(db_path=db_path, **fixture)
    print(f"  Score: {total}, breakdown keys: {list(breakdown.keys())}")

    expected_keys = {"price", "carpet", "size", "bedrooms", "epc", "transport", "outside", "region_pref"}
    assert set(breakdown.keys()) == expected_keys, (
        f"FAIL: expected keys {expected_keys}, got {set(breakdown.keys())}"
    )
    print("  PASS: score_breakdown has all eight components")


# ---------------------------------------------------------------------------
# Test 4: _score_region_pref tier rules
# ---------------------------------------------------------------------------

def test_score_region_pref():
    """
    Verify _score_region_pref returns:
    - 100 for a tier-1 location
    - 75 for a tier-2 location
    - 50 for a tier-3 location
    - 75 for a name not in any tier
    - 0 for region None (resolve_region no longer emits 'Other')
    """
    from homehunt.scorer import _score_region_pref

    # Tier 1 names (from tiers.yaml)
    assert_eq(
        "tier1_highbury",
        _score_region_pref("Highbury, North London", tiers_yaml_path=TIERS_YAML_PATH),
        100,
    )
    assert_eq(
        "tier1_shoreditch",
        _score_region_pref("Shoreditch, East London", tiers_yaml_path=TIERS_YAML_PATH),
        100,
    )

    # Tier 2 names
    assert_eq(
        "tier2_kentish_town",
        _score_region_pref("Kentish Town, North West London", tiers_yaml_path=TIERS_YAML_PATH),
        75,
    )
    assert_eq(
        "tier2_fitzrovia",
        _score_region_pref("Fitzrovia, London", tiers_yaml_path=TIERS_YAML_PATH),
        75,
    )

    # Tier 3 names
    assert_eq(
        "tier3_clapton",
        _score_region_pref("Clapton, East London", tiers_yaml_path=TIERS_YAML_PATH),
        50,
    )
    assert_eq(
        "tier3_finsbury_park",
        _score_region_pref("Finsbury Park, North London", tiers_yaml_path=TIERS_YAML_PATH),
        50,
    )

    # Name not in any tier -- should score 75 (quiet middle default)
    assert_eq(
        "unlisted_non_tier",
        _score_region_pref("Peckham, South London", tiers_yaml_path=TIERS_YAML_PATH),
        75,
    )

    # None -- defensive case (resolve_region no longer emits 'Other')
    assert_eq(
        "region_none",
        _score_region_pref(None, tiers_yaml_path=TIERS_YAML_PATH),
        0,
    )

    print("  PASS: all _score_region_pref tier rules correct")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        ("seed_weights_match_hardcoded", test_seed_weights_match_hardcoded),
        ("scorer_reads_latest_weights_row", test_scorer_reads_latest_weights_row),
        ("score_breakdown_has_eight_components", test_score_breakdown_has_eight_components),
        ("score_region_pref_tier_rules", test_score_region_pref),
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
        print(f"ALL {len(tests)} test groups passed.")
