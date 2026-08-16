"""Polite HTTP client.

Politeness policy — deliberate, and not to be "optimized" away:

  * obey robots.txt
  * one connection, serialized requests, fixed delay + jitter
  * ordinary desktop User-Agent, sent honestly and consistently
  * hard STOP on 403/429 — if the site signals "back off", the run ends
  * hard STOP on repeated 500s too: Nellis uses 500 for rate limiting rather
    than 429, and that throttle is network-wide — pushing through it locks you
    out of your own account in the browser, not just the scraper
  * per-run request budget so a bug cannot turn into a hammering loop
  * on-disk response cache so re-running analysis costs zero requests

There is deliberately no fingerprint spoofing, proxy rotation, CAPTCHA solving,
or stealth-browser patching here. If this client gets blocked, the fix is to
slow it down, not to hide it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from ..config import Settings, get_settings

log = logging.getLogger(__name__)


class IngestError(RuntimeError):
    """Base class for ingestion failures."""


class BlockedError(IngestError):
    """The site asked us to stop (403/429). We stop."""


class BudgetExhausted(IngestError):
    """Per-run request budget spent."""


class RobotsDisallowed(IngestError):
    """robots.txt disallows this path."""


@dataclass
class Fetched:
    url: str
    status: int
    text: str
    from_cache: bool = False

    def json(self) -> Any:
        return json.loads(self.text)


class ResponseCache:
    """Content-addressed disk cache. Keeps re-analysis free."""

    def __init__(self, root: Path, ttl_seconds: int):
        self.root = root
        self.ttl = ttl_seconds
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.root / digest[:2] / f"{digest}.json"

    def get(self, key: str) -> Fetched | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        if time.time() - payload.get("ts", 0) > self.ttl:
            return None
        return Fetched(
            url=payload["url"], status=payload["status"], text=payload["text"], from_cache=True
        )

    def put(self, key: str, fetched: Fetched) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "ts": time.time(),
                    "url": fetched.url,
                    "status": fetched.status,
                    "text": fetched.text,
                }
            )
        )


class PoliteClient:
    """Serialized, rate-limited, cache-backed HTTP client."""

    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None):
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None
        self._last_request_at: float = 0.0
        self._requests_made = 0
        self._consecutive_server_errors = 0
        self._robots: urllib.robotparser.RobotFileParser | None = None
        self._robots_loaded = False
        self._lock = asyncio.Lock()
        self.cache = ResponseCache(self.settings.cache_dir, self.settings.cache_ttl_seconds)

    async def __aenter__(self) -> PoliteClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
                follow_redirects=True,
                headers=self._base_headers(),
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    # -- politeness ------------------------------------------------------

    async def _load_robots(self) -> None:
        if self._robots_loaded or not self.settings.respect_robots_txt:
            self._robots_loaded = True
            return
        self._robots_loaded = True
        robots_url = urljoin(self.settings.nellis_base_url, "/robots.txt")
        parser = urllib.robotparser.RobotFileParser()
        try:
            assert self._client is not None
            resp = await self._client.get(robots_url)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
                self._robots = parser
                log.info("robots.txt loaded from %s", robots_url)
            else:
                log.warning("robots.txt returned %s; proceeding conservatively", resp.status_code)
        except httpx.HTTPError as exc:
            log.warning("robots.txt unreachable (%s); proceeding conservatively", exc)

    def _robots_allows(self, url: str) -> bool:
        if not self.settings.respect_robots_txt or self._robots is None:
            return True
        return self._robots.can_fetch(self.settings.user_agent, url)

    async def _throttle(self) -> None:
        delay = self.settings.request_delay_seconds + random.uniform(
            0, self.settings.request_jitter_seconds
        )
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self._last_request_at = time.monotonic()

    # -- fetching ---------------------------------------------------------

    async def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        use_cache: bool = True,
        accept_json: bool = False,
    ) -> Fetched:
        if not urlparse(url).scheme:
            url = urljoin(self.settings.nellis_base_url, url)

        cache_key = f"{url}?{sorted((params or {}).items())}"
        if use_cache:
            hit = self.cache.get(cache_key)
            if hit is not None:
                log.debug("cache hit %s", url)
                return hit

        async with self._lock:
            await self._load_robots()
            if not self._robots_allows(url):
                raise RobotsDisallowed(f"robots.txt disallows {url}")
            if self._requests_made >= self.settings.max_requests_per_run:
                raise BudgetExhausted(
                    f"request budget of {self.settings.max_requests_per_run} exhausted"
                )
            await self._throttle()
            self._requests_made += 1

            assert self._client is not None
            headers = {}
            if accept_json:
                headers["Accept"] = "application/json, text/plain, */*"
            try:
                resp = await self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise IngestError(f"request failed for {url}: {exc}") from exc

        if resp.status_code in (403, 429):
            raise BlockedError(
                f"HTTP {resp.status_code} from {url} — the site asked us to back off. "
                "Stopping this run. Increase REQUEST_DELAY_SECONDS before retrying."
            )

        # Nellis signals rate limiting with 500, not 429. Their help centre is
        # explicit: "Code 500 errors typically occur when too many requests come
        # from the same network." Treating 500 as an ordinary retryable server
        # error would let a scan keep hammering the very throttle it tripped —
        # and that throttle is network-wide, so it locks you out of your own
        # account in the browser too, not just the scraper.
        if resp.status_code >= 500:
            self._consecutive_server_errors += 1
            if self._consecutive_server_errors >= self.settings.server_error_stop_threshold:
                raise BlockedError(
                    f"HTTP {resp.status_code} from {url}, "
                    f"{self._consecutive_server_errors} in a row — on Nellis this means "
                    "rate limiting, not an outage. Stopping this run.\n"
                    "Wait ~15 minutes before retrying, raise REQUEST_DELAY_SECONDS, "
                    "and close other tabs/devices hitting Nellis on this network. "
                    "A VPN (especially non-US) also triggers it."
                )
            raise IngestError(
                f"HTTP {resp.status_code} from {url} "
                f"({self._consecutive_server_errors} consecutive; "
                f"stopping at {self.settings.server_error_stop_threshold})"
            )

        self._consecutive_server_errors = 0

        fetched = Fetched(url=str(resp.url), status=resp.status_code, text=resp.text)
        if use_cache and resp.status_code == 200:
            self.cache.put(cache_key, fetched)
        return fetched

    @property
    def requests_made(self) -> int:
        return self._requests_made
