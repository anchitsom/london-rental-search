"""
OpenRent Probe — Task 4: PROPERTYBYID is an id-to-array-index lookup map (NOT a metadata dict).

Recon revealed `var PROPERTYBYID = {2898832 : 0, 2849042 : 1, ...}` — values are integer
positions into the parallel PROPERTYLISTLATITUDES / PROPERTYLISTLONGITUDES arrays.
This means the search page carries no per-property metadata beyond ids and geo.

Pass: parsed dict non-empty, keys are integers, values are non-negative integers, and the
indices are a valid permutation of [0, len(PROPERTYIDS)).
Implication: all per-listing metadata (price, beds, EPC, etc.) requires a detail fetch.
"""

import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXP = PROJECT_ROOT / "experiments" / "openrent-probe"

spec = importlib.util.spec_from_file_location("openrent_probe_extract", EXP / "probe" / "extract.py")
em = importlib.util.module_from_spec(spec); spec.loader.exec_module(em)


def main() -> int:
    html = (EXP / "fixtures" / "search-page-0.html").read_text()
    by_id = em.extract_propertybyid(html)
    assert by_id, "PROPERTYBYID parsed as empty"
    sample_key = next(iter(by_id))
    sample_val = by_id[sample_key]
    assert isinstance(sample_key, int), f"key not int: {type(sample_key).__name__}"
    assert isinstance(sample_val, int) and sample_val >= 0, f"value not non-negative int: {sample_val!r}"
    # Cross-check: values should be a permutation of [0, N)
    n = len(by_id)
    values = sorted(by_id.values())
    assert values == list(range(n)), f"values are not a permutation of [0, {n}); got first 10 {values[:10]}"
    # Cross-check: keys should match PROPERTYIDS exactly (as a set)
    search = em.extract_search(html)
    assert set(by_id.keys()) == set(search["property_ids"]), "PROPERTYBYID keys do not match PROPERTYIDS"
    print(f"PASS — {n} entries, sample {sample_key} -> index {sample_val}")
    print(f"Format confirmed: id -> array index into parallel lat/lng arrays. NOT a metadata dict.")
    (EXP / "fixtures" / "propertybyid-sample.json").write_text(json.dumps({
        "format": "id_to_array_index_lookup",
        "entry_count": n,
        "sample_entries": {str(k): by_id[k] for k in list(by_id)[:10]},
        "implication": "Search page carries no per-listing metadata. All fields require a detail fetch.",
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
