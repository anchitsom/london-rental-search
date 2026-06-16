"""
OpenRent Probe — Task 3: regex-extract PROPERTYIDS, NUMBEROFPROPERTIES, lat/long arrays from the search HTML.

Pass: all four extracted, len(ids) == len(lats) == len(lngs), NUMBEROFPROPERTIES is a positive int.
Fail: missing var or length mismatch (parallel arrays must align by index).
"""

import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXP = PROJECT_ROOT / "experiments" / "openrent-probe"

spec = importlib.util.spec_from_file_location("openrent_probe_extract", EXP / "probe" / "extract.py")
em = importlib.util.module_from_spec(spec); spec.loader.exec_module(em)


def main() -> int:
    html = (EXP / "fixtures" / "search-page-0.html").read_text()
    res = em.extract_search(html)
    assert res["property_ids"], "PROPERTYIDS empty or missing"
    assert res["number_of_properties"] > 0, "NUMBEROFPROPERTIES not a positive int"
    assert res["latitudes"] and res["longitudes"], "lat or lng arrays missing"
    n = len(res["property_ids"])
    assert len(res["latitudes"]) == n, f"lat count {len(res['latitudes'])} != id count {n}"
    assert len(res["longitudes"]) == n, f"lng count {len(res['longitudes'])} != id count {n}"
    out = EXP / "fixtures" / "search-page-0.parsed.json"
    out.write_text(json.dumps({
        "number_of_properties": res["number_of_properties"],
        "id_count": n,
        "first_ten_ids": res["property_ids"][:10],
        "first_ten_lats": res["latitudes"][:10],
        "first_ten_lngs": res["longitudes"][:10],
    }, indent=2))
    print(f"PASS — {n} ids extracted, NUMBEROFPROPERTIES={res['number_of_properties']}, lat/lng arrays aligned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
