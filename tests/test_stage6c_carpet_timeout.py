"""
Stage 6c: prove that detect_carpet_two_signal returns within a bounded time
even when the underlying image-fetch hangs forever.

This is the regression test for the 2026-05-02 production hang where
PID 25761 sat for over 3 hours holding 8 image-CDN sockets, because the
inner asyncio.gather had no outer wall-clock guard.

The test monkeypatches _fetch_image_b64 to sleep for 600 seconds.
With the bug, the test hangs and the outer wait_for fires at 30s,
raising TimeoutError.
With the fix, detect_carpet_two_signal returns the empty-result dict
within ~25s.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from homehunt import carpet


async def _hang_forever(client, url):
    await asyncio.sleep(600)
    return None


async def main():
    carpet._fetch_image_b64 = _hang_forever  # noqa: SLF001

    urls = [
        "https://example.com/img1.jpg",
        "https://example.com/img2.jpg",
        "https://example.com/img3.jpg",
        "https://example.com/img4.jpg",
    ]

    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            carpet.detect_carpet_two_signal(urls),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - started
        print(f"FAIL: detect_carpet_two_signal hung past {elapsed:.1f}s, outer guard fired")
        print("This is the bug. The inner gather has no per-fetch wait_for.")
        sys.exit(1)

    elapsed = time.monotonic() - started
    assert result["carpet_in_bedroom"] is None, f"expected None, got {result}"
    assert result["carpet_other_areas"] is None
    assert result["carpet_confidence"] == 0.0
    print(f"PASS: returned in {elapsed:.1f}s with empty result on all-fetches-hung")


asyncio.run(main())
