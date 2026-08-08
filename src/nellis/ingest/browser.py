"""Strategy 4: real browser rendering (opt-in, off by default).

Only useful if the page turns out to be fully client-rendered. It runs a plain
Playwright Chromium with default settings — no stealth plugins, no
`navigator.webdriver` patching, no fingerprint spoofing. It is a slower way to
read the same public pages, not a way to look like something we aren't.

Enable with ENABLE_BROWSER_FALLBACK=true and `uv pip install -e ".[browser]"`.
"""

from __future__ import annotations

import logging

from .adapter import LotRecord, NellisAdapter, SearchFilters
from .client import PoliteClient
from .html_parse import parse_lot_cards
from .remix_json import extract_json_blobs, lots_from_blobs

log = logging.getLogger(__name__)


class BrowserAdapter(NellisAdapter):
    name = "browser"
    priority = 40

    async def _render(self, client: PoliteClient, path: str, params: dict | None = None) -> str:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                'Playwright not installed. Run: uv pip install -e ".[browser]" '
                "&& playwright install chromium"
            ) from exc

        settings = client.settings
        url = f"{settings.nellis_base_url.rstrip('/')}{path}"
        if params:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode(params)}"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent=settings.user_agent)
                page = await context.new_page()
                await page.goto(url, timeout=int(settings.request_timeout_seconds * 1000))
                await page.wait_for_load_state("networkidle")
                return await page.content()
            finally:
                await browser.close()

    def _parse(self, html: str, base_url: str) -> list[LotRecord]:
        records = lots_from_blobs(extract_json_blobs(html), base_url)
        return records or parse_lot_cards(html, base_url)

    async def search(self, client: PoliteClient, filters: SearchFilters) -> list[LotRecord]:
        if not client.settings.enable_browser_fallback:
            return []
        params = {}
        if filters.query:
            params["query"] = filters.query
        if filters.page > 1:
            params["page"] = filters.page
        html = await self._render(client, "/search", params)
        return self._parse(html, client.settings.nellis_base_url)

    async def fetch_lot(self, client: PoliteClient, lot_id: str) -> LotRecord | None:
        if not client.settings.enable_browser_fallback:
            return None
        html = await self._render(client, f"/p/{lot_id}")
        records = self._parse(html, client.settings.nellis_base_url)
        for record in records:
            if record.nellis_id == str(lot_id):
                return record
        return records[0] if records else None
