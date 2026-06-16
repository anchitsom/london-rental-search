"""
OpenRent Probe - Task 9: search-array lat/long vs postcodes.io postcode centroid.

For each cached detail fixture, look up the postcode-area in postcodes.io and compare
the search-page lat/long for that id to the postcode centroid. Median delta < 500m
suggests building / street-grade geocoding; > 500m suggests postcode-centroid only.

Note: OpenRent's title carries only the postcode AREA (e.g. WC2R) not the full postcode
(WC2R 1AA). postcodes.io exposes /outcodes/{outcode} for centroid lookup of areas.
"""

import asyncio
import importlib.util
import json
import math
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXP = PROJECT_ROOT / "experiments" / "openrent-probe"


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))


async def main() -> int:
    search_parsed = json.loads((EXP / "fixtures" / "search-page-0.parsed.json").read_text())
    id_to_coord = dict(zip(
        search_parsed["first_ten_ids"],
        zip(search_parsed["first_ten_lats"], search_parsed["first_ten_lngs"]),
    ))
    detail_jsons = sorted((EXP / "fixtures").glob("detail-*.parsed.json"))
    assert detail_jsons, "no parsed detail fixtures; run Task 8 first"

    deltas = []
    async with httpx.AsyncClient(timeout=15) as client:
        for f in detail_jsons[:5]:
            pid = int(f.stem.split("-")[1].split(".")[0])
            if pid not in id_to_coord:
                print(f"SKIP id={pid}: not in first_ten")
                continue
            parsed = json.loads(f.read_text())
            outcode = parsed.get("postcode_area")
            if not outcode:
                print(f"SKIP id={pid}: no postcode_area")
                continue
            r = await client.get(f"https://api.postcodes.io/outcodes/{outcode}")
            if r.status_code != 200:
                print(f"SKIP id={pid}: postcodes.io {r.status_code} for outcode {outcode}")
                continue
            pc_data = r.json()["result"]
            or_lat, or_lng = id_to_coord[pid]
            dist = haversine_m(or_lat, or_lng, pc_data["latitude"], pc_data["longitude"])
            deltas.append((pid, outcode, dist))
            print(f"  id={pid} outcode={outcode} delta={dist:.0f}m")

    assert deltas, "no comparisons possible"
    sorted_d = sorted(d[2] for d in deltas)
    median = sorted_d[len(sorted_d)//2]
    mean = sum(sorted_d) / len(sorted_d)
    print(f"Median delta: {median:.0f}m (mean {mean:.0f}m) over {len(deltas)} listings")
    print("(Outcode centroid is a coarse reference - building grade < 500m, street grade < 1000m,")
    print(" outcode-coarse > 1500m. Note this measures search-array coords vs outcode centroid,")
    print(" not vs full postcode centroid; full postcode would be the more sensitive test.)")
    if median < 1000:
        print(f"PASS - median {median:.0f}m suggests building/street-grade coords")
    else:
        print(f"WARN - median {median:.0f}m is outcode-coarse; may not be enough for EPC binding")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
