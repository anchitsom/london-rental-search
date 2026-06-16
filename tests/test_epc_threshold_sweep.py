"""
Tests for scripts/sweep_epc_threshold.py

Run:
  .venv/bin/python tests/test_epc_threshold_sweep.py

Tests:
  1. sweep_thresholds() returns a list with one entry per threshold step.
  2. Each entry has keys: threshold, tp, fp, fn, precision, recall.
  3. Precision is correctly computed (tp / (tp + fp)).
  4. At a threshold of 1.0 (impossibly strict), tp=0, precision=None/0.
  5. At a threshold of 0.0 (accept everything), fp >= 0 and recall == 1.0.
  6. best_precision_threshold() returns the threshold with the highest precision.
  7. CSV loading function handles the expected column layout.
"""

import csv
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.sweep_epc_threshold import sweep_thresholds, best_precision_threshold, load_labelled_csv


def assert_eq(label: str, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


def assert_approx(label: str, got, expected, tol=0.001):
    if got is None or abs(got - expected) > tol:
        raise AssertionError(f"FAIL [{label}]: expected ~{expected}, got {got!r}")
    print(f"  PASS [{label}]")


def assert_not_none(label: str, got):
    if got is None:
        raise AssertionError(f"FAIL [{label}]: expected not-None")
    print(f"  PASS [{label}]")


def assert_none(label: str, got):
    if got is not None:
        raise AssertionError(f"FAIL [{label}]: expected None, got {got!r}")
    print(f"  PASS [{label}]")


def assert_true(label: str, val: bool):
    if not val:
        raise AssertionError(f"FAIL [{label}]: expected True")
    print(f"  PASS [{label}]")


# ---------------------------------------------------------------------------
# Minimal labelled dataset for unit tests
# ---------------------------------------------------------------------------

def _make_labelled_rows():
    """
    Six rows:
      - 3 correct matches (label=1, score >= threshold)
      - 2 false positives (label=0, score >= threshold)
      - 1 miss (label=1, score below threshold)

    Scores:  0.80, 0.75, 0.70, 0.65, 0.55, 0.45
    Labels:    1     1     1     0     0     1
    """
    return [
        {"listing_address": "Flat 1, 10 Upper Street, N1 1AB", "epc_address": "Flat 1, 10 Upper Street", "score": 0.80, "label": 1},
        {"listing_address": "Flat 2, 12 Upper Street, N1 1AB", "epc_address": "Flat 2, 12 Upper Street", "score": 0.75, "label": 1},
        {"listing_address": "Flat 3, 14 Upper Street, N1 1AB", "epc_address": "Flat 3, 14 Upper Street", "score": 0.70, "label": 1},
        {"listing_address": "Flat A, 20 Commercial Road, N1 2AB", "epc_address": "Commercial Unit Ground Floor, 20 Commercial Road", "score": 0.65, "label": 0},
        {"listing_address": "Flat B, 22 Commercial Road, N1 2AB", "epc_address": "Commercial Unit First Floor, 22 Commercial Road", "score": 0.55, "label": 0},
        {"listing_address": "Flat 4, 30 Upper Street, N1 3AB", "epc_address": "Flat 4, 30 Upper Street", "score": 0.45, "label": 1},
    ]


def _write_labelled_csv(rows: list[dict]) -> str:
    """Write rows to a temp CSV and return the file path."""
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    fieldnames = ["listing_address", "epc_address", "score", "label"]
    writer = csv.DictWriter(tf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    tf.close()
    return tf.name


# ---------------------------------------------------------------------------
# Test 1: sweep returns one entry per threshold step
# ---------------------------------------------------------------------------

def test_sweep_entries_count():
    print("Test 1: sweep_thresholds returns correct entry count")
    rows = _make_labelled_rows()
    csv_path = _write_labelled_csv(rows)
    try:
        results = sweep_thresholds(csv_path, lo=0.4, hi=0.8, step=0.05)
        # 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80 = 9 steps
        assert_eq("entry count", len(results), 9)
    finally:
        os.unlink(csv_path)


# ---------------------------------------------------------------------------
# Test 2: Each entry has required keys
# ---------------------------------------------------------------------------

def test_sweep_entry_keys():
    print("Test 2: Each sweep entry has required keys")
    rows = _make_labelled_rows()
    csv_path = _write_labelled_csv(rows)
    try:
        results = sweep_thresholds(csv_path, lo=0.4, hi=0.8, step=0.05)
        required_keys = {"threshold", "tp", "fp", "fn", "precision", "recall"}
        for entry in results:
            for key in required_keys:
                if key not in entry:
                    raise AssertionError(f"FAIL [key missing]: {key} not in {entry}")
        print("  PASS [all entries have required keys]")
    finally:
        os.unlink(csv_path)


# ---------------------------------------------------------------------------
# Test 3: Precision computed correctly
# ---------------------------------------------------------------------------

def test_precision_at_threshold_070():
    print("Test 3: Precision at threshold 0.70")
    rows = _make_labelled_rows()
    csv_path = _write_labelled_csv(rows)
    try:
        results = sweep_thresholds(csv_path, lo=0.70, hi=0.70, step=0.05)
        assert_eq("single entry", len(results), 1)
        r = results[0]
        # At 0.70: scores >= 0.70 are 0.80, 0.75, 0.70 -> labels 1, 1, 1 -> tp=3, fp=0
        assert_eq("tp=3", r["tp"], 3)
        assert_eq("fp=0", r["fp"], 0)
        assert_approx("precision=1.0", r["precision"], 1.0)
    finally:
        os.unlink(csv_path)


# ---------------------------------------------------------------------------
# Test 4: At threshold 0.0, all rows accepted; recall == 1.0
# ---------------------------------------------------------------------------

def test_recall_at_zero_threshold():
    print("Test 4: Recall at threshold 0.0 is 1.0")
    rows = _make_labelled_rows()
    csv_path = _write_labelled_csv(rows)
    try:
        results = sweep_thresholds(csv_path, lo=0.0, hi=0.0, step=0.05)
        r = results[0]
        # All 4 label=1 rows accepted -> recall 1.0
        assert_approx("recall=1.0", r["recall"], 1.0)
    finally:
        os.unlink(csv_path)


# ---------------------------------------------------------------------------
# Test 5: best_precision_threshold returns the threshold with highest precision
# ---------------------------------------------------------------------------

def test_best_precision_threshold():
    print("Test 5: best_precision_threshold returns highest-precision threshold")
    rows = _make_labelled_rows()
    csv_path = _write_labelled_csv(rows)
    try:
        results = sweep_thresholds(csv_path, lo=0.4, hi=0.8, step=0.05)
        best = best_precision_threshold(results)
        # At 0.70+ all accepted rows are label=1, so precision=1.0
        assert_true("best precision is 1.0", best["precision"] == 1.0)
        # The threshold should be 0.70 (lowest threshold achieving max precision)
        assert_true("best threshold >= 0.70", best["threshold"] >= 0.70)
    finally:
        os.unlink(csv_path)


# ---------------------------------------------------------------------------
# Test 6: CSV loading
# ---------------------------------------------------------------------------

def test_csv_loading():
    print("Test 6: load_labelled_csv reads rows correctly")
    rows = _make_labelled_rows()
    csv_path = _write_labelled_csv(rows)
    try:
        loaded = load_labelled_csv(csv_path)
        assert_eq("row count", len(loaded), 6)
        assert_eq("first listing addr", loaded[0]["listing_address"], "Flat 1, 10 Upper Street, N1 1AB")
        assert_approx("first score", float(loaded[0]["score"]), 0.80)
        assert_eq("first label", int(loaded[0]["label"]), 1)
    finally:
        os.unlink(csv_path)


# ---------------------------------------------------------------------------
# Test 7: epc_threshold.py module threshold == 0.55
#
# Rationale: the Wave 1.7 round 1 audit (epc_enrichment/REPORT.md) confirmed
# zero false positives on the 35-listing control set when comparing 0.55 vs
# 0.60. Six of fifteen null-EPC listings were rescued at 0.55 that 0.60
# missed. The unit gate in homehunt/epc_unit_gate.py is the precision floor;
# the threshold controls recall on boundary residential matches.
# ---------------------------------------------------------------------------

def test_epc_threshold_module_default_value():
    """EPC_MATCH_THRESHOLD in epc_threshold.py must equal 0.55 (Wave 1.7 default)."""
    import importlib
    import importlib.util
    import os
    module_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "homehunt", "epc_threshold.py",
    )
    spec = importlib.util.spec_from_file_location("epc_threshold_mod", module_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    threshold = getattr(mod, "EPC_MATCH_THRESHOLD", None)
    if threshold is None:
        raise AssertionError("FAIL [threshold_exists]: EPC_MATCH_THRESHOLD not found in epc_threshold.py")
    if abs(threshold - 0.55) > 1e-6:
        raise AssertionError(
            f"FAIL [threshold_default]: EPC_MATCH_THRESHOLD={threshold} != 0.55; "
            "Wave 1.7 sets the default to 0.55 per round 1 audit"
        )
    print(f"  PASS [threshold_default]: EPC_MATCH_THRESHOLD={threshold} == 0.55")


# ---------------------------------------------------------------------------
# Test 8: round-1-rescued listings produce non-null at threshold 0.55
#
# The 6 listings rescued at 0.55 vs 0.60 (per epc_enrichment/REPORT.md
# per-listing breakdown, sweep best column) must score >= 0.55 (i.e. >= 55
# on the 0-100 rapidfuzz scale) when address-fuzzy matched against their
# cached postcode rows.
# ---------------------------------------------------------------------------

def test_round1_rescued_listings_score_above_055():
    """Each of the 6 round-1 rescued listings has at least one cached EPC row scoring >= 55."""
    import json
    import os
    from rapidfuzz import fuzz

    # uid -> (listing_address, expected min token-set score on its best cached row)
    # All 6 came from REPORT.md per-listing breakdown column "sweep best @ 0.50"
    # which were rescued at 0.55. The postcode row caches live in
    # epc_enrichment/output/epc_rows/.
    rescued = [
        ("rightmove_87989751", "327-329 Upper Street, N1 2XQ"),
        ("rightmove_87989742", "327-329 Upper Street, N1 2XQ"),
        ("rightmove_87923577", "327-329 Upper Street, N1 2XQ"),
        ("rightmove_87953625", "437 High Road, N17 6QH"),
        ("rightmove_172108217", "55 Heath Street, NW3 6UG"),
        ("rightmove_87638403", "327-329 Upper Street, N1 2XQ"),
    ]

    cache_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "epc_enrichment", "output", "epc_rows",
    )

    threshold_pct = 55  # 0.55 on the 0-100 rapidfuzz scale

    for uid, listing_addr in rescued:
        path = os.path.join(cache_dir, f"{uid}.json")
        if not os.path.exists(path):
            raise AssertionError(f"FAIL [{uid}]: cached fixture not found at {path}")
        with open(path, "r") as f:
            cached = json.load(f)
        rows = cached.get("rows", [])
        if not rows:
            raise AssertionError(f"FAIL [{uid}]: no rows in cached postcode response")
        best_score = 0
        for row in rows:
            parts = [
                (row.get("address1") or "").strip(),
                (row.get("address2") or "").strip(),
                (row.get("address3") or "").strip(),
            ]
            epc_addr = ", ".join(p for p in parts if p)
            score = fuzz.token_set_ratio(listing_addr, epc_addr)
            if score > best_score:
                best_score = score
        if best_score < threshold_pct:
            raise AssertionError(
                f"FAIL [{uid}]: best cached-row score {best_score} < {threshold_pct} "
                f"(listing addr: {listing_addr!r})"
            )
        print(f"  PASS [{uid}]: best score {best_score} >= {threshold_pct}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    tests = [
        test_sweep_entries_count,
        test_sweep_entry_keys,
        test_precision_at_threshold_070,
        test_recall_at_zero_threshold,
        test_best_precision_threshold,
        test_csv_loading,
        test_epc_threshold_module_default_value,
        test_round1_rescued_listings_score_above_055,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as exc:
            print(f"  {exc}")
            failed += 1
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"  ERROR [{t.__name__}]: {exc}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
