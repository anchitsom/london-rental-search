#!/usr/bin/env python
"""Carpet labelling tool (multi-listing).

Downloads all photos for a set of listings into tests/fixtures/carpet/<uid>/photos/,
serves a small localhost web app where the user labels each listing in turn,
saves labels.yaml fixtures.

Usage:
    .venv/bin/python scripts/carpet_label_capture.py
        # uses the default seed list (13 listings stratified by carpet state)

    .venv/bin/python scripts/carpet_label_capture.py --uids u1,u2,u3
        # custom comma-separated list

    .venv/bin/python scripts/carpet_label_capture.py --uids-file seeds.txt
        # newline-separated file

Optional:
    --port 8016
    --host 0.0.0.0
    --db data/homehunt.db

The page stays up until you Ctrl-C. Saving redirects to the next unlabelled
listing; the index page shows progress.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from html import escape
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "carpet"
ROOM_TYPES = ["bedroom", "living-room", "kitchen", "bathroom", "hallway", "dining-room", "other"]

DEFAULT_SEEDS: list[tuple[str, str]] = [
    ("rightmove:88045590", "two_bedrooms_carpet"),
    ("zoopla:73134473", "two_bedrooms_carpet"),
    ("rightmove:88211253", "two_bedrooms_carpet"),
    ("rightmove:88247988", "two_bedrooms_carpet"),
    ("zoopla:73089100", "two_bedrooms_carpet"),
    ("rightmove:88155948", "two_bedrooms_carpet"),
    ("zoopla:73102566", "two_bedrooms_carpet"),
    ("rightmove:173893733", "two_bedrooms_carpet_user_tiered_good"),
    ("zoopla:73124796", "two_bedrooms_carpet_user_tiered_good"),
    ("zoopla:73128487", "two_bedrooms_carpet_user_tiered_good"),
    ("zoopla:72115237", "no_carpet"),
    ("zoopla:73110416", "no_carpet"),
    ("zoopla:72037449", "one_bedroom_carpet"),
]


def safe_uid(uid: str) -> str:
    return uid.replace(":", "-").replace("/", "-")


def fixture_dir(uid: str) -> Path:
    return FIXTURE_ROOT / safe_uid(uid)


def labels_path(uid: str) -> Path:
    return fixture_dir(uid) / "labels.yaml"


def load_listing(con: sqlite3.Connection, uid: str) -> dict[str, Any] | None:
    con.row_factory = sqlite3.Row
    row = con.execute(
        """
        SELECT uid, url, title, address, postcode, area, bedrooms, images,
               carpet_in_bedroom, carpet_other_areas, score
        FROM listing
        WHERE uid = ?
        """,
        (uid,),
    ).fetchone()
    if row is None:
        return None
    images = json.loads(row["images"]) if row["images"] else []
    return {
        "uid": row["uid"],
        "url": row["url"],
        "title": row["title"] or "",
        "address": row["address"] or "",
        "postcode": row["postcode"] or "",
        "area": row["area"] or "",
        "bedrooms": row["bedrooms"],
        "image_urls": images,
        "current_carpet_bedroom": row["carpet_in_bedroom"],
        "current_carpet_other": row["carpet_other_areas"],
        "score": row["score"],
    }


async def download_listing_photos(uid: str, image_urls: list[str]) -> list[str]:
    target = fixture_dir(uid) / "photos"
    target.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for i, url in enumerate(image_urls):
            name = f"{i:02d}.jpg"
            path = target / name
            if path.exists() and path.stat().st_size > 0:
                saved.append(name)
                continue
            try:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                path.write_bytes(resp.content)
                saved.append(name)
            except Exception as exc:
                print(f"  [{uid}] failed {name}: {exc}", file=sys.stderr)
    return saved


def load_labels(uid: str) -> dict[str, Any] | None:
    p = labels_path(uid)
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text())


def save_labels(
    uid: str,
    listing: dict[str, Any],
    expected_category: str,
    labels: list[dict[str, Any]],
) -> dict[str, Any]:
    auto_counter = 0
    for entry in labels:
        if entry.get("room_type") == "bedroom" and not entry.get("bedroom_slug"):
            auto_counter += 1
            entry["bedroom_slug"] = f"bedroom_{auto_counter}"

    bedrooms_seen: set[str] = set()
    bedrooms_with_carpet: set[str] = set()
    has_living_area_carpet = False

    for entry in labels:
        rt = entry.get("room_type", "other")
        carpet = bool(entry.get("has_carpet"))
        slug = entry.get("bedroom_slug") or None
        if rt == "bedroom":
            if slug:
                bedrooms_seen.add(slug)
                if carpet:
                    bedrooms_with_carpet.add(slug)
        elif rt in {"living-room", "hallway", "dining-room"} and carpet:
            has_living_area_carpet = True

    bedrooms_seen_count = len(bedrooms_seen)
    bedrooms_with_carpet_count = len(bedrooms_with_carpet)
    if listing.get("bedrooms"):
        bedrooms_seen_count = min(bedrooms_seen_count, listing["bedrooms"])
        bedrooms_with_carpet_count = min(bedrooms_with_carpet_count, listing["bedrooms"])

    payload = {
        "listing_uid": listing["uid"],
        "listing_url": listing["url"],
        "address": listing["address"],
        "area": listing["area"],
        "bedrooms_declared": listing["bedrooms"],
        "production_score": listing["score"],
        "production_carpet_in_bedroom": listing["current_carpet_bedroom"],
        "production_carpet_other_areas": listing["current_carpet_other"],
        "expected_category": expected_category,
        "photos": labels,
        "expected": {
            "bedrooms_seen": bedrooms_seen_count,
            "bedrooms_with_carpet": bedrooms_with_carpet_count,
            "has_living_area_carpet": has_living_area_carpet,
        },
    }
    p = labels_path(listing["uid"])
    p.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    return payload


def render_index(seeds: list[tuple[str, str]], listings: dict[str, dict[str, Any]]) -> str:
    rows: list[str] = []
    labelled = 0
    for uid, category in seeds:
        listing = listings.get(uid)
        if listing is None:
            rows.append(
                f"<tr><td><code>{escape(uid)}</code></td>"
                f"<td>{escape(category)}</td>"
                f"<td colspan='4' style='color:#a00'>not in DB</td></tr>"
            )
            continue
        existing = load_labels(uid)
        status = "labelled" if existing else "not labelled"
        if existing:
            labelled += 1
            e = existing.get("expected", {})
            summary = (
                f"bedrooms_seen={e.get('bedrooms_seen')} "
                f"bedrooms_with_carpet={e.get('bedrooms_with_carpet')} "
                f"has_living_area_carpet={e.get('has_living_area_carpet')}"
            )
        else:
            summary = ""
        rows.append(
            f"<tr>"
            f"<td><code>{escape(uid)}</code></td>"
            f"<td>{escape(category)}</td>"
            f"<td>{escape(listing.get('area') or '')}</td>"
            f"<td>{listing.get('bedrooms', '')}</td>"
            f"<td>{listing.get('score', '')}</td>"
            f"<td>{escape(status)}</td>"
            f"<td><a href='/listing/{safe_uid(uid)}'>label</a></td>"
            f"<td><code>{escape(summary)}</code></td>"
            f"</tr>"
        )

    next_unlabelled = next(
        (uid for uid, _ in seeds if listings.get(uid) and not load_labels(uid)),
        None,
    )
    next_link = (
        f"<a href='/listing/{safe_uid(next_unlabelled)}'>start with {escape(next_unlabelled)}</a>"
        if next_unlabelled else "all labelled"
    )

    table = "\n".join(rows)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Carpet labelling</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 1200px; margin: 1em auto; padding: 0 1em; }}
  h1 {{ font-size: 1.2em; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9em; }}
  th, td {{ padding: 0.4em 0.6em; border-bottom: 1px solid #eee; text-align: left; }}
  th {{ background: #f5f5f5; }}
  code {{ font-family: ui-monospace, monospace; }}
  .next {{ margin: 1em 0; padding: 0.6em 1em; background: #f5f5f5; border-radius: 4px; }}
</style></head><body>
<h1>Carpet labelling — {labelled} / {len(seeds)} done</h1>
<div class='next'>Next unlabelled: {next_link}</div>
<table>
  <thead><tr>
    <th>uid</th><th>seed category</th><th>area</th><th>beds</th>
    <th>score</th><th>status</th><th></th><th>summary</th>
  </tr></thead>
  <tbody>{table}</tbody>
</table>
</body></html>
"""


