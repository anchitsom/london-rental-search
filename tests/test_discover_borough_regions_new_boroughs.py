"""
Tests for the added Haringey and Waltham Forest discovery targets.

Run:
  .venv/bin/python tests/test_discover_borough_regions_new_boroughs.py
"""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


EXPECTED_BOROUGHS = {
    "haringey": [
        ("Tottenham Hale, North London", "Tottenham Hale London"),
        ("Seven Sisters, North London", "Seven Sisters Station"),
    ],
    "waltham_forest": [
        ("Blackhorse Road, East London", "Blackhorse Road"),
        ("Walthamstow, East London", "Walthamstow"),
    ],
}

# Rightmove typeahead returns REGION for named areas and STATION for some
# places that only have a tube/overground stop in their name. Both are valid
# locationIdentifiers; the discovery script falls back to STATION when no
# REGION is offered.
ID_PATTERN = r"(?:REGION|STATION)\^\d+"


def assert_eq(label, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


def assert_regex(label, text, pattern):
    if not re.search(pattern, text):
        raise AssertionError(f"FAIL [{label}]: pattern {pattern!r} not found")
    print(f"  PASS [{label}]")


def _looks_like_network_failure(proc):
    combined = f"{proc.stdout}\n{proc.stderr}".lower()
    needles = [
        "httpx.",
        "connecterror",
        "connecttimeout",
        "readtimeout",
        "network is unreachable",
        "nodename nor servname",
        "temporary failure",
        "certificate verify failed",
    ]
    return any(needle in combined for needle in needles)


def test_borough_dict_contains_new_entries():
    from scripts.discover_borough_regions import BOROUGH_NEIGHBOURHOODS

    for borough, expected in EXPECTED_BOROUGHS.items():
        assert_eq(f"{borough}_entries", BOROUGH_NEIGHBOURHOODS[borough], expected)


def test_live_script_output_for_boroughs():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    for borough in EXPECTED_BOROUGHS:
        proc = subprocess.run(
            [sys.executable, "scripts/discover_borough_regions.py", borough],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=90,
        )
        if proc.returncode != 0 and _looks_like_network_failure(proc):
            print(f"  SKIPPED [network] {borough}")
            continue
        if proc.returncode != 0:
            raise AssertionError(
                f"FAIL [{borough}_script]: exit {proc.returncode}\n"
                f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )

        for display_name, _query in EXPECTED_BOROUGHS[borough]:
            short_name = display_name.split(",", 1)[0]
            assert_regex(
                f"{borough}_{short_name}_region",
                proc.stdout,
                rf"{ID_PATTERN}\s+{re.escape(short_name)}",
            )


if __name__ == "__main__":
    tests = [
        ("borough_dict_contains_new_entries", test_borough_dict_contains_new_entries),
        ("live_script_output_for_boroughs", test_live_script_output_for_boroughs),
    ]

    failures = []
    for test_name, fn in tests:
        print(f"\n=== {test_name} ===")
        try:
            fn()
        except AssertionError as exc:
            print(f"  {exc}")
            failures.append(test_name)
        except Exception as exc:
            import traceback

            print(f"  ERROR in {test_name}: {exc}")
            traceback.print_exc()
            failures.append(test_name)

    print(f"\n{'=' * 50}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)

    print(f"ALL {len(tests)} test groups passed.")
