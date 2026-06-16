"""
Unit tests for scripts/resolve_region_ids.py.

Run inside container:
  PATH="/opt/homebrew/bin:$PATH" docker exec -e PYTHONPATH=/app rental-engine \
    python3 tests/test_region_resolver.py
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _copy_fixture(name: str, dest_dir: str) -> str:
    src = os.path.join(FIXTURES, name)
    dst = os.path.join(dest_dir, name)
    shutil.copy2(src, dst)
    return dst


class TestRegionResolver(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _resolve(self, yaml_path: str, mock_id: str = "REGION^99999") -> None:
        """
        Run resolve_yaml_file with the typeahead mocked to return mock_id
        for any unresolved location.
        """
        import asyncio
        from scripts.resolve_region_ids import resolve_yaml_file

        async def fake_resolve(client, location):
            return mock_id

        with patch(
            "scripts.resolve_region_ids._resolve_location_name",
            new=fake_resolve,
        ):
            asyncio.run(resolve_yaml_file(yaml_path))

    def test_unresolved_location_gets_region_id(self):
        yaml_path = _copy_fixture("resolver-test.yaml", self.tmpdir)
        self._resolve(yaml_path, mock_id="REGION^99999")

        import yaml
        with open(yaml_path) as f:
            data = yaml.safe_load(f)

        locations = data["profiles"][0]["locations"]
        hoxton = next(loc for loc in locations if loc["name"] == "Hoxton, London")
        self.assertEqual(hoxton["region_id"], "REGION^99999")

    def test_already_resolved_location_is_not_overwritten(self):
        yaml_path = _copy_fixture("resolver-test.yaml", self.tmpdir)
        self._resolve(yaml_path, mock_id="REGION^00000")

        import yaml
        with open(yaml_path) as f:
            data = yaml.safe_load(f)

        locations = data["profiles"][0]["locations"]
        shoreditch = next(loc for loc in locations if loc["name"] == "Shoreditch, London")
        # Must keep the original REGION^87490, not be overwritten
        self.assertEqual(shoreditch["region_id"], "REGION^87490")

    def test_round_trip_preserves_all_keys(self):
        yaml_path = _copy_fixture("resolver-test.yaml", self.tmpdir)

        import yaml
        with open(yaml_path) as f:
            before = yaml.safe_load(f)

        self._resolve(yaml_path, mock_id="REGION^99999")

        with open(yaml_path) as f:
            after = yaml.safe_load(f)

        # Top-level keys must be identical
        self.assertEqual(set(before.keys()), set(after.keys()))
        # Profile names preserved
        before_names = [p["name"] for p in before["profiles"]]
        after_names = [p["name"] for p in after["profiles"]]
        self.assertEqual(before_names, after_names)

    def test_idempotent_on_second_run(self):
        yaml_path = _copy_fixture("resolver-test.yaml", self.tmpdir)
        self._resolve(yaml_path, mock_id="REGION^99999")
        self._resolve(yaml_path, mock_id="REGION^11111")

        import yaml
        with open(yaml_path) as f:
            data = yaml.safe_load(f)

        locations = data["profiles"][0]["locations"]
        hoxton = next(loc for loc in locations if loc["name"] == "Hoxton, London")
        # Second run must not overwrite because first run already resolved it
        self.assertEqual(hoxton["region_id"], "REGION^99999")


if __name__ == "__main__":
    unittest.main(verbosity=2)
