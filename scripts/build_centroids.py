"""
Build data/neighbourhood_centroids.json from tiers.yaml.

For each neighbourhood in tiers.yaml, looks up a representative postcode via
postcodes.io (free public API, no key required) and records the centroid
lat/lng returned by the API.

Representative postcodes were chosen as the well-known area-centre postcode
for each neighbourhood. Sources: Royal Mail postcode finder, Google Maps
centroid confirmation, and local knowledge.

Usage:
    python3 scripts/build_centroids.py

The script is idempotent: re-running it overwrites the output file with
freshly-fetched coordinates.

Output: data/neighbourhood_centroids.json
"""

import json
import os
import sys
import time

import httpx
import yaml

# ---------------------------------------------------------------------------
# Representative postcodes per neighbourhood.
# Each postcode is the widely-recognised centre of the named area.
# Format: neighbourhood name (exactly as in tiers.yaml) -> postcode string.
# ---------------------------------------------------------------------------

REPRESENTATIVE_POSTCODES = {
    # Tier 1
    "Islington, North London":       "N1 2UD",    # Islington Green / Upper Street centre
    "Barnsbury, North London":       "N1 1ER",    # Barnsbury Street / Liverpool Road
    "Highbury, North London":        "N5 1RA",    # Highbury Fields / Blackstock Road
    "Canonbury, North London":       "N1 2NQ",    # Canonbury Square
    "Kings Cross, North London":     "N1C 4AX",   # Kings Cross station area
    "Camden Town, North West London":"NW1 8NH",   # Camden High Street / Lock
    "Primrose Hill, North West London":"NW1 8YW", # Primrose Hill village / Regent's Park Rd
    "Shoreditch, East London":       "EC2A 3JL",  # Shoreditch High Street / Old Street
    "Hoxton, North London":          "N1 6SH",    # Hoxton Square
    "Haggerston, East London":       "E2 8JT",    # Haggerston station / Kingsland Road
    "Dalston, East London":          "E8 1BH",    # Dalston Junction / Kingsland High Street
    "De Beauvoir Town, North London":"N1 5SB",    # De Beauvoir Road / Englefield Road
    "Stoke Newington, North London": "N16 9ES",   # Stoke Newington Church Street
    "London Fields, East London":    "E8 3EU",    # London Fields park / Broadway Market area
    # Tier 2
    "Kentish Town, North West London":"NW5 2AB",  # Kentish Town Road centre
    "Hampstead, North West London":  "NW3 1QG",   # Hampstead High Street
    "Tufnell Park, North London":    "N7 0PS",    # Tufnell Park Road / Brecknock Road
    "Archway, London":               "N19 3TD",   # Archway Road / Junction Road
    "Fitzrovia, London":             "W1T 1JX",   # Goodge Street / Charlotte Street
    "Bloomsbury, Central London":    "WC1B 3HH",  # Bloomsbury / Museum Street
    "Holborn, Central London":       "WC1A 1BN",  # Holborn / High Holborn
    # Tier 3
    "Clapton, East London":          "E5 9BQ",    # Lower Clapton Road / Chatsworth Road
    "Homerton, East London":         "E9 6BU",    # Homerton High Street
    "Hackney Wick, East London":     "E9 5EN",    # Hackney Wick station / White Post Lane
    "Belsize Park, North West London":"NW3 4AY",  # Belsize Lane / Haverstock Hill
    "Finsbury Park, North London":   "N4 2DH",    # Stroud Green Road / Finsbury Park
    "Chalk Farm, North West London": "NW1 8AN",   # Chalk Farm Road
    "Holloway, North London":        "N7 8LX",    # Holloway Road centre
    "Upper Holloway, North London":  "N19 5QQ",   # Archway Road / Holloway
    "Lower Holloway, North London":  "N7 8PP",    # Lower Holloway / Caledonian Road area
}


def _load_tiers(tiers_path: str) -> list[str]:
    """Return all neighbourhood names from tiers.yaml, preserving order."""
    with open(tiers_path) as f:
        tiers = yaml.safe_load(f)

    names = []
    for tier in tiers:
        for loc in tier.get("locations", []):
            names.append(loc["name"])
    return names


def _lookup_centroid(postcode: str, client: httpx.Client) -> tuple[float, float]:
    """
    Fetch centroid (lat, lng) for a UK postcode from postcodes.io.

    Returns (latitude, longitude).
    Raises ValueError if the postcode is not found or the API returns an error.
    """
    url = f"https://api.postcodes.io/postcodes/{postcode.replace(' ', '%20')}"
    response = client.get(url, timeout=10.0)
    response.raise_for_status()
    data = response.json()

    if data.get("status") != 200 or not data.get("result"):
        raise ValueError(f"Postcode not found: {postcode}")

    result = data["result"]
    return float(result["latitude"]), float(result["longitude"])


def build_centroids(
    tiers_path: str,
    output_path: str,
    postcodes: dict[str, str],
) -> dict[str, dict]:
    """
    Build centroids dict by looking up each neighbourhood's representative postcode.

    Validates that all neighbourhood names in tiers.yaml are covered by
    the REPRESENTATIVE_POSTCODES mapping.

    Returns the centroids dict (also written to output_path as JSON).
    """
    neighbourhood_names = _load_tiers(tiers_path)

    missing = [n for n in neighbourhood_names if n not in postcodes]
    if missing:
        print(f"ERROR: missing representative postcodes for: {missing}", file=sys.stderr)
        sys.exit(1)

    centroids = {}
    with httpx.Client() as client:
        for name in neighbourhood_names:
            postcode = postcodes[name]
            print(f"  {name} <- {postcode} ...", end=" ", flush=True)
            try:
                lat, lng = _lookup_centroid(postcode, client)
                centroids[name] = {"lat": lat, "lng": lng}
                print(f"({lat}, {lng})")
            except Exception as exc:
                print(f"FAILED: {exc}", file=sys.stderr)
                sys.exit(1)
            # Be polite to the free API
            time.sleep(0.1)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(centroids, f, indent=2)
        f.write("\n")

    return centroids


if __name__ == "__main__":
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tiers_path = os.path.join(repo_root, "tiers.yaml")
    output_path = os.path.join(repo_root, "data", "neighbourhood_centroids.json")

    print(f"Building centroids from {tiers_path}")
    print(f"Output: {output_path}")
    print()

    centroids = build_centroids(tiers_path, output_path, REPRESENTATIVE_POSTCODES)

    print(f"\nDone. {len(centroids)} centroids written to {output_path}")
