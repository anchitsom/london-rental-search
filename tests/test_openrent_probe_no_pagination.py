"""
OpenRent Probe - Task 7: a single search GET returns the full matching set in one shot.

Approach: run a tight filter (small NUMBEROFPROPERTIES) and a broad filter (large). Confirm
that on the tight filter, len(PROPERTYIDS) == NUMBEROFPROPERTIES exactly. On broad, note
whether they still match (no cap) or diverge (a cap exists at the inline-array level).
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXP = PROJECT_ROOT / "experiments" / "openrent-probe"

spec_f = importlib.util.spec_from_file_location("of", EXP / "probe" / "fetch.py")
of = importlib.util.module_from_spec(spec_f); spec_f.loader.exec_module(of)
spec_e = importlib.util.spec_from_file_location("oe", EXP / "probe" / "extract.py")
oe = importlib.util.module_from_spec(spec_e); spec_e.loader.exec_module(oe)

TIGHT = "https://www.openrent.co.uk/properties-to-rent/london?term=London&prices_min=2500&prices_max=2700&bedrooms_min=2&bedrooms_max=2"
BROAD = "https://www.openrent.co.uk/properties-to-rent/london?term=London&prices_min=1500&prices_max=2800&bedrooms_min=1&bedrooms_max=2"


async def main() -> int:
    results = {}
    for label, url in [("tight", TIGHT), ("broad", BROAD)]:
        status, body = await of.fetch_search(url)
        assert status == 200, f"{label}: expected 200, got {status}"
        ext = oe.extract_search(body)
        results[label] = {
            "noop": ext["number_of_properties"],
            "id_count": len(ext["property_ids"]),
        }
        print(f"{label}: NUMBEROFPROPERTIES={ext['number_of_properties']}, len(PROPERTYIDS)={len(ext['property_ids'])}")
        await asyncio.sleep(2)
    t = results["tight"]
    b = results["broad"]
    # Primary assertion: NUMBEROFPROPERTIES always matches len(PROPERTYIDS) — no inline-array cap.
    assert t["noop"] == t["id_count"], f"tight: noop={t['noop']} != id_count={t['id_count']} (cap?)"
    assert b["noop"] == b["id_count"], f"broad: noop={b['noop']} != id_count={b['id_count']} (cap?)"
    print("PASS - no inline-array cap; one GET always returns NUMBEROFPROPERTIES items.")
    # Secondary observation: price/bedroom URL params do not narrow server-side.
    # Both filters return nearly the same count (~6113 vs ~6116). Filtering is client-side.
    delta = abs(t["noop"] - b["noop"])
    if delta < 50:
        print(f"NOTE - price/bed URL params have no observable server-side effect (delta={delta}).")
        print("       OpenRent loads the full term=London universe and filters client-side in JS.")
        print("       Integration MUST hard-filter price/beds after detail-fetch, not via URL params.")
    else:
        print(f"NOTE - URL params do narrow: tight={t['noop']}, broad={b['noop']}, delta={delta}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
