"""
Resolve missing Rightmove region IDs in london-search.yaml.

Reads the yaml, finds locations with empty or placeholder region_id,
calls the Rightmove typeahead, and writes resolved IDs back in place.

Safe to re-run: locations with a valid REGION^... ID are skipped.

Usage (inside container):
  python3 scripts/resolve_region_ids.py [path/to/yaml]

Default yaml path: /app/london-search.yaml
"""

import asyncio
import os
import sys
from typing import Optional

import httpx
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import the private resolver directly so the unit test can patch it.
# The public resolve_location_id wraps this but needs its own client;
# patching _resolve_location_name is cleaner in tests.
from homehunt.scrapers.rightmove_api import (
    _HEADERS,
    _resolve_location,
)

DEFAULT_YAML = "/app/london-search.yaml"

_PLACEHOLDER_VALUES = {"", "TODO", "todo", "FIXME", "fixme", None}


def _needs_resolution(region_id) -> bool:
    if region_id in _PLACEHOLDER_VALUES:
        return True
    if not isinstance(region_id, str):
        return True
    if not region_id.startswith("REGION^") or len(region_id) <= 7:
        return True
    return False


async def _resolve_location_name(
    client: httpx.AsyncClient, location: str
) -> Optional[str]:
    """Thin wrapper so unit tests can patch at this symbol."""
    return await _resolve_location(client, location)


async def resolve_yaml_file(yaml_path: str) -> None:
    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    profiles = data.get("profiles", [])
    any_change = False

    async with httpx.AsyncClient(
        headers=_HEADERS,
        follow_redirects=True,
        timeout=10.0,
    ) as client:
        for profile in profiles:
            profile_name = profile.get("name", "")
            for loc in profile.get("locations", []):
                loc_name = loc.get("name", "")
                current_id = loc.get("region_id")

                if not _needs_resolution(current_id):
                    print(f"  SKIP  {loc_name!r:40s}  already has {current_id}")
                    continue

                resolved = await _resolve_location_name(client, loc_name)
                if resolved:
                    print(f"  OK    {loc_name!r:40s}  -> {resolved}  (profile: {profile_name})")
                    loc["region_id"] = resolved
                    any_change = True
                else:
                    print(
                        f"  FAIL  {loc_name!r:40s}  could not resolve -- leaving blank "
                        f"(check Rightmove manually)"
                    )

    if any_change:
        with open(yaml_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        print(f"\nWrote updated yaml to {yaml_path}")
    else:
        print("\nNo changes needed.")


def main():
    yaml_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_YAML
    if not os.path.exists(yaml_path):
        print(f"ERROR: yaml not found: {yaml_path}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(resolve_yaml_file(yaml_path))


if __name__ == "__main__":
    main()
