"""Comps interface and aggregation.

`CompsProvider` is the vendor-neutral boundary. Nellis close history, eBay,
CSV imports and third-party feeds all implement it, so adding a paid sold-comps
service later is a new file and one config line — nothing else changes.

Aggregation is deliberately robust rather than clever: a trimmed, recency-
weighted median. Auction comps are noisy and contain outliers in both
directions (someone overpaid; someone stole one). A mean would chase them.
"""

from __future__ import annotations

import logging
import statistics
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ...normalize import normalize_item_key, title_similarity

log = logging.getLogger(__name__)


@dataclass
class ItemQuery:
    """What we're trying to price."""

    title: str
    brand: str | None = None
    model: str | None = None
    upc: str | None = None
    category: str | None = None

    @property
    def key(self) -> str:
        return normalize_item_key(self.title, brand=self.brand, model=self.model, upc=self.upc)


@dataclass
class CompPoint:
    price: float
    source: str
    title: str | None = None
    shipping: float = 0.0
    condition: str | None = None
    is_sold: bool = True
    sold_at: datetime | None = None
    url: str | None = None
    similarity: float = 1.0

    @property
    def total(self) -> float:
        return self.price + (self.shipping or 0.0)


@dataclass
class CompSet:
    """Aggregated view of every comp we found for one item."""

    value: float | None
    count: int
    sources: list[str] = field(default_factory=list)
    spread: float | None = None          # IQR / median — dispersion, 0 = tight
    median_age_days: float | None = None
    points: list[CompPoint] = field(default_factory=list)
    used_family_key: bool = False

    def as_dict(self) -> dict:
        return {
            "value": round(self.value, 2) if self.value else None,
            "count": self.count,
            "sources": self.sources,
            "spread": round(self.spread, 3) if self.spread is not None else None,
            "median_age_days": round(self.median_age_days, 1)
            if self.median_age_days is not None
            else None,
        }


class CompsProvider(ABC):
    """One source of price observations."""

    name: str = "abstract"
    enabled: bool = True

    @abstractmethod
    async def fetch(self, query: ItemQuery, *, limit: int = 40) -> list[CompPoint]:
        ...


def _age_weight(sold_at: datetime | None, now: datetime, half_life_days: float = 45.0) -> float:
    """Exponential recency decay. A 45-day-old comp counts half as much."""
    if sold_at is None:
        return 0.5
    if sold_at.tzinfo is None:
        sold_at = sold_at.replace(tzinfo=UTC)
    age_days = max(0.0, (now - sold_at).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life_days)


# Sold prices are ground truth; asking prices are aspirational. Discount them.
SOURCE_WEIGHTS = {"nellis": 1.0, "csv": 0.95, "ebay_sold": 1.0, "ebay": 0.72, "feed": 0.7}
ACTIVE_LISTING_HAIRCUT = 0.88


def weighted_median(values: list[tuple[float, float]]) -> float | None:
    """Median where each value carries a weight. Robust to outliers by design."""
    if not values:
        return None
    ordered = sorted(values, key=lambda pair: pair[0])
    total = sum(w for _, w in ordered)
    if total <= 0:
        return statistics.median([v for v, _ in ordered])
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= total / 2:
            return value
    return ordered[-1][0]


def aggregate(
    points: list[CompPoint],
    *,
    min_similarity: float = 0.45,
    max_age_days: int = 120,
    trim_fraction: float = 0.1,
) -> CompSet:
    """Turn raw observations into one defensible number."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=max_age_days)

    kept: list[CompPoint] = []
    for point in points:
        if point.similarity < min_similarity:
            continue
        if point.sold_at is not None:
            sold_at = (
                point.sold_at.replace(tzinfo=UTC)
                if point.sold_at.tzinfo is None
                else point.sold_at
            )
            if sold_at < cutoff:
                continue
        if point.total <= 0:
            continue
        kept.append(point)

    if not kept:
        return CompSet(value=None, count=0)

    # Trim extremes before weighting — one absurd listing shouldn't move the median.
    kept.sort(key=lambda p: p.total)
    if len(kept) >= 8:
        trim = max(1, int(len(kept) * trim_fraction))
        kept = kept[trim:-trim] or kept

    weighted: list[tuple[float, float]] = []
    for point in kept:
        source_weight = SOURCE_WEIGHTS.get(point.source, 0.7)
        price = point.total
        if not point.is_sold:
            price *= ACTIVE_LISTING_HAIRCUT
        weight = source_weight * _age_weight(point.sold_at, now) * max(0.1, point.similarity)
        weighted.append((price, weight))

    value = weighted_median(weighted)

    prices = sorted(p.total for p in kept)
    spread = None
    if len(prices) >= 4 and value:
        q1 = prices[len(prices) // 4]
        q3 = prices[(3 * len(prices)) // 4]
        spread = (q3 - q1) / value if value else None

    ages = [
        (now - (p.sold_at.replace(tzinfo=UTC) if p.sold_at.tzinfo is None else p.sold_at)).days
        for p in kept
        if p.sold_at is not None
    ]

    return CompSet(
        value=value,
        count=len(kept),
        sources=sorted({p.source for p in kept}),
        spread=spread,
        median_age_days=statistics.median(ages) if ages else None,
        points=kept,
    )


def score_similarity(query: ItemQuery, title: str | None) -> float:
    if not title:
        return 0.6
    return title_similarity(query.title, title)
