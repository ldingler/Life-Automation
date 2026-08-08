"""Parts price providers.

`CatalogPartsProvider` is the workhorse: a seeded table of typical replacement
costs plus anything you've cached from real lookups. It always answers, which
matters — an unpriced part silently drops out of the repair math otherwise.

`EbayPartsProvider` refines those guesses with real listings when credentials
are configured, and writes results back to the catalog so the estimate improves
over time.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import Settings
from ...models import PartPrice
from ..condition import REPLACEABLE_PARTS
from .base import PartQuote, PartsProvider

log = logging.getLogger(__name__)


class CatalogPartsProvider(PartsProvider):
    """DB-cached prices, falling back to the seeded typical-cost table."""

    name = "catalog"

    def __init__(self, session: Session):
        self.session = session

    async def quote(self, part: str, *, item_context: str = "") -> PartQuote | None:
        key = part.lower().strip()
        if not key:
            return None

        row = self.session.scalar(
            select(PartPrice).where(PartPrice.part_key == key).order_by(PartPrice.updated_at.desc())
        )
        if row is not None:
            return PartQuote(
                part=key,
                price=row.price,
                source=row.source,
                availability=row.availability or "unknown",
                url=row.url,
                confident=row.source != "typical",
            )

        typical = REPLACEABLE_PARTS.get(key)
        if typical is None:
            # Try a contained known part: "charger cable" -> "cable"
            matches = [p for p in REPLACEABLE_PARTS if p in key]
            if matches:
                typical = REPLACEABLE_PARTS[max(matches, key=len)]

        if typical is None:
            return None

        return PartQuote(
            part=key,
            price=typical,
            source="typical",
            availability="assumed available",
            confident=False,
        )


class EbayPartsProvider(PartsProvider):
    """Live part pricing via eBay search, cached back into the catalog."""

    name = "ebay_parts"

    def __init__(self, session: Session, settings: Settings, ebay_provider=None):
        self.session = session
        self.settings = settings
        self.enabled = bool(settings.ebay_client_id and settings.ebay_client_secret)
        self._ebay = ebay_provider

    async def quote(self, part: str, *, item_context: str = "") -> PartQuote | None:
        if not self.enabled:
            return None

        from ..comps.base import ItemQuery
        from ..comps.ebay import EbayProvider

        provider = self._ebay or EbayProvider(self.settings)
        term = f"{item_context} {part} replacement".strip()
        try:
            points = await provider.fetch(ItemQuery(title=term), limit=12)
        except Exception as exc:
            log.warning("eBay parts lookup failed for %r: %s", part, exc)
            return None

        prices = sorted(p.total for p in points if p.total > 0)
        if not prices:
            return None

        # Take the lower quartile: you buy the cheapest workable part, not the median.
        index = max(0, len(prices) // 4 - 1) if len(prices) >= 4 else 0
        price = prices[index]

        key = part.lower().strip()
        existing = self.session.scalar(
            select(PartPrice).where(PartPrice.part_key == key, PartPrice.source == "ebay")
        )
        if existing is None:
            self.session.add(
                PartPrice(
                    part_key=key,
                    description=term,
                    source="ebay",
                    price=price,
                    availability=f"{len(prices)} listings",
                )
            )
        else:
            existing.price = price
            existing.updated_at = datetime.now(UTC)

        return PartQuote(
            part=key,
            price=price,
            source="ebay",
            availability=f"{len(prices)} listings found",
            confident=True,
        )


class CompositePartsProvider(PartsProvider):
    """Tries providers in order; first confident answer wins, else best effort."""

    name = "composite"

    def __init__(self, providers: list[PartsProvider]):
        self.providers = [p for p in providers if p.enabled]

    async def quote(self, part: str, *, item_context: str = "") -> PartQuote | None:
        fallback: PartQuote | None = None
        for provider in self.providers:
            try:
                quote = await provider.quote(part, item_context=item_context)
            except Exception as exc:
                log.warning("parts provider %s failed: %s", provider.name, exc)
                continue
            if quote is None:
                continue
            if quote.confident:
                return quote
            fallback = fallback or quote
        return fallback


async def quote_parts(
    provider: PartsProvider, parts: list[str], *, item_context: str = ""
) -> tuple[list[PartQuote], list[str]]:
    """Price a list of parts. Returns (quotes, parts we could not price)."""
    quotes: list[PartQuote] = []
    unpriced: list[str] = []
    for part in parts:
        quote = await provider.quote(part, item_context=item_context)
        if quote is None:
            unpriced.append(part)
        else:
            quotes.append(quote)
    return quotes, unpriced
