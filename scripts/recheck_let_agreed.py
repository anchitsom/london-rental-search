"""Re-check a high-value subset of listings against the let-agreed signal.

Usage:
    .venv/bin/python scripts/recheck_let_agreed.py [--dry-run] [--apply] [--limit N]

Default is dry-run: the script logs what it would do but does not transition
or update. Pass --apply to actually write.

Per row, sequential with a 1.0s delay between requests:
  - Rightmove: fetch detail HTML, parse PAGE_MODEL, call
    rightmove_api.detect_let_agreed_from_page_model.
  - Zoopla: fetch detail HTML (after seeding the session), call
    zoopla_http.detect_let_agreed_from_html.

If let-agreed is detected and --apply is set:
  - Set listing.is_let_agreed = 1 (so the drop_let_agreed filter catches it).
  - Transition the lifecycle: contact_queued / contacted -> not_contacted with
    reason already_let; NULL or new -> triage_reject with reason already_let.

Idempotent: lifecycle.transition rejects illegal repeats and the script logs
those rather than raising.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

# Resolve project root regardless of cwd. Script lives at scripts/.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from homehunt import lifecycle  # noqa: E402
from homehunt.scrapers.direct_http import _extract_page_model  # noqa: E402
from homehunt.scrapers.rightmove_api import detect_let_agreed_from_page_model  # noqa: E402
from homehunt.scrapers.zoopla_http import (  # noqa: E402
    ZooplaHTTPScraper,
    detect_let_agreed_from_html,
)


DEFAULT_DB = str(_PROJECT_ROOT / "data" / "homehunt.db")

# Rightmove direct HTTP headers, mirrors the live discover_properties caller.
_RM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
}

# Seed URL for the Zoopla session. Any London search URL works; the session
# only needs Cloudflare cookies before detail fetches are attempted.
_ZOOPLA_SEED_URL = (
    "https://www.zoopla.co.uk/to-rent/property/london/islington/"
    "?beds_max=2&beds_min=1&price_frequency=per_month&price_max=2800"
    "&q=Islington%2C%20London&search_source=for-rent&view_type=list"
)

# Per-request throttle.
_REQUEST_DELAY = 1.0


TARGET_QUERY = """
    SELECT uid, portal, url, current_status, score
    FROM listing
    WHERE is_active = 1
      AND (
        current_status IN ('contact_queued', 'contacted')
        OR (
          (current_status IS NULL OR current_status = 'new')
          AND score >= 85
        )
      )
    ORDER BY portal, score DESC NULLS LAST, uid
