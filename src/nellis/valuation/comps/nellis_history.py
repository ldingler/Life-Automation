"""Comps from our own harvested Nellis close prices.

The highest-signal source we have, and the only one that is free, unlimited and
ToS-clean. It answers the question that actually matters — "what do Nellis
buyers pay for this?" — rather than "what is this worth in the abstract".

Empty on day one. Compounds from there.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Comp
from ...normalize import key_family
from .base import CompPoint, CompsProvider, ItemQuery, score_similarity

log = logging.getLogger(__name__)


class NellisHistoryProvider(CompsProvider):
    name = "nellis"

    def __init__(self, session: Session):
        self.session = session

    async def fetch(self, query: ItemQuery, *, limit: int = 40) -> list[CompPoint]:
        exact_key = query.key
        family = key_family(exact_key)

        rows = self.session.scalars(
            select(Comp)
            .where(Comp.source == "nellis", Comp.query_key == exact_key)
            .order_by(Comp.sold_at.desc())
            .limit(limit)
        ).all()

        # Widen to the SKU family (DCD999B -> DCD999) when the exact key is thin.
        if len(rows) < 5 and family != exact_key:
            rows = self.session.scalars(
                select(Comp)
                .where(Comp.source == "nellis", Comp.query_key.like(f"{family}%"))
                .order_by(Comp.sold_at.desc())
                .limit(limit)
            ).all()

        # Last resort: brand-wide scan filtered by title similarity.
        if len(rows) < 3 and exact_key.startswith("bm:"):
            brand = exact_key.split(":")[1]
            rows = self.session.scalars(
                select(Comp)
                .where(Comp.source == "nellis", Comp.query_key.like(f"bm:{brand}:%"))
                .order_by(Comp.sold_at.desc())
                .limit(limit * 3)
            ).all()

        points: list[CompPoint] = []
        for row in rows:
            similarity = 1.0 if row.query_key == exact_key else score_similarity(query, row.title)
            points.append(
                CompPoint(
                    price=row.price,
                    shipping=row.shipping or 0.0,
                    source="nellis",
                    title=row.title,
                    condition=row.condition,
                    is_sold=True,
                    sold_at=row.sold_at or row.observed_at,
                    url=row.url,
                    similarity=similarity,
                )
            )
        return points


class StoredCompsProvider(CompsProvider):
    """Replays comps any provider previously cached to the DB.

    Lets valuation re-run offline (and in tests) without re-hitting any API.
    """

    name = "stored"

    def __init__(self, session: Session, sources: tuple[str, ...] = ("ebay", "csv", "feed")):
        self.session = session
        self.sources = sources

    async def fetch(self, query: ItemQuery, *, limit: int = 40) -> list[CompPoint]:
        family = key_family(query.key)
        rows = self.session.scalars(
            select(Comp)
            .where(Comp.source.in_(self.sources), Comp.query_key.like(f"{family}%"))
            .order_by(Comp.observed_at.desc())
            .limit(limit)
        ).all()
        return [
            CompPoint(
                price=row.price,
                shipping=row.shipping or 0.0,
                source=row.source,
                title=row.title,
                condition=row.condition,
                is_sold=row.is_sold,
                sold_at=row.sold_at or row.observed_at,
                url=row.url,
                similarity=score_similarity(query, row.title),
            )
            for row in rows
        ]
