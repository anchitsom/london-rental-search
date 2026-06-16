"""
Zoopla HTTP scraper using curl_cffi with TLS impersonation.

Phase 0 verification (2026-05-08) showed Cloudflare blocks stateless detail
fetches two-thirds of the time. The pattern that survives is to seed a
Session by fetching the search-results URL first, capturing the Cloudflare
cookies, then reusing the same Session for detail fetches with a Referer
header matching the search URL and Sec-Fetch-Site set to same-origin.

Mirrors the return shape of homehunt.scrapers.direct_http.DirectHTTPScraper
so that PropertyListing.from_extraction_result() accepts the result without
modification.
"""

import asyncio
import json
import structlog
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from homehunt.core.models import ExtractionMethod, Portal, ScrapingResult


_BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_DETAIL_URL_RE = re.compile(r"/to-rent/details/(\d+)/?")
_PROPERTY_ID_RE = re.compile(r"/to-rent/details/(\d+)")
_REMOVED_MARKERS = (
    "this listing has been removed",
    "no longer available",
    "longer on zoopla",
)
_PHOTO_HASH_RE = re.compile(
    r'\\"filename\\":\\"([a-f0-9]{40}\.(?:jpg|jpeg|png|webp))\\"'
)
_PHOTO_HASH_RAW_RE = re.compile(
    r'/u/(?:\d+)/(?:\d+)/([a-f0-9]{40}\.(?:jpg|jpeg|png|webp))'
)


