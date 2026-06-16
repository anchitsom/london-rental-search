"""
OpenRent Integration - Task 2: production extract.py matches probe behaviour
on the cached search and detail fixtures.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from homehunt.scrapers.openrent_extract import (
    extract_search,
    extract_propertybyid,
    extract_detail,
)

FIXTURE_DIR = PROJECT_ROOT / "experiments" / "openrent-probe" / "fixtures"


def main() -> int:
    html = (FIXTURE_DIR / "search-page-0.html").read_text()
    res = extract_search(html)
    assert res["number_of_properties"] > 0, "number_of_properties not populated"
    assert len(res["property_ids"]) == res["number_of_properties"], "id count mismatch"
    assert len(res["latitudes"]) == len(res["property_ids"]), "lat array mismatch"
    assert len(res["longitudes"]) == len(res["property_ids"]), "lng array mismatch"

    by_id = extract_propertybyid(html)
    assert by_id, "propertybyid empty"
    sample_pid = res["property_ids"][0]
    assert sample_pid in by_id, "first id missing from by_id"
    assert by_id[sample_pid] == 0, "first id should map to array index 0"

    detail_files = sorted(FIXTURE_DIR.glob("detail-*.html"))
    assert detail_files, "no detail fixtures; rerun probe Task 8 to seed"
    detail_html = detail_files[0].read_text()
    parsed = extract_detail(detail_html)
    for f in ("title", "price_monthly", "bedrooms", "postcode_area"):
        assert parsed.get(f) not in (None, ""), f"required '{f}' missing in detail parse"

    print(f"PASS - search ({len(res['property_ids'])} ids), by_id ({len(by_id)}), detail OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
