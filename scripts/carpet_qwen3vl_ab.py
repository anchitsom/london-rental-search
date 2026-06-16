"""
A/B harness: llava-phi3 vs qwen3-vl:2b for carpet detection.

Pulls N listings from data/homehunt.db, downloads up to 4 photos per listing,
runs both models against each photo using the production ROOM_CARPET_PROMPT,
and writes a per-photo CSV plus an aggregate report markdown to
data/carpet-ab/<batch_id>/.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/carpet_qwen3vl_ab.py --n 12

Aggregates reported:
    - Per model: JSON-parse success rate, three-key conformance rate, mean and
      p95 latency, mean confidence on parsed responses.
    - Inter-model: agreement on room_type, agreement on carpet (only on rows
      where both parsed cleanly).
    - JSON-fragility class breakdown: counts of failures bucketed by
      (json_decode_error, missing_keys, wrong_types).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from homehunt.carpet import ROOM_CARPET_PROMPT  # noqa: E402

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODELS = ["llava-phi3", "qwen3-vl:2b"]
PHOTOS_PER_LISTING = 4
DB_PATH = REPO_ROOT / "data" / "homehunt.db"


def select_listings(n: int) -> list[tuple[str, list[str]]]:
    """Pull N most recent listings with at least 3 image URLs."""
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute(
        """
        SELECT uid, images FROM listing
        WHERE images IS NOT NULL
          AND length(images) > 50
          AND last_scraped IS NOT NULL
        ORDER BY last_scraped DESC
        LIMIT ?
        """,
        (n,),
    )
    rows = cur.fetchall()
    conn.close()
    out = []
    for uid, images_json in rows:
        try:
            images = json.loads(images_json)
        except Exception:
            continue
        if isinstance(images, list) and len(images) >= 1:
            out.append((uid, images[:PHOTOS_PER_LISTING]))
    return out


async def fetch_image(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=20.0,
        )
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        print(f"  image fetch failed: {url[:80]} -> {exc}", file=sys.stderr)
        return None


async def call_model(
    client: httpx.AsyncClient, model: str, image_b64: str
) -> dict[str, Any]:
    """Call one model on one image. Returns the per-call record."""
    payload = {
        "model": model,
        "prompt": ROOM_CARPET_PROMPT,
        "images": [image_b64],
        "stream": False,
    }
    record: dict[str, Any] = {
        "model": model,
        "raw": "",
        "latency_s": None,
        "parse_success": False,
        "missing_keys": True,
        "wrong_types": True,
        "room_type": None,
        "carpet": None,
        "confidence": None,
        "failure_class": None,
    }
    t0 = time.time()
    try:
        resp = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            timeout=300.0,
        )
        record["latency_s"] = round(time.time() - t0, 2)
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        record["raw"] = raw
        # Strip markdown fences (same logic as production carpet.py).
        cleaned = raw
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
        try:
            parsed = json.loads(cleaned)
            record["parse_success"] = True
            has_keys = all(k in parsed for k in ("room_type", "carpet", "confidence"))
            record["missing_keys"] = not has_keys
            if has_keys:
                rt = parsed["room_type"]
                cp = parsed["carpet"]
                cf = parsed["confidence"]
                wrong = (
                    not isinstance(rt, str)
                    or not isinstance(cp, bool)
                    or not isinstance(cf, (int, float))
                )
                record["wrong_types"] = wrong
                record["room_type"] = rt if isinstance(rt, str) else str(rt)
                record["carpet"] = bool(cp) if isinstance(cp, bool) else None
                record["confidence"] = float(cf) if isinstance(cf, (int, float)) else None
                if wrong:
                    record["failure_class"] = "wrong_types"
            else:
                record["failure_class"] = "missing_keys"
        except json.JSONDecodeError:
            record["failure_class"] = "json_decode_error"
    except Exception as exc:
        record["latency_s"] = round(time.time() - t0, 2)
        record["failure_class"] = f"http_error:{type(exc).__name__}"
    return record


async def run_ab(listings: list[tuple[str, list[str]]], out_dir: Path) -> list[dict]:
    """Run both models over every photo. Returns a flat list of per-call records."""
    all_records: list[dict] = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    ) as client:
        for listing_idx, (uid, urls) in enumerate(listings, start=1):
            print(f"[{listing_idx}/{len(listings)}] {uid} ({len(urls)} photos)")
            for photo_idx, url in enumerate(urls):
                img_bytes = await fetch_image(client, url)
                if img_bytes is None:
                    continue
                img_b64 = base64.b64encode(img_bytes).decode()
                # Sequential per photo: Ollama gets confused with concurrent
                # calls on the same backend. Across photos we go one at a time
                # too, to keep latency comparable across runs.
                for model in MODELS:
                    rec = await call_model(client, model, img_b64)
                    rec["uid"] = uid
                    rec["photo_idx"] = photo_idx
                    rec["url"] = url
                    all_records.append(rec)
                    print(
                        f"    photo {photo_idx} {model:14s} "
                        f"parse={rec['parse_success']} "
                        f"room={rec.get('room_type')} "
                        f"carpet={rec.get('carpet')} "
                        f"latency={rec['latency_s']}s"
                    )
    return all_records


def aggregate(records: list[dict]) -> dict:
    """Compute per-model and inter-model aggregates."""
    by_model: dict[str, list[dict]] = {m: [] for m in MODELS}
    for r in records:
        by_model[r["model"]].append(r)

    summary: dict[str, Any] = {"per_model": {}}
    for model, recs in by_model.items():
        n = len(recs)
        if n == 0:
            continue
        parsed = [r for r in recs if r["parse_success"]]
        clean = [r for r in parsed if not r["missing_keys"] and not r["wrong_types"]]
        latencies = [r["latency_s"] for r in recs if r["latency_s"] is not None]
        confidences = [
            r["confidence"] for r in clean if r["confidence"] is not None
        ]
        failure_buckets: dict[str, int] = {}
        for r in recs:
            fc = r.get("failure_class")
            if fc:
                failure_buckets[fc] = failure_buckets.get(fc, 0) + 1
        summary["per_model"][model] = {
            "n_calls": n,
            "n_json_parse_success": len(parsed),
            "n_three_key_clean": len(clean),
            "json_parse_rate": round(len(parsed) / n, 4),
            "three_key_clean_rate": round(len(clean) / n, 4),
            "json_fragility_rate": round(1 - (len(clean) / n), 4),
            "mean_latency_s": round(statistics.mean(latencies), 2)
            if latencies
            else None,
            "p95_latency_s": round(
                statistics.quantiles(latencies, n=20)[18], 2
            )
            if len(latencies) >= 20
            else (max(latencies) if latencies else None),
            "mean_confidence": round(statistics.mean(confidences), 3)
            if confidences
            else None,
            "failure_buckets": failure_buckets,
        }

    # Inter-model agreement: only on photos where BOTH models returned a clean
    # three-key parse.
    pair_index: dict[tuple[str, int], dict[str, dict]] = {}
    for r in records:
        key = (r["uid"], r["photo_idx"])
        pair_index.setdefault(key, {})[r["model"]] = r
    paired_clean = []
    for key, by_m in pair_index.items():
        if all(m in by_m for m in MODELS):
            ra, rb = by_m[MODELS[0]], by_m[MODELS[1]]
            ra_ok = (
                ra["parse_success"]
                and not ra["missing_keys"]
                and not ra["wrong_types"]
            )
            rb_ok = (
                rb["parse_success"]
                and not rb["missing_keys"]
                and not rb["wrong_types"]
            )
            if ra_ok and rb_ok:
                paired_clean.append((ra, rb))
    n_paired = len(paired_clean)
    if n_paired:
        room_agree = sum(
            1 for a, b in paired_clean if a["room_type"] == b["room_type"]
        )
        carpet_agree = sum(1 for a, b in paired_clean if a["carpet"] == b["carpet"])
        # Disagreements bucketed.
        disagreements_room = [
            (a["uid"], a["photo_idx"], a["room_type"], b["room_type"])
            for a, b in paired_clean
            if a["room_type"] != b["room_type"]
        ]
        disagreements_carpet = [
            (a["uid"], a["photo_idx"], a["carpet"], b["carpet"])
            for a, b in paired_clean
            if a["carpet"] != b["carpet"]
        ]
        summary["inter_model"] = {
            "n_paired_clean": n_paired,
            "room_type_agree_rate": round(room_agree / n_paired, 4),
            "carpet_agree_rate": round(carpet_agree / n_paired, 4),
            "disagreements_room": disagreements_room[:20],
            "disagreements_carpet": disagreements_carpet[:20],
        }
    else:
        summary["inter_model"] = {"n_paired_clean": 0}
    summary["models"] = MODELS
    summary["total_photos"] = len(pair_index)
    return summary


def write_artefacts(records: list[dict], summary: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "per_call.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "uid",
                "photo_idx",
                "url",
                "model",
                "latency_s",
                "parse_success",
                "missing_keys",
                "wrong_types",
                "room_type",
                "carpet",
                "confidence",
                "failure_class",
                "raw",
            ],
        )
        writer.writeheader()
        for r in records:
            writer.writerow(r)
    summary_path = out_dir / "summary.json"
    with summary_path.open("w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nWrote {csv_path}")
    print(f"Wrote {summary_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=12, help="Number of listings")
    parser.add_argument("--out", default=None, help="Output directory")
    args = parser.parse_args()

    listings = select_listings(args.n)
    if not listings:
        print("No listings with images found. Aborting.", file=sys.stderr)
        sys.exit(1)
    print(f"Selected {len(listings)} listings, "
          f"~{sum(len(u) for _, u in listings)} photo slots, "
          f"x {len(MODELS)} models = "
          f"{sum(len(u) for _, u in listings) * len(MODELS)} total calls")

    batch_id = datetime.utcnow().strftime("%Y%m%d-%H%M")
    out_dir = Path(args.out) if args.out else REPO_ROOT / "data" / "carpet-ab" / batch_id
    print(f"Output -> {out_dir}\n")

    records = asyncio.run(run_ab(listings, out_dir))
    summary = aggregate(records)
    write_artefacts(records, summary, out_dir)

    print("\n=== Summary ===")
    for model, s in summary["per_model"].items():
        print(f"  {model}:")
        print(f"    json_parse_rate     = {s['json_parse_rate']}")
        print(f"    three_key_clean     = {s['three_key_clean_rate']}")
        print(f"    json_fragility_rate = {s['json_fragility_rate']}")
        print(f"    mean_latency_s      = {s['mean_latency_s']}")
        print(f"    p95_latency_s       = {s['p95_latency_s']}")
        print(f"    mean_confidence     = {s['mean_confidence']}")
        print(f"    failures            = {s['failure_buckets']}")
    im = summary["inter_model"]
    print(f"  inter_model: paired_clean={im['n_paired_clean']}")
    if im["n_paired_clean"]:
        print(f"    room_type_agree_rate = {im['room_type_agree_rate']}")
        print(f"    carpet_agree_rate    = {im['carpet_agree_rate']}")


if __name__ == "__main__":
    main()
