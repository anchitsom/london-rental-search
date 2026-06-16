"""
Prototype script: fetch 10 live Rightmove listings and print extracted features.

Run: .venv/bin/python scripts/feature_extraction_demo.py

Uses the DirectHTTPScraper to fetch real pages, then runs extract_all over
each listing's description and features array. Output is a fixed-width table
for quick visual inspection of true positives and false positives.
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homehunt.scrapers.direct_http import DirectHTTPScraper
from homehunt.feature_extractor import extract_all

# Property URLs: discovered live from Rightmove search at runtime.
# Hardcoded fallback IDs are kept for offline runs; they may 404 once let agreed.
_FALLBACK_URLS = [
    "https://www.rightmove.co.uk/properties/169586816",
    "https://www.rightmove.co.uk/properties/174236120",
    "https://www.rightmove.co.uk/properties/174235406",
    "https://www.rightmove.co.uk/properties/174107000",
    "https://www.rightmove.co.uk/properties/173600000",
]

FIELDS = [
    "garden", "balcony", "bills_included", "furnished",
    "council_tax_band", "let_available_date",
]


def _fmt(val) -> str:
    if val is None:
        return "-"
    if isinstance(val, bool):
        return "Y" if val else "N"
    return str(val)[:14]


def _print_table(rows: list[dict]) -> None:
    header = (
        f"{'id':10}"
        f" {'garden':7}"
        f" {'balcony':7}"
        f" {'bills':10}"
        f" {'furnished':15}"
        f" {'ctb':10}"
        f" {'avail_date':18}"
    )
    sep = "-" * len(header)

    print(sep)
    print(header)
    print(sep)

    for row in rows:
        pid = row.get("property_id", "???")[:10]
        feats = row["features"]
        line = (
            f"{pid:10}"
            f" {_fmt(feats.get('garden')):7}"
            f" {_fmt(feats.get('balcony')):7}"
            f" {_fmt(feats.get('bills_included')):10}"
            f" {_fmt(feats.get('furnished')):15}"
            f" {_fmt(feats.get('council_tax_band')):10}"
            f" {_fmt(feats.get('let_available_date')):18}"
        )
        print(line)
        # Print available description fragment for manual spot-check
        desc_snippet = (row.get("description") or "")[:120].replace("\n", " ")
        feat_bullets = ", ".join((row.get("raw_features") or [])[:6])
        if desc_snippet:
            print(f"  desc: {desc_snippet}")
        if feat_bullets:
            print(f"  feats: {feat_bullets}")
        print()

    print(sep)


async def _discover_urls(n: int = 10) -> list[str]:
    """Return up to n live listing URLs from a Rightmove search."""
    try:
        from homehunt.scrapers.rightmove_api import discover_properties
        urls = await discover_properties("islington", max_results=n, min_price=1500, max_price=2800)
        # Strip fragment identifiers that the scraper adds
        clean = [u.split("#")[0] for u in urls]
        return clean[:n]
    except Exception as exc:
        print(f"Live discovery failed ({exc}), using fallback URLs.")
        return _FALLBACK_URLS


async def main():
    results = []
    skipped = 0

    property_urls = await _discover_urls(10)
    print(f"Running against {len(property_urls)} listings.\n")

    async with DirectHTTPScraper() as scraper:
        for url in property_urls:
            pid = url.rstrip("/").split("/")[-1]
            print(f"Fetching {pid}...", flush=True)
            result = await scraper.scrape_property(url)

            if not result.success or not result.data:
                print(f"  SKIP (failed: {result.error})")
                skipped += 1
                continue

            data = result.data
            description = data.get("description", "") or ""
            raw_features = data.get("features", []) or []
            title = data.get("title", "") or ""

            feats = extract_all(description, raw_features, title)

            results.append({
                "property_id": pid,
                "features": feats,
                "description": description,
                "raw_features": raw_features,
            })

    print(f"\nFetched {len(results)} listings, skipped {skipped}.\n")

    if results:
        _print_table(results)

        # Summary: how many had each field populated (non-None, non-unknown)
        print("\nField hit rates:")
        for field in FIELDS:
            populated = sum(
                1 for r in results
                if r["features"].get(field) not in (None, "unknown", False)
            )
            print(f"  {field:25} {populated}/{len(results)}")
    else:
        print("No results to display.")


if __name__ == "__main__":
    asyncio.run(main())
