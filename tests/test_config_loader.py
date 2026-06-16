"""
Unit tests for homehunt.config_loader.

Run inside container:
  PATH="/opt/homebrew/bin:$PATH" docker exec -e PYTHONPATH=/app rental-engine \
    python3 tests/test_config_loader.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from homehunt.config_loader import load_search_profiles

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class TestLoadSearchProfiles(unittest.TestCase):

    def test_returns_flat_list_of_dicts(self):
        profiles = load_search_profiles(os.path.join(FIXTURES, "sample-search.yaml"))
        self.assertIsInstance(profiles, list)
        self.assertEqual(len(profiles), 4)
        for p in profiles:
            self.assertIsInstance(p, dict)

    def test_each_entry_has_required_keys(self):
        profiles = load_search_profiles(os.path.join(FIXTURES, "sample-search.yaml"))
        required = {"location", "region_id", "min_price", "max_price",
                    "min_bedrooms", "max_bedrooms", "radius", "max_results",
                    "page_delay", "scrape_delay"}
        for p in profiles:
            missing = required - set(p.keys())
            self.assertEqual(missing, set(), f"Entry missing keys: {missing}")

    def test_defaults_applied(self):
        profiles = load_search_profiles(os.path.join(FIXTURES, "sample-search.yaml"))
        for p in profiles:
            self.assertEqual(p["min_price"], 1600)
            self.assertEqual(p["max_price"], 2800)
            self.assertEqual(p["min_bedrooms"], 1)
            self.assertEqual(p["max_bedrooms"], 2)
            self.assertAlmostEqual(p["radius"], 1.0)

    def test_profile_override_applied(self):
        # east_london_core overrides max_results: 30
        profiles = load_search_profiles(os.path.join(FIXTURES, "sample-search.yaml"))
        east = [p for p in profiles if p.get("profile") == "east_london_core"]
        for p in east:
            self.assertEqual(p["max_results"], 30)

    def test_default_max_results_for_non_overridden_profile(self):
        profiles = load_search_profiles(os.path.join(FIXTURES, "sample-search.yaml"))
        north = [p for p in profiles if p.get("profile") == "north_london"]
        for p in north:
            self.assertEqual(p["max_results"], 40)

    def test_region_id_present_in_output(self):
        profiles = load_search_profiles(os.path.join(FIXTURES, "sample-search.yaml"))
        ids = {p["region_id"] for p in profiles}
        self.assertIn("REGION^87490", ids)
        self.assertIn("REGION^85331", ids)
        self.assertIn("REGION^87498", ids)

    def test_location_name_present_in_output(self):
        profiles = load_search_profiles(os.path.join(FIXTURES, "sample-search.yaml"))
        names = {p["location"] for p in profiles}
        self.assertIn("Shoreditch, London", names)
        self.assertIn("Canonbury, London", names)

    def test_raises_value_error_on_missing_region_id(self):
        with self.assertRaises(ValueError) as ctx:
            load_search_profiles(os.path.join(FIXTURES, "missing-region-id.yaml"))
        self.assertIn("Hoxton, London", str(ctx.exception))

    def test_raises_value_error_names_the_bad_location(self):
        try:
            load_search_profiles(os.path.join(FIXTURES, "missing-region-id.yaml"))
            self.fail("Expected ValueError")
        except ValueError as exc:
            self.assertIn("region_id", str(exc).lower())

    def test_file_not_found_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_search_profiles("/nonexistent/path/search.yaml")


if __name__ == "__main__":
    unittest.main(verbosity=2)
