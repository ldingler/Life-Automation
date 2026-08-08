"""eBay comps via the official API.

Important limitation, confirmed during research: eBay's **Marketplace Insights**
API (true SOLD prices) is restricted to approved partners. Without that grant
we can only read the **Browse** API, which returns *active* listings — i.e.
asking prices, which run optimistic.

We handle that honestly rather than pretending otherwise:
  * Browse results are marked `is_sold=False`
  * the aggregator applies a haircut and a lower source weight to them
  * if you are later granted Insights, set EBAY_USE_INSIGHTS=true and the sold
    endpoint is used automatically with full weight

Credentials are optional — with none configured this provider disables itself.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx

from ...config import Settings
from ...normalize import search_query
from .base import CompPoint, CompsProvider, ItemQuery, score_similarity

log = logging.getLogger(__name__)

OAUTH_URL = "https://api.ebay.com/identity/oauth2/token"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
INSIGHTS_URL = "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"


class EbayProvider(CompsProvider):
    name = "ebay"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self.enabled = bool(settings.ebay_client_id and settings.ebay_client_secret)
        self.use_insights = bool(getattr(settings, "ebay_use_insights", False))

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def _access_token(self) -> str | None:
        if not self.enabled:
            return None
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        credentials = f"{self.settings.ebay_client_id}:{self.settings.ebay_client_secret}"
        encoded = base64.b64encode(credentials.encode()).decode()
        http = await self._http()
        try:
            response = await http.post(
                OAUTH_URL,
                headers={
                    "Authorization": f"Basic {encoded}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials", "scope": SCOPE},
            )
        except httpx.HTTPError as exc:
            log.warning("eBay auth request failed: %s", exc)
            return None

        if response.status_code != 200:
            log.warning("eBay auth failed (%s): %s", response.status_code, response.text[:200])
            self.enabled = False
            return None

        payload = response.json()
        self._token = payload.get("access_token")
        self._token_expires_at = time.time() + float(payload.get("expires_in", 7200))
        return self._token

    async def fetch(self, query: ItemQuery, *, limit: int = 40) -> list[CompPoint]:
        token = await self._access_token()
        if not token:
            return []

        term = search_query(query.title, brand=query.brand, model=query.model)
        if not term:
            return []

        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": self.settings.ebay_marketplace,
        }
        url = INSIGHTS_URL if self.use_insights else BROWSE_URL
        params: dict[str, Any] = {"q": term, "limit": min(limit, 50)}
        if not self.use_insights:
            params["filter"] = "buyingOptions:{FIXED_PRICE|AUCTION}"

        http = await self._http()
        try:
            response = await http.get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            log.warning("eBay search failed: %s", exc)
            return []

        if response.status_code == 403 and self.use_insights:
            log.warning(
                "eBay Marketplace Insights returned 403 — partner approval required. "
                "Falling back to Browse (active listings)."
            )
            self.use_insights = False
            return await self.fetch(query, limit=limit)
        if response.status_code != 200:
            log.warning("eBay search %s: %s", response.status_code, response.text[:200])
            return []

        return self._parse(response.json(), query, sold=self.use_insights)

    def _parse(self, payload: dict, query: ItemQuery, *, sold: bool) -> list[CompPoint]:
        items = payload.get("itemSales") or payload.get("itemSummaries") or []
        points: list[CompPoint] = []
        for item in items:
            price = _money(item.get("price"))
            if price is None:
                continue
            shipping = 0.0
            options = item.get("shippingOptions") or []
            if options:
                shipping = _money(options[0].get("shippingCost")) or 0.0

            sold_at = None
            if sold and item.get("lastSoldDate"):
                from dateutil import parser as date_parser

                try:
                    sold_at = date_parser.parse(item["lastSoldDate"])
                except (ValueError, TypeError):
                    sold_at = None

            title = item.get("title")
            points.append(
                CompPoint(
                    price=price,
                    shipping=shipping,
                    source="ebay_sold" if sold else "ebay",
                    title=title,
                    condition=item.get("condition"),
                    is_sold=sold,
                    sold_at=sold_at,
                    url=item.get("itemWebUrl"),
                    similarity=score_similarity(query, title),
                )
            )
        return points

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _money(node: Any) -> float | None:
    if not isinstance(node, dict):
        return None
    raw = node.get("value") or node.get("amount")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
