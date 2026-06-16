"""
Verify london-search.yaml carries the haringey + waltham_forest profiles.

Project convention: plain async-style script, NOT pytest. Each test asserts and
prints PASS / FAIL; run via .venv/bin/python tests/test_london_search_haringey_walthamforest.py.

Live-network criterion (criterion 4) skips cleanly with SKIPPED [network] if
the typeahead/search endpoints cannot be reached.
"""

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homehunt.config_loader import load_search_profiles  # noqa: E402

YAML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "london-search.yaml",
)

ID_PATTERN = re.compile(r"^(?:REGION|STATION)\^\d+$")

# Snapshot of the three pre-existing profiles. If anything here changes the
# test must be updated deliberately, not silently regressed.
EXISTING_PROFILES = {
    "islington_borough": 11,
    "camden_borough": 9,
    "hackney_borough": 10,
}

NEW_PROFILES = {
    "haringey_borough": [
        "Tottenham Hale, North London",
        "Seven Sisters, North London",
    ],
    "waltham_forest_borough": [
        "Blackhorse Road, East London",
        "Walthamstow, East London",
    ],
}


def _profiles_by_name():
    # load_search_profiles returns a flat list of one entry per location, with
    # 'profile' and 'location' keys. Re-group by profile name.
    grouped: dict[str, dict] = {}
    for entry in load_search_profiles(YAML_PATH):
        name = entry["profile"]
        grouped.setdefault(name, {"name": name, "locations": []})
        grouped[name]["locations"].append(
            {"name": entry["location"], "region_id": entry["region_id"]}
        )
    return grouped


def test_new_profiles_present_with_valid_ids():
    profiles = _profiles_by_name()
    for name, expected_locations in NEW_PROFILES.items():
        assert name in profiles, f"FAIL: profile {name!r} missing"
        locs = profiles[name]["locations"]
        got_names = [loc["name"] for loc in locs]
        assert got_names == expected_locations, (
            f"FAIL [{name}]: expected {expected_locations}, got {got_names}"
        )
        for loc in locs:
            rid = loc["region_id"]
            assert ID_PATTERN.match(rid), (
                f"FAIL [{name}/{loc['name']}]: bad region_id {rid!r}"
            )
        print(f"  PASS [{name}]: {len(locs)} locations, all IDs well-formed")


def test_existing_profiles_unchanged():
    profiles = _profiles_by_name()
    for name, expected_count in EXISTING_PROFILES.items():
        assert name in profiles, f"FAIL: profile {name!r} missing"
        got = len(profiles[name]["locations"])
        assert got == expected_count, (
            f"FAIL [{name}]: expected {expected_count} locations, got {got}"
        )
        print(f"  PASS [{name}]: {got} locations (unchanged)")


def test_live_verify_new_ids():
    try:
        from scripts.discover_borough_regions import _verify_region, _HEADERS  # noqa
        import httpx
    except Exception as exc:
        print(f"  SKIPPED [import]: {exc}")
        return

    profiles = _profiles_by_name()
    new_ids = []
    for name in NEW_PROFILES:
        for loc in profiles[name]["locations"]:
            new_ids.append((loc["name"], loc["region_id"]))

    async def run():
        async with httpx.AsyncClient(
            headers=_HEADERS, follow_redirects=True, timeout=20.0
        ) as client:
            for display, rid in new_ids:
                try:
                    count = await _verify_region(client, rid)
                except Exception as exc:
                    print(f"  SKIPPED [network] {display}: {exc}")
                    return
                assert count > 0, f"FAIL [{display}]: 0 results for {rid}"
                print(f"  PASS [{display}]: {rid} -> {count} results")

    asyncio.run(run())


if __name__ == "__main__":
    tests = [
        ("new_profiles_present_with_valid_ids", test_new_profiles_present_with_valid_ids),
        ("existing_profiles_unchanged", test_existing_profiles_unchanged),
        ("live_verify_new_ids", test_live_verify_new_ids),
    ]
    failures = []
    for name, fn in tests:
        print(f"\n=== {name} ===")
        try:
            fn()
        except AssertionError as exc:
            print(f"  {exc}")
            failures.append(name)
        except Exception as exc:
            import traceback
            print(f"  ERROR in {name}: {exc}")
            traceback.print_exc()
            failures.append(name)
    print("\n" + "=" * 50)
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    print(f"ALL {len(tests)} test groups passed.")