"""


def _portal_norm(portal: Optional[str]) -> str:
    return (portal or "").strip().lower()


def _planned_to_status(current_status: Optional[str]) -> Optional[str]:
    """Map the current lifecycle state to the target transition state.

    Returns None if the listing falls outside the target set (defensive: the
    SQL already filters, this is belt-and-braces).
    """
    if current_status in ("contact_queued", "contacted"):
        return "not_contacted"
    if current_status is None or current_status == "new":
        return "triage_reject"
    return None


async def _fetch_rightmove(url: str, client: httpx.AsyncClient) -> Optional[dict]:
    """Fetch a Rightmove detail page and return the parsed PAGE_MODEL dict.

    Returns None on HTTP error or missing PAGE_MODEL. A 404 / 410 is treated
    as a hard miss; callers can fall back to status-based inference if they
    wish, but this script returns None and the row stays unchanged on the
    let-agreed front (a 404 is also a "no longer available" signal but the
    let-agreed re-check scope is narrow on purpose).
    """
    try:
        resp = await client.get(url, timeout=20.0)
    except Exception:
        return None
    if resp.status_code != 200:
        # A 410 means the Rightmove listing is archived. Surface that as a
        # let-agreed-equivalent: build a synthetic dict the detector reads as
        # archived=True. Keeps the rest of the pipeline simple.
        if resp.status_code in (404, 410):
            return {"propertyData": {"status": {"published": False, "archived": True}}}
        return None
    return _extract_page_model(resp.text)


async def _fetch_zoopla(url: str, scraper: ZooplaHTTPScraper) -> Optional[str]:
    """Fetch a Zoopla detail page and return its HTML, or None on failure."""
    try:
        result = await scraper.scrape_property(url)
    except Exception:
        return None
    if not result.success:
        # `property_removed` is a let-agreed equivalent on Zoopla too -- the
        # listing redirected away. Return a marker that the detector reads
        # as let-agreed.
        if result.error == "property_removed":
            return (
                '<html><body>'
                '<div class="Badges_galleryBadge">Let agreed</div>'
                '</body></html>'
            )
        return None
    return (result.data or {}).get("raw_content")


def _log_row(uid: str, portal: str, score, status, detected: bool, action: str) -> None:
    print(
        f"uid={uid} portal={portal} score={score} status={status} "
        f"let_agreed={detected} action={action}"
    )


def _apply_changes(
    db_path: str,
    uid: str,
    to_status: str,
) -> str:
    """Mark the listing let-agreed and transition. Returns action description.

    Catches IllegalTransition and reports it as a warning string. Idempotent:
    a second run sees the listing already terminal and the transition is
    rejected.
    """
    # Stamp is_let_agreed first so the next scrape's drop_let_agreed filter
    # catches the row regardless of whether the transition succeeded.
    con = sqlite3.connect(db_path)
    try:
        con.execute("UPDATE listing SET is_let_agreed = 1 WHERE uid = ?", (uid,))
        con.commit()
    finally:
        con.close()

    try:
        lifecycle.transition(
            uid=uid,
            to_status=to_status,
            reason="already_let",
            transitioned_by="recheck_let_agreed_script",
            db_path=db_path,
        )
        return f"transition->{to_status}"
    except lifecycle.IllegalTransition as exc:
        print(f"  WARN illegal_transition uid={uid}: {exc}")
        return f"is_let_agreed=1; skipped illegal transition->{to_status}"


async def run(db_path: str, dry_run: bool, limit: Optional[int]) -> None:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        sql = TARGET_QUERY
        if limit is not None:
            sql = sql + f" LIMIT {int(limit)}"
        rows = con.execute(sql).fetchall()
    finally:
        con.close()

    n_total = len(rows)
    n_let_agreed = 0
    n_applied = 0

    rm_client = httpx.AsyncClient(headers=_RM_HEADERS, follow_redirects=True)
    zoopla_scraper = ZooplaHTTPScraper()
    zoopla_seeded = False

    try:
        for i, row in enumerate(rows):
            uid = row["uid"]
            portal = _portal_norm(row["portal"])
            url = row["url"]
            status = row["current_status"]
            score = row["score"]

            detected = False
            try:
                if portal == "rightmove":
                    pm = await _fetch_rightmove(url, rm_client)
                    detected = detect_let_agreed_from_page_model(pm)
                elif portal == "zoopla":
                    if not zoopla_seeded:
                        try:
                            await zoopla_scraper.scrape_search_page(_ZOOPLA_SEED_URL)
                            zoopla_seeded = True
                        except Exception as exc:
                            print(f"  WARN zoopla_seed_failed: {exc}")
                    html = await _fetch_zoopla(url, zoopla_scraper)
                    detected = detect_let_agreed_from_html(html)
                else:
                    _log_row(uid, portal, score, status, False, "skip_unknown_portal")
                    continue
            except Exception as exc:
                _log_row(uid, portal, score, status, False, f"error:{exc}")
                continue

            if not detected:
                _log_row(uid, portal, score, status, detected, "skip")
            else:
                n_let_agreed += 1
                target = _planned_to_status(status)
                if target is None:
                    _log_row(uid, portal, score, status, detected, "skip_no_target")
                elif dry_run:
                    _log_row(
                        uid, portal, score, status, detected,
                        f"dry_run_would_transition->{target}",
                    )
                else:
                    action = _apply_changes(db_path, uid, target)
                    _log_row(uid, portal, score, status, detected, action)
                    if action.startswith("transition->"):
                        n_applied += 1

            if i < n_total - 1:
                await asyncio.sleep(_REQUEST_DELAY)
    finally:
        await rm_client.aclose()
        await zoopla_scraper.close()

    print(
        f"Processed: {n_total}. Let-agreed: {n_let_agreed}. "
        f"Transitions applied: {n_applied}."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="(default) Log what would happen, do not write.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes. Without this, the script is read-only.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of listings to re-check (default: all in target set).",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"Path to homehunt.db (default: {DEFAULT_DB}).",
    )
    args = parser.parse_args()

    # Default is dry-run unless --apply was passed explicitly.
    dry_run = not args.apply

    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}", file=sys.stderr)
        sys.exit(2)

    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"Mode: {mode}. DB: {args.db}. Limit: {args.limit or 'all'}.")
    asyncio.run(run(args.db, dry_run=dry_run, limit=args.limit))


if __name__ == "__main__":
    main()
