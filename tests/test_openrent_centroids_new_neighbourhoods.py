"""
Verify the four new neighbourhood centroids land in both
data/neighbourhood_centroids.json and london-search.yaml's openrent_profiles.

Project convention: plain async-style script, NOT pytest. Each test asserts
and prints PASS / FAIL.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homehunt.config_loader import load_openrent_profiles  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CENTROIDS_JSON = os.path.join(ROOT, "data", "neighbourhood_centroids.json")
YAML_PATH = os.path.join(ROOT, "london-search.yaml")

NEW_NEIGHBOURHOODS = {
    "Tottenham Hale, North London": (51.5882, -0.0594),
    "Seven Sisters, North London": (51.5828, -0.0739),
    "Blackhorse Road, East London": (51.5867, -0.0419),
    "Walthamstow, East London": (51.5828, -0.0193),
}

# Existing 30 keys, frozen so a silent drop is caught.
EXISTING_KEYS = {
    "Islington, North London", "Barnsbury, North London",
    "Highbury, North London", "Canonbury, North London",
    "Kings Cross, North London", "Holloway, North London",
    "Upper Holloway, North London", "Lower Holloway, North London",
    "Tufnell Park, North London", "Archway, London",
    "Finsbury Park, North London", "Camden Town, North West London",
    "Kentish Town, North West London", "Belsize Park, North West London",
    "Hampstead, North West London", "Primrose Hill, North West London",
    "Chalk Farm, North West London", "Fitzrovia, London",
    "Bloomsbury, Central London", "Holborn, Central London",
    "Shoreditch, East London", "Hoxton, North London",
    "Haggerston, East London", "Dalston, East London",
    "De Beauvoir Town, North London", "Stoke Newington, North London",
    "Clapton, East London", "London Fields, East London",
    "Homerton, East London", "Hackney Wick, East London",
}


def test_centroids_json_extended():
    with open(CENTROIDS_JSON) as fh:
        data = json.load(fh)

    for key in EXISTING_KEYS:
        assert key in data, f"FAIL: existing key {key!r} dropped"
    print(f"  PASS [existing_30_keys]: all present")

    for key, (lat, lng) in NEW_NEIGHBOURHOODS.items():
        assert key in data, f"FAIL: new key {key!r} missing"
        entry = data[key]
        assert abs(entry["lat"] - lat) < 1e-4, (
            f"FAIL [{key}]: lat {entry['lat']} != {lat}"
        )
        assert abs(entry["lng"] - lng) < 1e-4, (
            f"FAIL [{key}]: lng {entry['lng']} != {lng}"
        )
        print(f"  PASS [{key}]: {entry['lat']}, {entry['lng']}")


def test_openrent_profiles_extended():
    profiles = load_openrent_profiles(YAML_PATH)
    assert len(profiles) == 1, f"FAIL: expected 1 profile, got {len(profiles)}"
    p = profiles[0]
    # load_openrent_profiles normalises radius_km vs radius depending on the
    # loader version; accept either key.
    radius = p.get("radius_km", p.get("radius"))
    assert radius == 1.5, f"FAIL: radius {radius} != 1.5"
    centroids = p["centroids"]
    assert len(centroids) == 34, (
        f"FAIL: expected 34 centroids, got {len(centroids)}"
    )
    print(f"  PASS [profile_shape]: 1 profile, radius_km=1.5, 34 centroids")

    for label, (lat, lng) in NEW_NEIGHBOURHOODS.items():
        match = any(
            abs(c[0] - lat) < 1e-4 and abs(c[1] - lng) < 1e-4
            for c in centroids
        )
        assert match, f"FAIL [{label}]: centroid ({lat}, {lng}) not in yaml"
        print(f"  PASS [{label}]: centroid present")


if __name__ == "__main__":
    tests = [
        ("centroids_json_extended", test_centroids_json_extended),
        ("openrent_profiles_extended", test_openrent_profiles_extended),
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
