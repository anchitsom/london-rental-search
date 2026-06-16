"""
Bug fix - openrent_mapper drops epc_rating.

The extractor returns epc_rating ('C', 'D', etc.) but the mapper does not pass
it through to PropertyListing, so it never reaches the DB. Listing.epc_rating
exists on the schema; this is a pure plumbing bug.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from homehunt.scrapers.openrent_extract import extract_detail
from homehunt.scrapers.openrent_mapper import to_property_listing


def main() -> int:
    fx = sorted((PROJECT_ROOT / "experiments" / "openrent-probe" / "fixtures").glob("detail-*.html"))[0]
    pid = int(fx.stem.split("-")[1])
    parsed = extract_detail(fx.read_text())
    # Sanity: extractor must have epc_rating to begin with
    assert parsed.get("epc_rating") in ("A", "B", "C", "D", "E", "F", "G"), (
        f"extractor missing epc_rating on {fx.name}: got {parsed.get('epc_rating')!r}"
    )
    expected_epc = parsed["epc_rating"]

    listing = to_property_listing(
        parsed=parsed,
        property_id=pid,
        search_lat=51.5,
        search_lng=-0.1,
    )
    assert listing.epc_rating == expected_epc, (
        f"mapper dropped epc_rating: extractor said {expected_epc!r}, "
        f"PropertyListing has {listing.epc_rating!r}"
    )
    print(f"PASS - mapper preserves epc_rating={listing.epc_rating}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
