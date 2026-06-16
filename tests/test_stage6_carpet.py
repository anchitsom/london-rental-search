"""
Stage 6: Carpet detection via llava-phi3.

Fetches images directly from known Rightmove property pages (no DB required).
Depends only on: DirectHTTPScraper (stage 3b verified) + Ollama at OLLAMA_URL.

Run inside container: python tests/test_stage6_carpet.py
"""

import asyncio
import sys

sys.path.insert(0, "/app")

# (url, expected_carpet)
TEST_URLS = [
    ("https://www.rightmove.co.uk/properties/174236120", False),  # expected: no carpet
    ("https://www.rightmove.co.uk/properties/174086936", True),   # expected: carpet
    ("https://www.rightmove.co.uk/properties/173377823", False),  # expected: no carpet
    ("https://www.rightmove.co.uk/properties/142690991", True),   # expected: carpet
]


async def main():
    import httpx
    from homehunt.carpet import detect_carpet, OLLAMA_URL, VISION_MODEL
    from homehunt.scrapers.direct_http import DirectHTTPScraper

    print(f"Ollama: {OLLAMA_URL}")
    print(f"Model:  {VISION_MODEL}\n")

    # Pre-flight: confirm model exists and vision support
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            tags = await client.get(f"{OLLAMA_URL}/api/tags")
            models = [m["name"] for m in tags.json().get("models", [])]
            match = [m for m in models if VISION_MODEL in m]
            print(f"Model present: {match or 'NOT FOUND'}")
            info = await client.post(f"{OLLAMA_URL}/api/show", json={"name": VISION_MODEL})
            families = info.json().get("details", {}).get("families", [])
            print(f"Model families: {families}\n")
        except Exception as e:
            print(f"Pre-flight failed: {e}\n")

    passed = 0
    failed = 0

    async with DirectHTTPScraper() as scraper:
        for url, expected in TEST_URLS:
            prop_id = url.split("/")[-1]
            print(f"--- {prop_id} (expected: {'carpet' if expected else 'no carpet'}) ---")

            result = await scraper.scrape_property(url)
            images = result.data.get("images", []) if result.data else []
            print(f"  Images found: {len(images)}")

            if not images:
                print("  SKIP — no images extracted\n")
                continue

            carpet, confidence = await detect_carpet(images)

            if carpet is True:
                verdict = f"CARPET DETECTED ({int(confidence * 100)}% confidence)"
            elif carpet is False:
                verdict = f"NO CARPET ({int(confidence * 100)}% confidence)"
            else:
                verdict = "UNKNOWN"

            correct = carpet == expected
            result_str = "PASS" if correct else "FAIL"
            if carpet is None:
                result_str = "UNKNOWN"

            passed += correct
            failed += (not correct and carpet is not None)

            print(f"  Result:   {verdict}")
            print(f"  [{result_str}]\n")

    print(f"=== {passed}/{passed+failed} correct ===")
    print("Stage 6: DONE")


asyncio.run(main())
