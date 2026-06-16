"""
Tests for homehunt.region.resolve_region.

Run: python3 tests/test_region_resolution.py

Tests use plain assertions following the project convention (plain async scripts,
not pytest).

resolve_region returns the nearest centroid name with no distance threshold.
Geographic constraint comes from the Rightmove search radius upstream.
"""

import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def assert_eq(label, got, expected):
    if got != expected:
        raise AssertionError(f"FAIL [{label}]: expected {expected!r}, got {got!r}")
    print(f"  PASS [{label}]")


def haversine_miles(lat1, lon1, lat2, lon2):
    """Return distance in miles between two lat/lng points."""
    R_miles = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R_miles * math.asin(math.sqrt(a))


def _point_at_distance(lat, lon, distance_miles, bearing_deg=0.0):
    """
    Return (lat, lon) that is exactly distance_miles from (lat, lon)
    along the given bearing (degrees, 0 = north).
    """
    R_miles = 3958.8
    bearing = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    angular = distance_miles / R_miles
    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular)
        + math.cos(lat1) * math.sin(angular) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(lat1),
        math.cos(angular) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_highbury_returns_highbury():
    """A lat/lng inside Highbury returns the Highbury centroid name."""
    from homehunt.region import resolve_region

    # Point 0.096 miles from the Highbury centroid (N5 1RA area).
    # Verified as closest centroid in the set -- no other centroid is nearer.
    result = resolve_region(51.547, -0.102)
    assert_eq("highbury_inside", result, "Highbury, North London")


def test_far_point_returns_nearest_centroid():
    """
    A lat/lng outside the centroid set still returns the nearest centroid name.
    No 'Other' fallback; geography is constrained upstream by the Rightmove
    search radius.
    """
    import json
    from homehunt.region import resolve_region

    centroids_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "neighbourhood_centroids.json",
    )
    with open(centroids_path) as f:
        centroids = json.load(f)

    # Walthamstow central: E17 7LP approx. Compute the actual nearest centroid
    # so the assertion stays correct if the centroid set changes.
    test_lat, test_lng = 51.5842, -0.0197
    expected = min(
        centroids.items(),
        key=lambda kv: haversine_miles(kv[1]["lat"], kv[1]["lng"], test_lat, test_lng),
    )[0]

    result = resolve_region(test_lat, test_lng)
    assert_eq("walthamstow_nearest", result, expected)


def test_exact_centroid_returns_name():
    """A lat/lng exactly on a centroid returns that centroid's name."""
    import json
    from homehunt.region import resolve_region

    centroids_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "neighbourhood_centroids.json",
    )
    with open(centroids_path) as f:
        centroids = json.load(f)

    # Pick first centroid and query exactly on it
    name, coords = next(iter(centroids.items()))
    result = resolve_region(coords["lat"], coords["lng"])
    assert_eq("exact_centroid", result, name)


def test_boundary_inclusive_0_5_miles():
    """A point exactly 0.5 miles from a centroid is matched (boundary inclusive)."""
    import json
    from homehunt.region import resolve_region

    centroids_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "neighbourhood_centroids.json",
    )
    with open(centroids_path) as f:
        centroids = json.load(f)

    # Use a centroid that is well isolated so no other centroid is closer
    # Hackney Wick is geographically peripheral in the set.
    name = "Hackney Wick, East London"
    coords = centroids[name]
    clat, clng = coords["lat"], coords["lng"]

    # Generate a point exactly 0.5 miles due north of the centroid
    test_lat, test_lng = _point_at_distance(clat, clng, 0.5, bearing_deg=0.0)

    # Sanity-check our helper
    dist = haversine_miles(clat, clng, test_lat, test_lng)
    assert abs(dist - 0.5) < 1e-6, f"Helper produced wrong distance: {dist}"

    result = resolve_region(test_lat, test_lng)
    assert_eq("boundary_0_5_miles_inclusive", result, name)


def test_beyond_half_mile_still_returns_nearest():
    """
    A point further than 0.5 miles from any centroid still returns the
    nearest centroid name. Threshold removed; nearest is always used.
    """
    import json
    from homehunt.region import resolve_region

    centroids_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "neighbourhood_centroids.json",
    )
    with open(centroids_path) as f:
        centroids = json.load(f)

    name = "Hackney Wick, East London"
    coords = centroids[name]
    clat, clng = coords["lat"], coords["lng"]

    # 1.5 miles due south of Hackney Wick centroid; nearest centroid in the
    # set is whichever scores lowest by Haversine. Compute the expected value.
    test_lat, test_lng = _point_at_distance(clat, clng, 1.5, bearing_deg=180.0)
    expected = min(
        centroids.items(),
        key=lambda kv: haversine_miles(kv[1]["lat"], kv[1]["lng"], test_lat, test_lng),
    )[0]

    result = resolve_region(test_lat, test_lng)
    assert_eq("beyond_half_mile_nearest", result, expected)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        ("highbury_inside", test_highbury_returns_highbury),
        ("walthamstow_nearest", test_far_point_returns_nearest_centroid),
        ("exact_centroid", test_exact_centroid_returns_name),
        ("boundary_0_5_inclusive", test_boundary_inclusive_0_5_miles),
        ("beyond_half_mile_nearest", test_beyond_half_mile_still_returns_nearest),
    ]

    failures = []
    for test_name, fn in tests:
        print(f"\n=== {test_name} ===")
        try:
            fn()
        except AssertionError as exc:
            print(f"  {exc}")
            failures.append(test_name)
        except Exception as exc:
            import traceback
            print(f"  ERROR in {test_name}: {exc}")
            traceback.print_exc()
            failures.append(test_name)

    print(f"\n{'=' * 50}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print(f"ALL {len(tests)} test groups passed.")
