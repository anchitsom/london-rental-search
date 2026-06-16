"""Unit tests for homehunt.size_parser.parse_size_sqft."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from homehunt.size_parser import parse_size_sqft


CASES = [
    ("650 sq ft", 650),
    ("1,000 sq ft", 1000),  # comma stripped by regex match on first number group
    ("62 sq m", 667),
    ("60 sq m", 646),
    ("650 sq ft / 60 sq m", 650),  # sq ft preferred when both present
    ("60 sq m / 650 sq ft", 650),  # order independent
    ("Ask agent", None),
    ("", None),
    (None, None),
    ("approx 720 sq.ft.", 720),
    ("750 sqft", 750),
    ("65 sqm", 700),
    ("contact agent for size", None),
    # Edge: too small to be a flat
    ("9 sq ft", None),  # below 2-digit floor, regex skips
]


def main():
    failures = []
    for raw, expected in CASES:
        got = parse_size_sqft(raw)
        ok = got == expected
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] {raw!r} -> {got} (expected {expected})")
        if not ok:
            failures.append((raw, expected, got))
    print("---")
    if failures:
        print(f"FAIL: {len(failures)} of {len(CASES)} cases failed")
        sys.exit(1)
    print(f"PASS: {len(CASES)} cases")


main()
