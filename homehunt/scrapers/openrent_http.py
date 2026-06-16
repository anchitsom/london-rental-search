"""OpenRent HTTP transport.

Plain httpx + Chrome User-Agent reaches OpenRent without curl_cffi or Cloudflare
mitigations (probe REPORT 2026-05-23). One outbound rate limit governs both
search and detail fetches: max 1 request per second per instance, matching the
empirical envelope from probe Task 8 (5/5 success at 1s spacing).
"""

from typing import Tuple

import httpx

from homehunt.core.models import ExtractionMethod, Portal
from homehunt.scrapers.base import RateLimiter

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class OpenRentHTTP:
    """Async OpenRent HTTP client with a 1s outbound rate limit."""

    def __init__(self, request_timeout: float = 30.0, min_request_interval: float = 1.0):
        self._rate_limiter = RateLimiter(
            max_requests=1,
            time_window=max(1, int(min_request_interval)),
            max_concurrent=1,
        )
        self._request_timeout = request_timeout
        self._client: httpx.AsyncClient | None = None

    def get_portal(self) -> Portal:
        return Portal.OPENRENT

    def get_extraction_method(self) -> ExtractionMethod:
        return ExtractionMethod.DIRECT_HTTP

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            headers={"User-Agent": _UA},
            timeout=self._request_timeout,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch_search(self, url: str) -> Tuple[int, str]:
        await self._rate_limiter.acquire()
        assert self._client is not None, "use as async context manager"
        resp = await self._client.get(url)
        return resp.status_code, resp.text

    async def fetch_detail(self, property_id: int | str) -> Tuple[int, str]:
        await self._rate_limiter.acquire()
        assert self._client is not None, "use as async context manager"
        resp = await self._client.get(f"https://www.openrent.co.uk/{property_id}")
        return resp.status_code, resp.text