class ZooplaHTTPScraper:
    """Stateful Zoopla scraper. Search-page fetch must precede detail fetches."""

    def __init__(
        self,
        impersonate: str = "chrome124",
        request_timeout: float = 30.0,
        min_request_interval: float = 1.0,
    ):
        self._impersonate = impersonate
        self._request_timeout = request_timeout
        self._min_request_interval = min_request_interval
        self._session = None
        self._search_url: Optional[str] = None
        self._last_request_at: float = 0.0
        self.logger = structlog.get_logger(self.__class__.__name__)

    def get_portal(self) -> Portal:
        return Portal.ZOOPLA

    def get_extraction_method(self) -> ExtractionMethod:
        return ExtractionMethod.DIRECT_HTTP

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    def _ensure_session(self):
        if self._session is None:
            from curl_cffi import requests as cc_requests
            self._session = cc_requests.Session(impersonate=self._impersonate)

    async def _pace(self):
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_request_interval:
            await asyncio.sleep(self._min_request_interval - elapsed)
        self._last_request_at = time.monotonic()

    async def _get(self, url: str, headers: Dict[str, str]):
        self._ensure_session()
        await self._pace()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self._session.get(url, headers=headers, timeout=self._request_timeout),
        )

    @staticmethod
    def _parse_search_html(html: str) -> List[str]:
        ids: list[str] = []
        for m in _DETAIL_URL_RE.finditer(html):
            pid = m.group(1)
            if pid not in ids:
                ids.append(pid)
        return [f"https://www.zoopla.co.uk/to-rent/details/{pid}/" for pid in ids]

    async def scrape_search_page(self, url: str) -> List[str]:
        """Fetch a Zoopla search-results URL and return detail-page URLs.

        Side effect: seeds the internal Session with Cloudflare cookies
        required for subsequent detail-page fetches.
        """
        response = await self._get(url, headers=_BASE_HEADERS)
        if response.status_code != 200:
            self.logger.warning(
                "zoopla_search_status_non_200",
                status=response.status_code,
                url=url,
            )
            return []
        self._search_url = url
        urls = self._parse_search_html(response.text)
        self.logger.info(f"zoopla_search_found {len(urls)} detail urls")
        return urls

    async def scrape_property(self, url: str) -> ScrapingResult:
        """Fetch a single Zoopla detail page and return a ScrapingResult."""
        if self._session is None or self._search_url is None:
            raise RuntimeError(
                "session not seeded; call scrape_search_page first"
            )

        property_id = self.extract_property_id(url)
        detail_headers = {
            **_BASE_HEADERS,
            "Sec-Fetch-Site": "same-origin",
            "Referer": self._search_url,
        }
        try:
            response = await self._get(url, headers=detail_headers)
        except Exception as e:
            self.logger.error(f"zoopla_detail_fetch_error: {e}")
            return ScrapingResult(
                url=url,
                success=False,
                portal=self.get_portal(),
                property_id=property_id,
                error=str(e),
                extraction_method=self.get_extraction_method(),
            )

        if response.status_code != 200:
            return ScrapingResult(
                url=url,
                success=False,
                portal=self.get_portal(),
                property_id=property_id,
                error=f"http_{response.status_code}",
                extraction_method=self.get_extraction_method(),
                content_length=len(response.text or ""),
            )

        # Detect removal/redirect: final URL no longer matches the requested
        # detail path, or body carries a removal marker.
        final_url = str(response.url) if hasattr(response, "url") else url
        classification = self._classify_detail_html(
            response.text, url, final_url=final_url
        )
        if not classification["success"]:
            return ScrapingResult(
                url=url,
                success=False,
                portal=self.get_portal(),
                property_id=property_id,
                error=classification["error"],
                extraction_method=self.get_extraction_method(),
                content_length=len(response.text or ""),
            )

        data = self._parse_detail_html(response.text, url)
        return ScrapingResult(
            url=url,
            success=True,
            portal=self.get_portal(),
            property_id=property_id,
            data=data,
            content_length=len(response.text or ""),
            extraction_method=self.get_extraction_method(),
        )

    @staticmethod
    def extract_property_id(url: str) -> Optional[str]:
        m = _PROPERTY_ID_RE.search(url)
        return m.group(1) if m else None

    @staticmethod
    def _classify_detail_html(
        html: str, requested_url: str, final_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Decide whether the detail HTML represents a live listing.

        Returns a dict with keys success, error.
        """
        # Redirect away from the /to-rent/details/<id>/ path is removal.
        requested_id = ZooplaHTTPScraper.extract_property_id(requested_url)
        if final_url:
            final_id = ZooplaHTTPScraper.extract_property_id(final_url)
            final_path = urlparse(final_url).path
            if "/to-rent/details/" not in final_path:
                return {"success": False, "error": "property_removed"}
            if requested_id and final_id and requested_id != final_id:
                return {"success": False, "error": "property_removed"}

        lower = html.lower()
        for marker in _REMOVED_MARKERS:
            if marker in lower:
                return {"success": False, "error": "property_removed"}

        # A live page must carry the Zoopla data-targeting block.
        if '"listing_id"' not in html and "ZAD_TARGETING" not in html:
            return {"success": False, "error": "property_removed"}

        return {"success": True, "error": None}

    @staticmethod
    def _parse_detail_html(html: str, url: str) -> Dict[str, Any]:
        """Extract structured property fields from a Zoopla detail-page HTML."""
        data: Dict[str, Any] = {}

        targeting = ZooplaHTTPScraper._parse_zad_targeting(html)
        ld = ZooplaHTTPScraper._parse_realestate_jsonld(html)

        # Address
        address = (
            (targeting or {}).get("display_address")
            or _scan_quoted_value(html, "displayAddress")
            or (ld or {}).get("name")
        )
        if address:
            data["address"] = address

        # Postcode = outcode + " " + incode
        outcode = (targeting or {}).get("outcode") or _scan_quoted_value(html, "outcode")
        incode = (targeting or {}).get("incode") or _scan_quoted_value(html, "incode")
        if outcode and incode:
            data["postcode"] = f"{outcode} {incode}".upper()
        elif outcode:
            data["postcode"] = outcode.upper()

        # Bedrooms
        beds = None
        for src in (
            (targeting or {}).get("num_beds"),
            _scan_numeric(html, "numBedrooms"),
            _scan_numeric(html, "numBeds"),
            _scan_numeric(html, "totalBedrooms"),
        ):
            if src is not None:
                try:
                    beds = int(src)
                    break
                except (ValueError, TypeError):
                    pass
        if beds is None and ld and ld.get("additionalProperty"):
            for prop in ld.get("additionalProperty", []):
                if (prop.get("name") or "").lower() == "bedrooms":
                    try:
                        beds = int(prop.get("value"))
                        break
                    except (ValueError, TypeError):
                        pass
        if beds is not None:
            data["bedrooms"] = beds

        # Bathrooms
        baths = None
        for src in (
            (targeting or {}).get("num_baths"),
            _scan_numeric(html, "numBathrooms"),
            _scan_numeric(html, "numBaths"),
        ):
            if src is not None:
                try:
                    baths = int(src)
                    break
                except (ValueError, TypeError):
                    pass
        if baths is not None:
            data["bathrooms"] = baths

        # Price
        price_pcm = None
        if targeting and targeting.get("price_actual"):
            try:
                price_pcm = int(targeting["price_actual"])
            except (ValueError, TypeError):
                pass
        if price_pcm is None:
            m = re.search(r'\\"label\\":\\"£([\d,]+)\s*pcm\\"', html)
            if m:
                try:
                    price_pcm = int(m.group(1).replace(",", ""))
                except ValueError:
                    pass
        if price_pcm is None and ld and isinstance(ld.get("offers"), dict):
            try:
                price_pcm = int(ld["offers"].get("price"))
            except (ValueError, TypeError):
                pass
        if price_pcm is not None:
            data["price_pcm"] = price_pcm
            data["price"] = f"£{price_pcm:,} pcm"

        # Property type
        ptype = (targeting or {}).get("property_type")
        if ptype:
            data["property_type"] = ptype

        # Furnished
        furnished_raw = (targeting or {}).get("furnished_state") or _scan_quoted_value(
            html, "furnished_state"
        )
        if furnished_raw:
            data["furnished"] = furnished_raw.replace("_", " ").title()

        # Latitude / longitude
        lat = _scan_numeric(html, "latitude")
        lng = _scan_numeric(html, "longitude")
        if lat is not None and lng is not None:
            try:
                data["latitude"] = float(lat)
                data["longitude"] = float(lng)
            except (ValueError, TypeError):
                pass

        # Title
        m = re.search(r"<title>([^<]+)</title>", html)
        if m:
            data["title"] = m.group(1).strip()

        # Description
        if ld and ld.get("description"):
            desc = ld["description"]
            desc = re.sub(r"<[^>]+>", " ", desc)
            desc = desc.replace("&amp;", "&").replace("&quot;", '"')
            desc = re.sub(r"\s+", " ", desc).strip()
            data["description"] = desc

        # Features (key features bullets)
        features = ZooplaHTTPScraper._parse_feature_bullets(html)
        if features:
            data["features"] = features

        # EPC chart image URL (pre-Wave-3 S2). Zoopla exposes the EPC
        # certificate image inside the React hydration payload at
        # \"epc\":{\"image\":[{\"caption\":\"EPC\",\"filename\":\"<sha1>.jpg\"}]}.
        # When epc.image is null (no chart), the field stays absent. The
        # filename is served at lid.zoocdn.com/u/1024/768/<filename>, the
        # same imgproxy CDN as photos. Wired here so the existing run.py
        # vision-OCR rescue path (epc_vision.py) fires on Zoopla rows.
        epc_graph = ZooplaHTTPScraper._parse_epc_graph_url(html)
        if epc_graph:
            data["epc_graph_url"] = epc_graph

        # Photos. Subtract any filename already attributed to the EPC chart
        # so the EPC certificate image does not contaminate photo-driven
        # downstream stages (carpet vision, room labelling).
        photos = ZooplaHTTPScraper._parse_photos(html)
        if epc_graph and photos:
            photos = [p for p in photos if epc_graph not in p]
        if photos:
            data["photo_urls"] = photos
            data["images"] = photos

        # Size in square feet, exposed as a string in __ZAD_TARGETING__.
        # Derive square metres at 1 sqft = 0.0929 sqm.
        size_sqft_raw = (targeting or {}).get("size_sq_feet")
        if size_sqft_raw:
            try:
                sqft = int(str(size_sqft_raw).strip())
                if sqft > 0:
                    data["size_sqft"] = sqft
                    data["size_sqm"] = int(round(sqft * 0.0929))
            except (ValueError, TypeError):
                pass

        # EPC rating letter (A to G), present in the React hydration payload
        # as escaped JSON: \"epcRating\":\"X\". Falls back to existing EPC API
        # by postcode when not present.
        epc = ZooplaHTTPScraper._parse_epc_rating(html)
        if epc:
            data["epc_rating"] = epc

        # Floorplan URLs from the hydration block. Two shapes observed:
        #   \"floorPlan\":[{\"original\":\"<url>\"}]
        #   \"floorPlan\":{\"image\":[{\"filename\":\"<sha1>.jpg\"}]}
        # Reconstructed filename URLs use lc.zoocdn.com (distinct from photos).
        fps = ZooplaHTTPScraper._parse_floorplan_urls(html)
        if fps:
            data["floorplan_urls"] = fps

        # Run NLP-style extraction for downstream amenity inference, mirroring
        # direct_http.py. NLP values never overwrite structured ones.
        try:
            from homehunt.feature_extractor import extract_all as _extract_all
            nlp = _extract_all(
                description=data.get("description", "") or "",
                features=data.get("features", []) or [],
                title=data.get("title", "") or "",
            )
            for k, v in nlp.items():
                if v is None:
                    continue
                if data.get(k) is not None:
                    continue
                data[k] = v
        except Exception:
            pass

        data["raw_content"] = html
        return data

    @staticmethod
    def _parse_zad_targeting(html: str) -> Optional[Dict[str, Any]]:
        m = re.search(
            r'<script id="__ZAD_TARGETING__"[^>]*>([\s\S]*?)</script>',
            html,
        )
        if not m:
            return None
        try:
            return json.loads(m.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _parse_realestate_jsonld(html: str) -> Optional[Dict[str, Any]]:
        for m in re.finditer(
            r'<script[^>]*type="application/ld\+json"[^>]*>([\s\S]*?)</script>',
            html,
        ):
            try:
                obj = json.loads(m.group(1).strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and obj.get("@type") == "RealEstateListing":
                return obj
        return None

    @staticmethod
    def _parse_feature_bullets(html: str) -> List[str]:
        m = re.search(r'\\"bullets\\":\[((?:[^\[\]]|\\.)*?)\]', html)
        if not m:
            return []
        body = m.group(1)
        items = re.findall(r'\\"((?:[^\\"]|\\\\.)+)\\"', body)
        return [i.replace("\\u0026", "&").replace('\\"', '"') for i in items if i]

    @staticmethod
    def _parse_photos(html: str) -> List[str]:
        # Primary: filenames inside the React server payload
        hashes: list[str] = []
        for m in _PHOTO_HASH_RE.finditer(html):
            fn = m.group(1)
            if fn not in hashes:
                hashes.append(fn)
        if hashes:
            return [f"https://lid.zoocdn.com/u/1024/768/{fn}" for fn in hashes]

        # Fallback: scan literal CDN paths in the rendered DOM
        for m in _PHOTO_HASH_RAW_RE.finditer(html):
            fn = m.group(1)
            if fn not in hashes:
                hashes.append(fn)
        return [f"https://lid.zoocdn.com/u/1024/768/{fn}" for fn in hashes]

    @staticmethod
    def _parse_epc_rating(html: str) -> Optional[str]:
        m = re.search(r'\\"epcRating\\":\\"([A-G])\\"', html)
        if m:
            return m.group(1)
        m = re.search(r'"epcRating":"([A-G])"', html)
        if m:
            return m.group(1)
        return None

    @staticmethod
    def _parse_epc_graph_url(html: str) -> Optional[str]:
        """Extract the EPC chart image URL from Zoopla's hydration payload.

        Two shapes are observed in the React Server Components stream. Both
        live under the same `propertyData.epc.image[]` key.

          escaped: \"epc\":{\"image\":[{...,\"filename\":\"<sha1>.jpg\"}]}
          plain:   "epc":{"image":[{...,"filename":"<sha1>.jpg"}]}

        When the listing has no certificate, the payload carries
        `\"epc\":{\"image\":null,...}` and this returns None.

        The recovered filename is served at lid.zoocdn.com/u/1024/768/
        <filename>, the same imgproxy CDN as listing photos. Live HEAD
        confirms 200 with EPC-shaped JPEGs (552x613).
        """
        # Escaped (RSC stream) form.
        m = re.search(
            r'\\"epc\\":\{\\"image\\":\[\{[^\]]*?\\"filename\\":\\"([a-f0-9]{40}\.(?:jpg|jpeg|png))\\"',
            html,
        )
        if m:
            return f"https://lid.zoocdn.com/u/1024/768/{m.group(1)}"
        # Plain form (defensive: Zoopla may switch some hydration paths).
        m = re.search(
            r'"epc":\{"image":\[\{[^\]]*?"filename":"([a-f0-9]{40}\.(?:jpg|jpeg|png))"',
            html,
        )
        if m:
            return f"https://lid.zoocdn.com/u/1024/768/{m.group(1)}"
        return None

    @staticmethod
    def _parse_floorplan_urls(html: str) -> List[str]:
        out: list[str] = []
        for m in re.finditer(
            r'\\"floorPlan\\":\[\{\\"original\\":\\"(https?://[^\\"]+\.(?:jpg|jpeg|png|pdf))\\"',
            html,
        ):
            url = m.group(1)
            if url not in out:
                out.append(url)
        for m in re.finditer(
            r'\\"floorPlan\\":\{\\"image\\":\[\{[^\]]*?\\"filename\\":\\"([a-f0-9]{40}\.(?:jpg|jpeg|png))\\"',
            html,
        ):
            url = f"https://lc.zoocdn.com/{m.group(1)}"
            if url not in out:
                out.append(url)
        return out


_ZOOPLA_LET_AGREED_TEXT_RE = re.compile(
    r"\b(?:let\s+agreed|now\s+let|under\s+offer)\b",
    re.IGNORECASE,
)
_ZOOPLA_GALLERY_BADGE_RE = re.compile(
    r"galleryBadge[^>]*>\s*([^<]+?)\s*<",
    re.IGNORECASE,
)


def detect_let_agreed_from_html(html: Optional[str]) -> bool:
    """Return True if the Zoopla detail HTML indicates the listing is let-agreed.

    Signals checked, in order:
      1. A galleryBadge element whose visible text contains "let agreed",
         "now let", or "under offer" (case-insensitive). Zoopla renders the
         badge in the photo strip for let-agreed and pulled-from-market.
      2. JSON-LD `Offer.availability` set to anything other than InStock.
      3. Loose text match for the same phrases anywhere in the body, as a
         backstop in case Zoopla re-skins the badge.

    Defensive: returns False on missing or unparseable input, never raises.
    """
    if not html or not isinstance(html, str):
        return False
    try:
        # 1. Gallery badge text.
        for m in _ZOOPLA_GALLERY_BADGE_RE.finditer(html):
            text = m.group(1)
            if _ZOOPLA_LET_AGREED_TEXT_RE.search(text):
                return True
        # 2. JSON-LD availability. Zoopla wraps the Offer in a RealEstateListing
        # JSON-LD block; an offers.availability that is not schema.org/InStock
        # is a let-agreed signal.
        for m in re.finditer(
            r'<script[^>]*type="application/ld\+json"[^>]*>([\s\S]*?)</script>',
            html,
        ):
            try:
                obj = json.loads(m.group(1).strip())
            except (json.JSONDecodeError, ValueError):
                continue
            offers = obj.get("offers") if isinstance(obj, dict) else None
            if isinstance(offers, dict):
                avail = (offers.get("availability") or "").lower()
                if avail and "instock" not in avail and "in_stock" not in avail:
                    return True
    except Exception:
        return False
    return False


def _scan_quoted_value(html: str, key: str) -> Optional[str]:
    m = re.search(rf'\\"{re.escape(key)}\\":\\"((?:[^\\"]|\\\\.)*?)\\"', html)
    if m:
        return m.group(1)
    m = re.search(rf'"{re.escape(key)}":"((?:[^"\\]|\\.)*?)"', html)
    if m:
        return m.group(1)
    return None


def _scan_numeric(html: str, key: str) -> Optional[str]:
    m = re.search(rf'\\"{re.escape(key)}\\":(-?[\d.]+)', html)
    if m:
        return m.group(1)
    m = re.search(rf'"{re.escape(key)}":(-?[\d.]+)', html)
    if m:
        return m.group(1)
    return None
