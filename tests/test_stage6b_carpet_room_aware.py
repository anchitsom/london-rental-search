"""
Stage 6b: Room-aware two-signal carpet detection tests.

Unit tests run with mocked vision responses (no Ollama required).
Integration test requires OLLAMA_URL in the environment.

Unit run:
    docker exec -e PYTHONPATH=/app rental-engine python3 tests/test_stage6b_carpet_room_aware.py

Integration run:
    docker exec -e PYTHONPATH=/app -e OLLAMA_URL=http://host.docker.internal:11434 \
        -e VISION_MODEL=llava-phi3 rental-engine \
        python3 tests/test_stage6b_carpet_room_aware.py
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, "/app")

from homehunt.carpet import detect_carpet_two_signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_responses(*pairs):
    """
    Build a list of mocked _query_vision_model return values from
    (room_type, carpet) pairs. confidence is fixed at 0.8 for determinism.
    """
    out = []
    for room_type, carpet in pairs:
        out.append({"room_type": room_type, "carpet": carpet, "confidence": 0.8})
    return out


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestDetectCarpetTwoSignal(unittest.TestCase):

    def _patch_and_run(self, responses, photo_urls=None):
        """Run detect_carpet_two_signal with _query_vision_model returning scripted values."""
        if photo_urls is None:
            photo_urls = [f"http://example.com/img{i}.jpg" for i in range(len(responses))]

        # _fetch_image_b64 returns a dummy b64 string so the pipeline continues.
        async def fake_fetch(client, url):
            return "ZmFrZQ=="  # base64("fake")

        response_iter = iter(responses)

        async def fake_query(client, image_b64):
            try:
                return next(response_iter)
            except StopIteration:
                return None

        with patch("homehunt.carpet._fetch_image_b64", fake_fetch), \
             patch("homehunt.carpet._query_vision_model_room_aware", fake_query):
            return _run(detect_carpet_two_signal(photo_urls))

    # --- case 1: 4 bedroom photos, all carpeted
    def test_all_bedroom_all_carpet(self):
        responses = _make_responses(
            ("bedroom", True),
            ("bedroom", True),
            ("bedroom", True),
            ("bedroom", True),
        )
        result = self._patch_and_run(responses)
        self.assertTrue(result["carpet_in_bedroom"])
        self.assertFalse(result["carpet_other_areas"])
        self.assertTrue(result["carpet_detected"])

    # --- case 2: 1 bedroom carpet, 3 non-bedroom hardwood
    def test_one_bedroom_carpet_three_other_hardwood(self):
        responses = _make_responses(
            ("bedroom", True),
            ("living-room", False),
            ("kitchen", False),
            ("hallway", False),
        )
        result = self._patch_and_run(responses)
        self.assertTrue(result["carpet_in_bedroom"])
        self.assertFalse(result["carpet_other_areas"])
        self.assertTrue(result["carpet_detected"])

    # --- case 3: 1 bedroom hardwood, 3 living-room carpet
    def test_one_bedroom_hardwood_three_living_carpet(self):
        responses = _make_responses(
            ("bedroom", False),
            ("living-room", True),
            ("living-room", True),
            ("living-room", True),
        )
        result = self._patch_and_run(responses)
        self.assertFalse(result["carpet_in_bedroom"])
        self.assertTrue(result["carpet_other_areas"])
        self.assertTrue(result["carpet_detected"])

    # --- case 4: all hardwood, no carpet anywhere
    def test_all_hardwood(self):
        responses = _make_responses(
            ("bedroom", False),
            ("living-room", False),
            ("kitchen", False),
            ("bathroom", False),
        )
        result = self._patch_and_run(responses)
        self.assertFalse(result["carpet_in_bedroom"])
        self.assertFalse(result["carpet_other_areas"])
        self.assertFalse(result["carpet_detected"])

    # --- case 5: 1 bedroom carpet + 1 living-room carpet + 2 hardwood
    def test_bedroom_and_other_both_carpeted(self):
        responses = _make_responses(
            ("bedroom", True),
            ("living-room", True),
            ("kitchen", False),
            ("hallway", False),
        )
        result = self._patch_and_run(responses)
        self.assertTrue(result["carpet_in_bedroom"])
        self.assertTrue(result["carpet_other_areas"])
        self.assertTrue(result["carpet_detected"])

    # --- case 6: 0 photos input
    def test_empty_photo_list(self):
        result = _run(detect_carpet_two_signal([]))
        self.assertIsNone(result["carpet_in_bedroom"])
        self.assertIsNone(result["carpet_other_areas"])
        self.assertIsNone(result["carpet_detected"])
        self.assertEqual(result["carpet_confidence"], 0.0)

    # --- case 7: all photos fail to parse (None responses)
    def test_all_photos_fail_to_parse(self):
        async def fake_fetch(client, url):
            return "ZmFrZQ=="

        async def fake_query(client, image_b64):
            return None  # every call fails

        photo_urls = ["http://example.com/img0.jpg"] * 4
        with patch("homehunt.carpet._fetch_image_b64", fake_fetch), \
             patch("homehunt.carpet._query_vision_model", fake_query):
            result = _run(detect_carpet_two_signal(photo_urls))

        self.assertIsNone(result["carpet_in_bedroom"])
        self.assertIsNone(result["carpet_other_areas"])
        self.assertIsNone(result["carpet_detected"])
        self.assertEqual(result["carpet_confidence"], 0.0)


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

OLLAMA_URL = os.environ.get("OLLAMA_URL", "")
INTEGRATION_PROPERTY_ID = "174236120"
INTEGRATION_URL = f"https://www.rightmove.co.uk/properties/{INTEGRATION_PROPERTY_ID}"


class TestCarpetTwoSignalIntegration(unittest.TestCase):

    @unittest.skipUnless(OLLAMA_URL, "OLLAMA_URL not set, skipping integration test")
    def test_no_carpet_listing(self):
        """
        Property 174236120 has been observed to show hardwood/tile floors.
        Expect carpet_in_bedroom=False after a live model run.
        """
        import httpx

        async def run():
            from homehunt.scrapers.direct_http import DirectHTTPScraper

            async with DirectHTTPScraper() as scraper:
                scrape_result = await scraper.scrape_property(INTEGRATION_URL)
            images = (
                scrape_result.data.get("images", []) if scrape_result.data else []
            )
            print(f"\n  Integration: {len(images)} images for {INTEGRATION_PROPERTY_ID}")
            if not images:
                self.skipTest("No images extracted from live listing")
            result = await detect_carpet_two_signal(images)
            print(f"  Result: {result}")
            return result

        result = _run(run())
        self.assertFalse(
            result["carpet_in_bedroom"],
            f"Expected carpet_in_bedroom=False for listing {INTEGRATION_PROPERTY_ID}, got {result}",
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Unit tests always run
    suite.addTests(loader.loadTestsFromTestCase(TestDetectCarpetTwoSignal))

    # Integration test runs when OLLAMA_URL is set
    suite.addTests(loader.loadTestsFromTestCase(TestCarpetTwoSignalIntegration))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
