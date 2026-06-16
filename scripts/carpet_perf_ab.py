"""
Carpet vision perf A/B harness.

Tiers under test (modelled on improvements.md from the floorplan-vision perf
work):

  V0  baseline    -- production carpet.py settings: no options, no resize, sem=2, 4 photos
  V1              -- Tier 1: Ollama options block (format=json, num_predict=64, num_ctx=2048, temperature=0)
  V2a             -- V1 + Tier 2 image pre-resize at long-edge 768
  V2b             -- V1 + Tier 2 image pre-resize at long-edge 640
  V3a             -- V2a + Tier 3a sem=3 (concurrency)
  V3b             -- V2a + Tier 3b photos=3 (less work)
  V3combined      -- V2a + sem=3 + photos=3
  V4              -- V2a but model swapped to qwen3-vl:2b-instruct-q4_K_M (Tier 4 quant/variant)
  V4_q8           -- V2a but model swapped to qwen3-vl:2b-instruct-q8_0 if available

The harness:
  1. Pulls a fixed sample of N listings from data/homehunt.db where images is non-null.
  2. Downloads up to 4 photos per listing once and caches the bytes to /tmp/carpet_perf_imgs/.
  3. For each variant, runs REPEATS passes over the listings using the variant's payload
     builder and concurrency settings. Records per-call latency, parse success, the
     extracted carpet bool, and matches it against the V0 baseline call on the same
     image to compute an "agreement with V0" rate.
  4. Writes data/carpet-perf/<batch_id>/{per_call.csv, per_listing.csv, summary.json}.

Production carpet.py is NOT modified by this harness. Each variant constructs its own
payload inline via build_payload() and runs against /api/generate at OLLAMA_URL.

Note on Ollama contention: production scrape may be live during measurement. The
script logs ollama_ps before each variant pass so stale numbers can be flagged.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import io
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
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from homehunt.carpet import ROOM_CARPET_PROMPT  # noqa: E402

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
DB_PATH = REPO_ROOT / "data" / "homehunt.db"
IMG_CACHE = Path("/tmp/carpet_perf_imgs_v2")
PHOTOS_PER_LISTING = 4

# When True, override variant["sem"] with 1 inside the harness so per-call
# latency is clean (no in-flight contention against the same Ollama instance).
# Tier 3 variants override this to False so the harness measures real
# per-listing total under the declared concurrency.
DEFAULT_SERIALISE_CALLS = True


# ----------------------------------------------------------------------
# Variants
# ----------------------------------------------------------------------

def _options_tier1() -> dict:
    return {
        "temperature": 0.0,
        "num_predict": 64,
        "num_ctx": 2048,
    }


VARIANTS: list[dict[str, Any]] = [
    {
        "name": "V0_baseline",
        "model": "qwen3-vl:2b",
        "format_json": False,
        "options": None,
        "resize_max_edge": None,
        "sem": 2,
        "photos": 4,
        "repeats_override": 1,  # baseline is the anchor; one pass over the sample is enough
        "notes": "production carpet.py settings (no options, no resize, sem=2, 4 photos)",
    },
    {
        "name": "V1_options",
        "model": "qwen3-vl:2b",
        "format_json": True,
        "options": _options_tier1(),
        "resize_max_edge": None,
        "sem": 2,
        "photos": 4,
        "notes": "Tier 1: format=json + options block",
    },
    {
        "name": "V2a_resize768",
        "model": "qwen3-vl:2b",
        "format_json": True,
        "options": _options_tier1(),
        "resize_max_edge": 768,
        "sem": 2,
        "photos": 4,
        "notes": "V1 + resize to 768px long edge",
    },
    {
        "name": "V2b_resize640",
        "model": "qwen3-vl:2b",
        "format_json": True,
        "options": _options_tier1(),
        "resize_max_edge": 640,
        "sem": 2,
        "photos": 4,
        "notes": "V1 + resize to 640px long edge",
    },
    {
        "name": "V3a_sem3",
        "model": "qwen3-vl:2b",
        "format_json": True,
        "options": _options_tier1(),
        "resize_max_edge": 768,
        "sem": 3,
        "photos": 4,
        "serialise": False,  # Tier 3 measures concurrency, do not override sem
        "notes": "V2a + concurrency sem=3",
    },
    {
        "name": "V3b_photos3",
        "model": "qwen3-vl:2b",
        "format_json": True,
        "options": _options_tier1(),
        "resize_max_edge": 768,
        "sem": 2,
        "photos": 3,
        "serialise": False,
        "notes": "V2a + only 3 photos per listing (sem=2 prod default)",
    },
    {
        "name": "V3combined",
        "model": "qwen3-vl:2b",
        "format_json": True,
        "options": _options_tier1(),
        "resize_max_edge": 768,
        "sem": 3,
        "photos": 3,
        "serialise": False,
        "notes": "V2a + sem=3 + photos=3",
    },
    {
        "name": "V4_instruct_q4",
        "model": "qwen3-vl:2b-instruct-q4_K_M",
        "format_json": True,
        "options": _options_tier1(),
        "resize_max_edge": 768,
        "sem": 2,
        "photos": 4,
        "notes": "V2a but model is the non-thinking instruct Q4 variant",
    },
    {
        "name": "V4_instruct_q8",
        "model": "qwen3-vl:2b-instruct-q8_0",
        "format_json": True,
        "options": _options_tier1(),
        "resize_max_edge": 768,
        "sem": 2,
        "photos": 4,
        "notes": "V2a but model is the non-thinking instruct Q8 variant",
    },
]


# ----------------------------------------------------------------------
# Image acquisition + cache
# ----------------------------------------------------------------------

def select_listings(n: int) -> list[tuple[str, list[str]]]:
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


async def warm_image_cache(listings: list[tuple[str, list[str]]]) -> dict[tuple[str, int], bytes]:
    """Download every photo once and cache to /tmp. Returns (uid, photo_idx) -> bytes."""
    IMG_CACHE.mkdir(parents=True, exist_ok=True)
    cache: dict[tuple[str, int], bytes] = {}
    async with httpx.AsyncClient() as client:
        for uid, urls in listings:
            for idx, url in enumerate(urls):
                key = (uid, idx)
                disk = IMG_CACHE / f"{uid.replace(':', '_')}__{idx}.bin"
                if disk.exists():
                    cache[key] = disk.read_bytes()
                    continue
                img = await fetch_image(client, url)
                if img is None:
                    continue
                disk.write_bytes(img)
                cache[key] = img
    return cache


def encode_image(raw: bytes, resize_max_edge: Optional[int]) -> str:
    """Base64 encode the image. Optionally pre-resize via Pillow."""
    if resize_max_edge is None:
        return base64.b64encode(raw).decode()
    img = Image.open(io.BytesIO(raw))
    img.thumbnail((resize_max_edge, resize_max_edge), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


# ----------------------------------------------------------------------
# Per-call invocation (variant-aware payload builder)
# ----------------------------------------------------------------------

def build_payload(variant: dict, image_b64: str) -> dict:
    payload: dict[str, Any] = {
        "model": variant["model"],
        "prompt": ROOM_CARPET_PROMPT,
        "images": [image_b64],
        "stream": False,
    }
    if variant.get("format_json"):
        payload["format"] = "json"
    if variant.get("options"):
        payload["options"] = dict(variant["options"])
    return payload


async def call_variant(
    client: httpx.AsyncClient,
    variant: dict,
    image_b64: str,
) -> dict[str, Any]:
    payload = build_payload(variant, image_b64)
    record: dict[str, Any] = {
        "variant": variant["name"],
        "model": variant["model"],
        "latency_s": None,
        "parse_success": False,
        "missing_keys": True,
        "wrong_types": True,
        "room_type": None,
        "carpet": None,
        "confidence": None,
        "failure_class": None,
        "raw_len": 0,
        "eval_count": None,
        "prompt_eval_count": None,
    }
    t0 = time.time()
    try:
        resp = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            timeout=300.0,
        )
        record["latency_s"] = round(time.time() - t0, 3)
        resp.raise_for_status()
        body = resp.json()
        # qwen3-vl:2b is a thinking model: with format=json it routes structured
        # output to the `thinking` field and leaves `response` empty. The instruct
        # variant returns JSON in `response` as expected. Fall back to `thinking`
        # so the harness handles both shapes uniformly.
        raw = body.get("response", "").strip()
        if not raw:
            raw = body.get("thinking", "").strip()
        record["raw_len"] = len(raw)
        record["eval_count"] = body.get("eval_count")
        record["prompt_eval_count"] = body.get("prompt_eval_count")

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
                rt, cp, cf = parsed["room_type"], parsed["carpet"], parsed["confidence"]
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
        if record["latency_s"] is None:
            record["latency_s"] = round(time.time() - t0, 3)
        record["failure_class"] = f"http_error:{type(exc).__name__}:{str(exc)[:80]}"
    return record


# ----------------------------------------------------------------------
# Per-variant runner
# ----------------------------------------------------------------------

async def run_variant_pass(
    variant: dict,
    listings: list[tuple[str, list[str]]],
    image_cache: dict[tuple[str, int], bytes],
    repeat_idx: int,
) -> list[dict]:
    """Run one pass of one variant over all listings with the variant's sem and photo cap.

    For variants where serialise=True (the default), the in-flight semaphore is
    forced to 1 so per-call latency is uncontended. Tier 3 variants set
    serialise=False so the harness measures real per-listing total throughput
    under the declared sem.
    """
    serialise = variant.get("serialise", DEFAULT_SERIALISE_CALLS)
    effective_sem = 1 if serialise else variant["sem"]
    sem = asyncio.Semaphore(effective_sem)
    records: list[dict] = []

    async def call_with_sem(client, image_b64, ctx):
        async with sem:
            r = await call_variant(client, variant, image_b64)
            r.update(ctx)
            return r

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    ) as client:
        # Per listing: parallelise photo calls (up to sem in flight at a time across listings).
        # We fan out all listings and let the semaphore bound concurrency, just like prod.
        tasks = []
        for listing_idx, (uid, _urls) in enumerate(listings):
            for photo_idx in range(min(variant["photos"], PHOTOS_PER_LISTING)):
                key = (uid, photo_idx)
                if key not in image_cache:
                    continue
                raw = image_cache[key]
                try:
                    image_b64 = encode_image(raw, variant.get("resize_max_edge"))
                except Exception as exc:
                    records.append({
                        "variant": variant["name"],
                        "model": variant["model"],
                        "uid": uid,
                        "photo_idx": photo_idx,
                        "repeat_idx": repeat_idx,
                        "latency_s": None,
                        "parse_success": False,
                        "failure_class": f"encode_error:{type(exc).__name__}",
                    })
                    continue
                ctx = {
                    "uid": uid,
                    "photo_idx": photo_idx,
                    "repeat_idx": repeat_idx,
                }
                tasks.append(call_with_sem(client, image_b64, ctx))
        # Run them. Per-listing parallelism within the bound of variant["sem"].
        # NOTE: per-listing total is computed downstream from per-call timings.
        for fut in asyncio.as_completed(tasks):
            r = await fut
            records.append(r)
    return records


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------

def aggregate_per_variant(records: list[dict]) -> dict:
    by_variant: dict[str, list[dict]] = {}
    for r in records:
        by_variant.setdefault(r["variant"], []).append(r)

    summary: dict[str, Any] = {"per_variant": {}}
    # Build V0 baseline carpet map for agreement check: (uid, photo_idx) -> carpet bool, mode across repeats
    v0 = by_variant.get("V0_baseline", [])
    v0_carpet_by_pair: dict[tuple[str, int], list[bool]] = {}
    for r in v0:
        if r.get("parse_success") and r.get("carpet") is not None:
            v0_carpet_by_pair.setdefault((r["uid"], r["photo_idx"]), []).append(r["carpet"])
    v0_baseline_call: dict[tuple[str, int], bool] = {}
    for k, vs in v0_carpet_by_pair.items():
        # majority vote across V0 repeats; tie-break to True
        true_count = sum(1 for v in vs if v)
        v0_baseline_call[k] = true_count >= (len(vs) - true_count)

    for variant_name, recs in by_variant.items():
        latencies = [r["latency_s"] for r in recs if r.get("latency_s") is not None]
        parsed = [r for r in recs if r.get("parse_success")]
        clean = [r for r in parsed if not r.get("missing_keys") and not r.get("wrong_types")]
        # Per-listing total: sum latencies per (uid, repeat_idx)
        per_listing_totals: dict[tuple[str, int], float] = {}
        for r in recs:
            if r.get("latency_s") is None:
                continue
            key = (r["uid"], r.get("repeat_idx", 0))
            per_listing_totals[key] = per_listing_totals.get(key, 0.0) + r["latency_s"]
        listing_totals = list(per_listing_totals.values())
        # Agreement with V0: only on (uid, photo_idx) where V0 has a carpet and this variant parsed cleanly.
        agree_n = 0
        agree_total = 0
        for r in clean:
            k = (r["uid"], r["photo_idx"])
            if k in v0_baseline_call and r.get("carpet") is not None:
                agree_total += 1
                if r["carpet"] == v0_baseline_call[k]:
                    agree_n += 1
        # Failure buckets
        failures: dict[str, int] = {}
        for r in recs:
            fc = r.get("failure_class")
            if fc:
                # collapse http_error:... noise into base class
                bucket = fc.split(":", 1)[0]
                failures[bucket] = failures.get(bucket, 0) + 1
        summary["per_variant"][variant_name] = {
            "n_calls": len(recs),
            "n_parsed": len(parsed),
            "n_clean": len(clean),
            "json_parse_rate": round(len(parsed) / len(recs), 4) if recs else None,
            "three_key_clean_rate": round(len(clean) / len(recs), 4) if recs else None,
            "mean_latency_s": round(statistics.mean(latencies), 3) if latencies else None,
            "p95_latency_s": (
                round(statistics.quantiles(latencies, n=20)[18], 3)
                if len(latencies) >= 20 else (round(max(latencies), 3) if latencies else None)
            ),
            "max_latency_s": round(max(latencies), 3) if latencies else None,
            "mean_per_listing_total_s": round(statistics.mean(listing_totals), 3) if listing_totals else None,
            "p95_per_listing_total_s": (
                round(statistics.quantiles(listing_totals, n=20)[18], 3)
                if len(listing_totals) >= 20 else (round(max(listing_totals), 3) if listing_totals else None)
            ),
            "max_per_listing_total_s": round(max(listing_totals), 3) if listing_totals else None,
            "n_listings_measured": len(per_listing_totals),
            "agreement_with_v0": (
                round(agree_n / agree_total, 4) if agree_total else None
            ),
            "agreement_n": agree_n,
            "agreement_total": agree_total,
            "failure_buckets": failures,
        }
    return summary


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------

def write_artefacts(records: list[dict], summary: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "per_call.csv"
    fieldnames = [
        "variant", "model", "uid", "photo_idx", "repeat_idx",
        "latency_s", "parse_success", "missing_keys", "wrong_types",
        "room_type", "carpet", "confidence", "raw_len",
        "eval_count", "prompt_eval_count", "failure_class",
    ]
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow(r)
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"Wrote {csv_path}")
    print(f"Wrote {summary_path}")


def log_ollama_state() -> None:
    try:
        import subprocess
        out = subprocess.run(
            ["/opt/homebrew/bin/ollama", "ps"],
            capture_output=True, text=True, timeout=5,
        )
        print("--- ollama ps ---")
        print(out.stdout.strip())
        print("------")
    except Exception as exc:
        print(f"(ollama ps failed: {exc})")


def model_available(model: str) -> bool:
    try:
        with httpx.Client() as client:
            resp = client.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
            tags = [m["name"] for m in resp.json().get("models", [])]
            return model in tags or any(t.startswith(model + ":") for t in tags)
    except Exception:
        return False


async def main(args) -> None:
    listings = select_listings(args.n)
    if not listings:
        print("No listings with images found. Aborting.", file=sys.stderr)
        sys.exit(1)
    print(f"Selected {len(listings)} listings, "
          f"{sum(min(len(u), PHOTOS_PER_LISTING) for _, u in listings)} photo slots.")

    print("Warming image cache (one download per photo)...")
    image_cache = await warm_image_cache(listings)
    print(f"Cached {len(image_cache)} images at {IMG_CACHE}.\n")

    if args.variants:
        wanted = set(args.variants.split(","))
        variants = [v for v in VARIANTS if v["name"] in wanted]
    else:
        variants = list(VARIANTS)
    # Skip Tier 4 variants if model is not pulled
    runnable: list[dict] = []
    for v in variants:
        if v["model"] != "qwen3-vl:2b" and not model_available(v["model"]):
            print(f"SKIP {v['name']}: model {v['model']} not available locally")
            continue
        runnable.append(v)
    print(f"Variants to run: {[v['name'] for v in runnable]}\n")

    batch_id = datetime.now(tz=__import__("datetime").timezone.utc).strftime("%Y%m%d-%H%M")
    out_dir = Path(args.out) if args.out else REPO_ROOT / "data" / "carpet-perf" / batch_id
    print(f"Output -> {out_dir}\n")

    all_records: list[dict] = []
    for variant in runnable:
        print(f"\n=== Variant {variant['name']} ({variant['notes']}) ===")
        log_ollama_state()
        repeats = variant.get("repeats_override") or args.repeats
        for repeat_idx in range(repeats):
            t0 = time.time()
            recs = await run_variant_pass(variant, listings, image_cache, repeat_idx)
            dt = time.time() - t0
            print(f"  repeat {repeat_idx}: {len(recs)} calls in {dt:.1f}s "
                  f"(mean {dt/max(1,len(recs)):.2f}s/call)")
            all_records.extend(recs)

    summary = aggregate_per_variant(all_records)
    summary["meta"] = {
        "n_listings": len(listings),
        "repeats": args.repeats,
        "ollama_url": OLLAMA_URL,
        "batch_id": batch_id,
        "variants": [v["name"] for v in runnable],
    }

    write_artefacts(all_records, summary, out_dir)

    print("\n=== Summary table ===")
    headers = ["variant", "n", "mean", "p95", "max", "listing_mean", "parse", "agree_v0"]
    print("  " + "  ".join(f"{h:14s}" for h in headers))
    for name, s in summary["per_variant"].items():
        row = [
            name,
            str(s["n_calls"]),
            f"{s['mean_latency_s']}" if s['mean_latency_s'] is not None else "-",
            f"{s['p95_latency_s']}" if s['p95_latency_s'] is not None else "-",
            f"{s['max_latency_s']}" if s['max_latency_s'] is not None else "-",
            f"{s['mean_per_listing_total_s']}" if s['mean_per_listing_total_s'] is not None else "-",
            f"{s['three_key_clean_rate']}" if s['three_key_clean_rate'] is not None else "-",
            f"{s['agreement_with_v0']}" if s['agreement_with_v0'] is not None else "-",
        ]
        print("  " + "  ".join(f"{c:14s}" for c in row))


def cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=20, help="Number of listings to sample")
    parser.add_argument("--repeats", type=int, default=3, help="How many passes per variant")
    parser.add_argument("--out", default=None, help="Output directory")
    parser.add_argument("--variants", default=None,
                        help="Comma-separated subset of variant names to run")
    args = parser.parse_args()
    asyncio.run(main(args))


if __name__ == "__main__":
    cli()
