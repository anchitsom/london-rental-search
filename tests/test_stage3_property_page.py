"""
Stage 3: Scrape a single known Rightmove property page.
Verifies DirectHTTPScraper can fetch and parse a real listing
with plain HTTP — no browser required.

Run inside container: python tests/test_stage3_property_page.py
"""

import asyncio
import sys

sys.path.insert(0, "/app")

# Real URLs from Stage 2 discovery run (Canonbury, 2026-04-09)
TEST_URLS = [
    "https://www.rightmove.co.uk/properties/169586816",
    "https://www.rightmove.co.uk/properties/174236120",
    "https://www.rightmove.co.uk/properties/174235406",
]

SKIP_FIELDS = {"raw_content", "description", "features", "images"}


async def main():
    from homehunt.scrapers.direct_http import DirectHTTPScraper

    async with DirectHTTPScraper() as scraper:
        for url in TEST_URLS:
            print(f"\n--- {url} ---")
            result = await scraper.scrape_property(url)
            print(f"  success:     {result.success}")
            print(f"  error:       {result.error}")
            print(f"  content_len: {result.content_length}")

            if result.data:
                for k, v in result.data.items():
                    if k not in SKIP_FIELDS:
                        print(f"  {k:20} {str(v)[:100]}")
            else:
                print("  No data extracted")

    print("\nStage 3: DONE")


asyncio.run(main())
