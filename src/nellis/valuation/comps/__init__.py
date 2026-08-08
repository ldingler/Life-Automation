"""Comps service: run every enabled provider, cache results, aggregate."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import Settings, get_settings
from ...models import Comp
from .base import CompPoint, CompSet, CompsProvider, ItemQuery, aggregate
from .ebay import EbayProvider
from .manual_csv import LocalFeedProvider, ManualCsvProvider
from .nellis_history import NellisHistoryProvider, StoredCompsProvider

log = logging.getLogger(__name__)

__all__ = [
    "CompPoint",
    "CompSet",
    "CompsProvider",
    "CompsService",
    "EbayProvider",
    "ItemQuery",
    "LocalFeedProvider",
    "ManualCsvProvider",
    "NellisHistoryProvider",
    "StoredCompsProvider",
    "aggregate",
]


class CompsService:
    """Fans out to providers and merges the result.

    External providers are cached to the `comps` table so a re-valuation costs
    zero API calls, and so `offline=True` still produces a full answer.
    """

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        providers: list[CompsProvider] | None = None,
        *,
        offline: bool = False,
    ):
        self.session = session
        self.settings = settings or get_settings()
        self.offline = offline
        self.providers = providers if providers is not None else self._default_providers()

    def _default_providers(self) -> list[CompsProvider]:
        providers: list[CompsProvider] = [
            NellisHistoryProvider(self.session),
            StoredCompsProvider(self.session),
        ]
        if not self.offline:
            comps_dir = Path(self.settings.database_url.split("///")[-1]).parent / "comps"
            providers.append(ManualCsvProvider(comps_dir))
            providers.append(EbayProvider(self.settings))
            providers.append(LocalFeedProvider(self.settings))
        return [p for p in providers if p.enabled]

    async def collect(self, query: ItemQuery, *, limit: int = 40) -> CompSet:
        results = await asyncio.gather(
            *(self._safe_fetch(p, query, limit) for p in self.providers),
            return_exceptions=False,
        )

        points: list[CompPoint] = []
        for provider, fetched in zip(self.providers, results, strict=True):
            if provider.name not in ("nellis", "stored"):
                self._persist(query, fetched)
            points.extend(fetched)

        comp_set = aggregate(
            points,
            max_age_days=self.settings.comps_max_age_days,
        )
        log.debug(
            "comps for %s: value=%s n=%d sources=%s",
            query.key,
            comp_set.value,
            comp_set.count,
            comp_set.sources,
        )
        return comp_set

    async def _safe_fetch(
        self, provider: CompsProvider, query: ItemQuery, limit: int
    ) -> list[CompPoint]:
        try:
            return await provider.fetch(query, limit=limit)
        except Exception as exc:  # one bad provider must not sink valuation
            log.warning("comps provider %s failed: %s", provider.name, exc)
            return []

    def _persist(self, query: ItemQuery, points: list[CompPoint]) -> None:
        """Cache external comps, skipping ones we already stored."""
        for point in points:
            if point.url:
                exists = self.session.scalar(
                    select(Comp.id).where(Comp.source == point.source, Comp.url == point.url)
                )
                if exists:
                    continue
            self.session.add(
                Comp(
                    source=point.source,
                    query_key=query.key,
                    title=point.title,
                    price=point.price,
                    shipping=point.shipping,
                    condition=point.condition,
                    is_sold=point.is_sold,
                    sold_at=point.sold_at,
                    url=point.url,
                )
            )
