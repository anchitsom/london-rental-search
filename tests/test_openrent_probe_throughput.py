"""
OpenRent Probe - Task 8: 5 detail fetches at 1s delay, all 200 + populate parsed fixtures.

Pass: all 5 == 200 status, all 5 produce a non-empty parsed-detail JSON.
Soft fail: any 429 - record interval where it triggers, recommend slower interval for production.
"""

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXP = PROJECT_ROOT / "experiments" / "openrent-probe"

spec_f = importlib.util.spec_from_file_location("of", EXP / "probe" / "fetch.py")
of = importlib.util.module_from_spec(spec_f); spec_f.loader.exec_module(of)
spec_e = importlib.util.spec_from_file_location("oe", EXP / "probe" / "extract.py")
oe = importlib.util.module_from_spec(spec_e); spec_e.loader.exec_module(oe)


async def main() -> int:
    parsed = json.loads((EXP / "fixtures" / "search-page-0.parsed.json").read_text())
    ids = parsed["first_ten_ids"][:5]
    print(f"Will fetch ids={ids} with 1s spacing")
    results = []
    for i, pid in enumerate(ids):
        t0 = time.monotonic()
        status, body = await of.fetch_detail(pid)
        dt = time.monotonic() - t0
        if status == 200:
            (EXP / "fixtures" / f"detail-{pid}.html").write_text(body)
            parsed_detail = oe.extract_detail(body)
            (EXP / "fixtures" / f"detail-{pid}.parsed.json").write_text(json.dumps(parsed_detail, indent=2, default=str))
        results.append((pid, status, dt, len(body)))
        print(f"  [{i+1}/{len(ids)}] id={pid} status={status} dt={dt:.2f}s body={len(body)}b")
        if i < len(ids) - 1:
            await asyncio.sleep(1.0)
    bad = [r for r in results if r[1] != 200]
    total_dt = sum(r[2] for r in results)
    print(f"Successes: {len(results)-len(bad)}/{len(results)}, mean fetch {total_dt/len(results):.2f}s, total {total_dt:.2f}s")
    assert not bad, f"non-200 responses at 1s spacing: {bad}"
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
