"""Fetching and storing outside-market prices.

Access reality, same as everywhere else in this project: Amazon's Product
Advertising API needs an Associate account with qualifying sales, Walmart's needs
partner approval, and Alibaba has no open price API. So the sources that work
without begging for credentials are:

  * **eBay Browse** — already wired for comps, and returns new listings too
  * **Browser capture** — the extension reads a product page you have open
  * **CSV / manual** — for anything you'd rather enter yourself

Everything lands in `MarketPrice` regardless of origin, so the ceiling logic
never has to care where a number came from.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Lot, MarketPrice, PriceKind
from ..normalize import key_family, normalize_item_key, title_similarity
from .alternatives import MarketView, build_market_view

log = logging.getLogger(__name__)


def record_prices(session: Session, query_key: str, records: list[dict]) -> int:
    """Store observed prices, skipping ones already seen at the same URL."""
    added = 0
    for record in records:
        url = record.get("url")
        if url:
            exists = session.scalar(
                select(MarketPrice.id).where(
                    MarketPrice.url == url, MarketPrice.query_key == query_key
                )
            )
            if exists:
                continue
        try:
            amount = float(record["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if amount <= 0:
            continue

        session.add(
            MarketPrice(
                query_key=query_key,
                kind=PriceKind(record.get("kind", PriceKind.EXACT_NEW.value)),
                source=record.get("source", "manual"),
                title=(record.get("title") or "")[:512],
                price=amount,
                shipping=float(record.get("shipping") or 0.0),
                url=url,
                in_stock=bool(record.get("in_stock", True)),
                rating=_opt_float(record.get("rating")),
                review_count=_opt_int(record.get("review_count")),
                brand=record.get("brand"),
                similarity=float(record.get("similarity") or 1.0),
            )
        )
        added += 1
    session.flush()
    return added


def prices_for(session: Session, lot: Lot) -> list[MarketPrice]:
    """Every observed price relevant to this lot.

    Exact matches come from the lot's own key. Substitutes are drawn from the
    wider family and filtered by title similarity, so a genuinely different
    product doesn't get treated as a stand-in.
    """
    key = normalize_item_key(lot.title, brand=lot.brand, model=lot.model, upc=lot.upc)
    family = key_family(key)

    rows = session.scalars(
        select(MarketPrice).where(MarketPrice.query_key.like(f"{family}%"))
    ).all()

    kept: list[MarketPrice] = []
    for row in rows:
        if row.kind in (PriceKind.EXACT_NEW, PriceKind.EXACT_USED):
            kept.append(row)
            continue
        # A substitute must at least be in the same product territory.
        if title_similarity(lot.title, row.title) >= 0.2:
            kept.append(row)
    return kept


def market_view_for_lot(session: Session, lot: Lot) -> MarketView:
    """What the outside market says about this lot."""
    return build_market_view(
        stated_retail=lot.retail_price, prices=prices_for(session, lot)
    )


def _opt_float(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _opt_int(value) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
