"""
Stage 3b: Verify image extraction from Rightmove property pages.
Confirms window.PAGE_MODEL is parsed and property-photo URLs returned.

Run inside container: python tests/test_stage3b_images.py
"""

import asyncio
import sys

sys.path.insert(0, "/app")

TEST_URLS = [
    "https://www.rightmove.co.uk/properties/169586816",
    "https://www.rightmove.co.uk/properties/174236120",
]


async def main():
    from homehunt.scrapers.direct_http import DirectHTTPScraper

    async with DirectHTTPScraper() as scraper:
        for url in TEST_URLS:
            print(f"\n--- {url.split('/')[-1]} ---")
            result = await scraper.scrape_property(url)
            images = result.data.get("images", []) if result.data else []
            print(f"  images extracted: {len(images)}")
            for img in images:
                print(f"    {img}")

    print("\nStage 3b: DONE")


asyncio.run(main())
