"""
OpenRent Probe — Task 6: extract structured fields from a detail-page HTML.

REQUIRED (cannot score without): title, price_monthly, bedrooms, postcode_area.
OPTIONAL (log presence/absence, do not assert): bathrooms, furnishing, epc_rating,
    let_agreed, photos, description, available_from, min_tenancy, garden, parking,
    fireplace, bills_included, preferences, transport_nearby.

Note: this is the 2026-05-23 template. The Kwan-Li 2024 spider's table.table-striped /
h1.property-title / h3.price-title selectors are gone; the page now uses h2-anchored
section blocks ("Price & Bills", "Tenant Preference", "Availability", "Features").
"""

import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXP = PROJECT_ROOT / "experiments" / "openrent-probe"

spec = importlib.util.spec_from_file_location("openrent_probe_extract", EXP / "probe" / "extract.py")
em = importlib.util.module_from_spec(spec); spec.loader.exec_module(em)

REQUIRED = ["title", "price_monthly", "bedrooms", "postcode_area"]
OPTIONAL = [
    "bathrooms", "furnishing", "epc_rating", "let_agreed", "photos",
    "description", "available_from", "min_tenancy", "garden", "parking",
    "fireplace", "bills_included", "preferences", "transport_nearby",
    "deposit",
]


def main() -> int:
    html_files = sorted((EXP / "fixtures").glob("detail-*.html"))
    assert html_files, "no detail-*.html fixtures; run Task 5 first"
    html = html_files[0].read_text()
    pid = int(html_files[0].stem.split("-")[1])
    parsed = em.extract_detail(html)
    (EXP / "fixtures" / f"detail-{pid}.parsed.json").write_text(json.dumps(parsed, indent=2, default=str))
    for f in REQUIRED:
        v = parsed.get(f)
        assert v not in (None, ""), f"REQUIRED field '{f}' missing/empty (got {v!r})"
    present = [f for f in OPTIONAL if parsed.get(f) not in (None, "", [])]
    missing = [f for f in OPTIONAL if f not in present]
    print(f"PASS - id={pid}, required fields present")
    print(f"  title: {parsed['title']!r}")
    print(f"  price_monthly: {parsed['price_monthly']}")
    print(f"  bedrooms: {parsed['bedrooms']}, postcode_area: {parsed['postcode_area']}")
    print(f"  optional present ({len(present)}/{len(OPTIONAL)}): {present}")
    print(f"  optional missing: {missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
