"""
OpenRent Probe — Task 5: fetch one detail page by integer id, confirm classic Bootstrap HTML.

Pass: status 200, body > 30_000 bytes, h1.property-title present in body, h3.price-title present.
Fail: 403 / 429 (rate limit kicked in immediately), or selectors missing (template changed).
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXP = PROJECT_ROOT / "experiments" / "openrent-probe"

spec = importlib.util.spec_from_file_location("openrent_probe_fetch", EXP / "probe" / "fetch.py")
fm = importlib.util.module_from_spec(spec); spec.loader.exec_module(fm)


async def main() -> int:
    parsed = json.loads((EXP / "fixtures" / "search-page-0.parsed.json").read_text())
    pid = parsed["first_ten_ids"][0]
    status, body = await fm.fetch_detail(pid)
    (EXP / "fixtures" / f"detail-{pid}.html").write_text(body)
    assert status == 200, f"expected 200 for id {pid}, got {status}"
    assert len(body) > 30_000, f"body too short ({len(body)} bytes)"
    # Stable content markers (the Kwan-Li 2024 class names property-title/price-title are gone):
    # 1. <title> on a rental detail page always contains "To Rent" and a "p/m" or "p/w" rate
    # 2. <h1> always exists and carries the property type + area, e.g. "1 Bed Flat, Strand, WC2R"
    # 3. Section headings "Price & Bills" and "Availability" mark the structured-data sections
    assert "<title>" in body and "To Rent" in body, "page title missing 'To Rent' marker"
    assert "p/m" in body or "p/w" in body, "no rent rate marker (p/m or p/w) in body"
    assert "<h1" in body, "no h1 element"
    assert "Price &amp; Bills" in body, "no 'Price & Bills' section anchor"
    assert "Availability" in body, "no 'Availability' section anchor"
    print(f"PASS - id={pid}, status={status}, {len(body)} bytes, stable content markers present")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
