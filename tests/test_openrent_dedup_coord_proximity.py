"""
OpenRent Integration - Task 6: cross-portal dedup matches an OpenRent row to a
Rightmove row within 50m + same beds + price within 2%.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from homehunt.dedup import _pair_matches


def L(**kw):
    defaults = dict(
        portal=None,
        postcode=None,
        bedrooms=None,
        price_numeric=None,
        latitude=None,
        longitude=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def main() -> int:
    # Case 1: Rightmove + Rightmove, same postcode, beds, price-within-2%: MATCH (unchanged)
    a = L(portal="rightmove", postcode="N1 7TT", bedrooms=2, price_numeric=240000)
    b = L(portal="rightmove", postcode="N1 7TT", bedrooms=2, price_numeric=244000)
    assert _pair_matches(a, b), "Rightmove-Rightmove same-postcode match regressed"

    # Case 2: OpenRent + Rightmove, OpenRent no postcode, coords 30m apart, same beds, price within 2%: MATCH
    a = L(portal="openrent", postcode=None, latitude=51.5326, longitude=-0.1058, bedrooms=2, price_numeric=240000)
    b = L(portal="rightmove", postcode="N1 7TT", latitude=51.5327, longitude=-0.1058, bedrooms=2, price_numeric=244000)
    assert _pair_matches(a, b), "OpenRent-Rightmove coord-proximity match did not fire"

    # Case 3: OpenRent + Rightmove, coords 200m apart (above 50m threshold): NO MATCH
    a = L(portal="openrent", postcode=None, latitude=51.5326, longitude=-0.1058, bedrooms=2, price_numeric=240000)
    b = L(portal="rightmove", postcode="N1 7TT", latitude=51.5345, longitude=-0.1058, bedrooms=2, price_numeric=244000)
    assert not _pair_matches(a, b), "200m apart should not match"

    # Case 4: OpenRent + Rightmove, 30m apart, different beds: NO MATCH
    a = L(portal="openrent", latitude=51.5326, longitude=-0.1058, bedrooms=1, price_numeric=240000)
    b = L(portal="rightmove", postcode="N1 7TT", latitude=51.5327, longitude=-0.1058, bedrooms=2, price_numeric=244000)
    assert not _pair_matches(a, b), "different beds should not match"

    # Case 5: OpenRent + OpenRent, same coords, beds, price: MATCH
    a = L(portal="openrent", latitude=51.5326, longitude=-0.1058, bedrooms=2, price_numeric=240000)
    b = L(portal="openrent", latitude=51.5326, longitude=-0.1058, bedrooms=2, price_numeric=244000)
    assert _pair_matches(a, b), "OpenRent-OpenRent at identical coords should match"

    print("PASS - all 5 dedup scenarios behave correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