def render_form(
    uid: str,
    listing: dict[str, Any],
    photo_files: list[str],
    expected_category: str,
    seeds: list[tuple[str, str]],
) -> str:
    existing = load_labels(uid)
    existing_by_file: dict[str, dict[str, Any]] = {}
    if existing:
        for entry in existing.get("photos", []):
            existing_by_file[entry.get("file")] = entry

    photo_rows: list[str] = []
    sid = safe_uid(uid)
    for i, fname in enumerate(photo_files):
        cur = existing_by_file.get(fname, {})
        cur_room = cur.get("room_type", "other")
        cur_carpet = bool(cur.get("has_carpet"))
        cur_slug = cur.get("bedroom_slug") or ""

        room_options = "".join(
            f'<option value="{rt}"{" selected" if rt == cur_room else ""}>{rt}</option>'
            for rt in ROOM_TYPES
        )
        carpet_checked = " checked" if cur_carpet else ""
        photo_rows.append(f"""
<div class="row">
  <img src="/listing/{sid}/photos/{fname}" alt="{fname}">
  <div class="controls">
    <div class="filename">{fname}</div>
    <label>Room type
      <select name="room_type__{i}">{room_options}</select>
    </label>
    <label class="carpet">
      <input type="checkbox" name="has_carpet__{i}"{carpet_checked}>
      Has carpet
    </label>
    <label>Bedroom slug (only if room is a bedroom; same slug for two angles of one bedroom)
      <input type="text" name="bedroom_slug__{i}" value="{escape(cur_slug)}" placeholder="bedroom_1">
    </label>
    <input type="hidden" name="file__{i}" value="{fname}">
  </div>
</div>""")

    expected_block = ""
    if existing and "expected" in existing:
        e = existing["expected"]
        expected_block = (
            f"<div class='expected'>Last saved: bedrooms_seen={e.get('bedrooms_seen')} "
            f"bedrooms_with_carpet={e.get('bedrooms_with_carpet')} "
            f"has_living_area_carpet={e.get('has_living_area_carpet')}</div>"
        )

    photo_grid = "".join(photo_rows)
    bedrooms_declared = listing.get("bedrooms")
    bedrooms_str = "" if bedrooms_declared is None else str(bedrooms_declared)

    pos = next((i for i, (u, _) in enumerate(seeds) if u == uid), 0)
    total = len(seeds)
    prev_uid = seeds[pos - 1][0] if pos > 0 else None
    next_uid = seeds[pos + 1][0] if pos < total - 1 else None
    prev_link = f"<a href='/listing/{safe_uid(prev_uid)}'>&larr; prev</a>" if prev_uid else ""
    next_link = f"<a href='/listing/{safe_uid(next_uid)}'>next &rarr;</a>" if next_uid else ""

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Carpet labels: {escape(uid)}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 1200px; margin: 1em auto; padding: 0 1em; }}
  h1 {{ font-size: 1.2em; }}
  .nav {{ display: flex; justify-content: space-between; margin-bottom: 1em; }}
  .nav a {{ padding: 0.4em 0.8em; background: #f5f5f5; border-radius: 4px; text-decoration: none; }}
  .meta {{ color: #555; font-size: 0.9em; margin-bottom: 1em; }}
  .row {{ display: flex; gap: 1em; padding: 0.5em; border-bottom: 1px solid #eee; align-items: flex-start; }}
  .row img {{ width: 320px; height: auto; flex-shrink: 0; }}
  .controls {{ display: flex; flex-direction: column; gap: 0.5em; flex-grow: 1; }}
  .controls label {{ display: flex; flex-direction: column; gap: 0.25em; font-size: 0.9em; color: #555; }}
  .controls label.carpet {{ flex-direction: row; gap: 0.5em; align-items: center; }}
  .controls .filename {{ font-family: ui-monospace, monospace; font-size: 0.85em; color: #888; }}
  .controls select, .controls input[type=text] {{ font-size: 1em; padding: 0.4em; }}
  .save {{ position: sticky; bottom: 0; background: #fff; padding: 1em 0; border-top: 1px solid #ddd; display: flex; gap: 1em; align-items: center; }}
  .save button {{ font-size: 1.1em; padding: 0.6em 1.2em; cursor: pointer; }}
  .save .hint {{ color: #888; font-size: 0.9em; }}
  .expected {{ font-family: ui-monospace, monospace; font-size: 0.9em; color: #555; padding: 0.5em; background: #f5f5f5; border-radius: 4px; margin-bottom: 1em; }}
  .badge {{ display: inline-block; padding: 0.2em 0.5em; background: #eef; border-radius: 4px; font-size: 0.85em; }}
</style></head><body>
<div class='nav'>
  <div>{prev_link} <a href='/'>&uarr; index</a></div>
  <div><span class='badge'>{pos + 1} / {total}</span></div>
  <div>{next_link}</div>
</div>
<h1>Carpet labels: <code>{escape(uid)}</code></h1>
<div class="meta">
  <div>{escape(listing.get('address',''))}, {escape(listing.get('area',''))}</div>
  <div>Bedrooms declared: <strong>{bedrooms_str}</strong> | Production score: <strong>{listing.get('score','')}</strong> | Seed: <code>{escape(expected_category)}</code></div>
  <div>Production carpet flags: bedroom={listing.get('current_carpet_bedroom')} other={listing.get('current_carpet_other')}</div>
  <div><a href="{escape(listing.get('url',''))}" target="_blank">Open listing on portal</a></div>
</div>
{expected_block}
<form method="POST" action="/listing/{sid}/save">
  {photo_grid}
  <div class="save">
    <button type="submit" name="action" value="save_and_next">Save and next</button>
    <button type="submit" name="action" value="save_and_stay">Save and stay</button>
    <span class="hint">Save jumps to the next unlabelled listing; "stay" reloads this page.</span>
  </div>
</form>
</body></html>
"""


def build_app(seeds: list[tuple[str, str]], listings: dict[str, dict[str, Any]]) -> FastAPI:
    app = FastAPI(title="Carpet labelling")

    safe_to_uid = {safe_uid(uid): uid for uid, _ in seeds}
    category_by_uid = dict(seeds)

    def find_next_unlabelled(after_uid: str | None) -> str | None:
        ordered = [u for u, _ in seeds if u in listings]
        start = 0
        if after_uid is not None and after_uid in ordered:
            start = ordered.index(after_uid) + 1
        for u in ordered[start:] + ordered[:start]:
            if not load_labels(u):
                return u
        return None

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(render_index(seeds, listings))

    @app.get("/listing/{sid}", response_class=HTMLResponse)
    def listing_page(sid: str) -> HTMLResponse:
        uid = safe_to_uid.get(sid)
        if uid is None or uid not in listings:
            return HTMLResponse(f"<h1>Unknown listing</h1><p>{escape(sid)}</p>", status_code=404)
        listing = listings[uid]
        photo_files = sorted(p.name for p in (fixture_dir(uid) / "photos").glob("*.jpg"))
        return HTMLResponse(render_form(uid, listing, photo_files, category_by_uid.get(uid, ""), seeds))

    @app.get("/listing/{sid}/photos/{filename}")
    def photo(sid: str, filename: str) -> FileResponse:
        uid = safe_to_uid.get(sid)
        if uid is None:
            return HTMLResponse("not found", status_code=404)
        path = fixture_dir(uid) / "photos" / filename
        if not path.exists():
            return HTMLResponse("not found", status_code=404)
        return FileResponse(str(path))

    @app.post("/listing/{sid}/save")
    async def save_endpoint(sid: str, request: Request) -> RedirectResponse:
        uid = safe_to_uid.get(sid)
        if uid is None or uid not in listings:
            return RedirectResponse(url="/", status_code=303)
        form = await request.form()
        listing = listings[uid]
        photo_files = sorted(p.name for p in (fixture_dir(uid) / "photos").glob("*.jpg"))
        labels: list[dict[str, Any]] = []
        for i, _ in enumerate(photo_files):
            file = form.get(f"file__{i}")
            if not file:
                continue
            labels.append({
                "file": file,
                "room_type": form.get(f"room_type__{i}", "other"),
                "has_carpet": form.get(f"has_carpet__{i}") is not None,
                "bedroom_slug": (form.get(f"bedroom_slug__{i}") or "").strip() or None,
            })
        save_labels(uid, listing, category_by_uid.get(uid, ""), labels)

        action = form.get("action", "save_and_stay")
        if action == "save_and_next":
            next_uid = find_next_unlabelled(uid)
            target = f"/listing/{safe_uid(next_uid)}" if next_uid else "/"
        else:
            target = f"/listing/{sid}"
        return RedirectResponse(url=target, status_code=303)

    return app


def parse_seeds(args: argparse.Namespace) -> list[tuple[str, str]]:
    if args.uids:
        return [(u.strip(), "custom") for u in args.uids.split(",") if u.strip()]
    if args.uids_file:
        return [(line.strip(), "custom") for line in Path(args.uids_file).read_text().splitlines() if line.strip()]
    return list(DEFAULT_SEEDS)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uids", default=None, help="comma-separated uids; overrides default seed list")
    parser.add_argument("--uids-file", default=None, help="newline-separated uids file")
    parser.add_argument("--port", type=int, default=8016)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data" / "homehunt.db")
    args = parser.parse_args()

    seeds = parse_seeds(args)
    print(f"Seed list: {len(seeds)} listings")

    con = sqlite3.connect(args.db)
    listings: dict[str, dict[str, Any]] = {}
    for uid, _ in seeds:
        l = load_listing(con, uid)
        if l is None:
            print(f"  [{uid}] NOT IN DB — will be skipped", file=sys.stderr)
            continue
        listings[uid] = l
    con.close()

    print(f"Resolved {len(listings)} of {len(seeds)} listings against {args.db}")
    print()
    print("Downloading photos for all listings ...")
    for uid, listing in listings.items():
        target = fixture_dir(uid) / "photos"
        existing = sorted(target.glob("*.jpg")) if target.exists() else []
        if len(existing) >= len(listing["image_urls"]):
            print(f"  [{uid}] {len(existing)} already cached, skipping")
            continue
        files = asyncio.run(download_listing_photos(uid, listing["image_urls"]))
        print(f"  [{uid}] {len(files)} photos")
    print()

    app = build_app(seeds, listings)
    print(f"Serving on http://{args.host}:{args.port}/")
    print(f"  index page: http://{args.host}:{args.port}/")
    print(f"  fixtures dir: {FIXTURE_ROOT}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
