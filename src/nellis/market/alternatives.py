"""What else could I buy instead, and what does that do to my bid?

The gap this closes, in Logan's words:

    "On Nellis, widget A has a suggested retail of $500 and the bid is at $350
    with a minute left. But a similar widget, with similar reviews and ratings,
    sells new somewhere else for $300. It would be foolish to bid, because we
    can get something very similar, brand new, for less."

Everything before this compared a lot against *itself* — what the same item sold
for second-hand. That silently assumes the only way to get the thing is to win
this auction. It isn't. The real ceiling is the cheapest way to obtain the
capability, whatever product that happens to be.

Two jobs here:

  1. **Verify stated retail.** Not assume it's inflated, not assume it's honest —
     check it against real listings for the exact item and say which it was.
  2. **Find the best alternative.** The cheapest genuinely comparable option,
     new or used, and refuse any bid that lands above it.

The quality gate is what makes (2) usable rather than destructive. A $19 no-name
knock-off is not a substitute for a $500 tool, and without a ratings/review floor
the cheapest junk on the internet would veto every worthwhile lot. A substitute
only counts if it is plausibly as good.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..models import MarketPrice, PriceKind, VerificationStatus

# Stated retail is treated as honest within this band of real listings. Retail
# genuinely varies by seller, so a small gap is noise rather than deception.
RETAIL_TOLERANCE = 0.25

# A substitute must clear these to count as comparable. Below them it is a
# different (worse) product, not a cheaper version of the same one.
MIN_SUBSTITUTE_RATING = 4.0
MIN_SUBSTITUTE_REVIEWS = 25

# How much cheaper than the best new alternative a landed bid must be before it's
# worth buying used at auction, with its pickup trip and no warranty or returns.
NEW_ALTERNATIVE_MARGIN = 0.30

# Prices go stale. Beyond this they inform but don't decide.
FRESH_DAYS = 21


@dataclass
class Alternative:
    """One way of getting this capability that isn't winning this lot."""

    price: float
    source: str
    title: str
    kind: PriceKind
    url: str | None = None
    rating: float | None = None
    review_count: int | None = None
    is_new: bool = True

    def describe(self) -> str:
        quality = ""
        if self.rating and self.review_count:
            quality = f", {self.rating:.1f}★ from {self.review_count:,} reviews"
        condition = "new" if self.is_new else "used"
        return f"${self.price:,.2f} {condition} at {self.source}{quality}"


@dataclass
class MarketView:
    """Everything known about buying this thing somewhere other than Nellis."""

    stated_retail: float | None
    verified_retail: float | None
    verification: VerificationStatus
    best_alternative: Alternative | None
    exact_new_price: float | None = None
    alternatives_considered: int = 0
    rejected_for_quality: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def opportunity_ceiling(self) -> float | None:
        """The most it can make sense to pay landed, given the alternatives.

        Buying used at auction has to beat buying new elsewhere by a real margin,
        not a rounding error — there's a pickup trip, no warranty and no returns.
        """
        if self.best_alternative is None:
            return None
        if self.best_alternative.is_new:
            return self.best_alternative.price * (1.0 - NEW_ALTERNATIVE_MARGIN)
        return self.best_alternative.price

    def as_dict(self) -> dict:
        return {
            "stated_retail": self.stated_retail,
            "verified_retail": self.verified_retail,
            "verification": self.verification.value,
            "exact_new_price": self.exact_new_price,
            "best_alternative": self.best_alternative.describe()
            if self.best_alternative
            else None,
            "best_alternative_price": self.best_alternative.price
            if self.best_alternative
            else None,
            "opportunity_ceiling": round(self.opportunity_ceiling, 2)
            if self.opportunity_ceiling is not None
            else None,
            "alternatives_considered": self.alternatives_considered,
            "rejected_for_quality": self.rejected_for_quality,
            "notes": self.notes,
        }


def _fresh(prices: list[MarketPrice], now: datetime) -> list[MarketPrice]:
    cutoff = now - timedelta(days=FRESH_DAYS)
    kept = []
    for price in prices:
        observed = price.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        if observed >= cutoff and price.in_stock and price.total_price > 0:
            kept.append(price)
    return kept


def verify_retail(
    stated_retail: float | None, exact_new: list[MarketPrice]
) -> tuple[float | None, VerificationStatus, str]:
    """Check a stated retail price against real listings for the same item.

    Deliberately not skeptical by default. Stated retail is right much of the
    time, and assuming otherwise throws away a usable number. The point is to
    *check* rather than guess in either direction.
    """
    if not exact_new:
        if stated_retail:
            return None, VerificationStatus.UNVERIFIED, (
                "couldn't find this exact item for sale new — stated retail is unconfirmed"
            )
        return None, VerificationStatus.UNVERIFIED, "no retail price and nothing found for sale"

    observed = sorted(p.total_price for p in exact_new)
    # Median, not minimum: one clearance listing shouldn't redefine retail.
    street = observed[len(observed) // 2]

    if stated_retail is None:
        return street, VerificationStatus.UNVERIFIED, (
            f"no stated retail; real listings put it around ${street:,.2f}"
        )

    ratio = street / stated_retail if stated_retail > 0 else 0.0
    if ratio >= 1.0 + RETAIL_TOLERANCE:
        return street, VerificationStatus.UNDERSTATED, (
            f"stated retail ${stated_retail:,.0f} is below the ${street:,.2f} "
            f"street price across {len(observed)} listing(s)"
        )
    if ratio >= 1.0 - RETAIL_TOLERANCE:
        return street, VerificationStatus.VERIFIED, (
            f"confirmed: {len(observed)} listing(s) around ${street:,.2f}, "
            f"consistent with the ${stated_retail:,.0f} claim"
        )
    return street, VerificationStatus.OVERSTATED, (
        f"stated retail ${stated_retail:,.0f} is overstated — the same item sells "
        f"for about ${street:,.2f} new ({len(observed)} listing(s))"
    )


def _qualifies(price: MarketPrice) -> bool:
    """Is this genuinely comparable, or just cheaper?"""
    if price.rating is None or price.review_count is None:
        # Unrated listings can't be quality-checked, so they may inform but must
        # never be the reason a lot is rejected.
        return False
    return price.rating >= MIN_SUBSTITUTE_RATING and price.review_count >= MIN_SUBSTITUTE_REVIEWS


def build_market_view(
    *,
    stated_retail: float | None,
    prices: list[MarketPrice],
    now: datetime | None = None,
    min_rating: float = MIN_SUBSTITUTE_RATING,
    min_reviews: int = MIN_SUBSTITUTE_REVIEWS,
) -> MarketView:
    """Assemble what the outside market says about this item."""
    now = now or datetime.now(UTC)
    usable = _fresh(prices, now)

    exact_new = [p for p in usable if p.kind == PriceKind.EXACT_NEW]
    verified_retail, status, note = verify_retail(stated_retail, exact_new)

    view = MarketView(
        stated_retail=stated_retail,
        verified_retail=verified_retail,
        verification=status,
        best_alternative=None,
        exact_new_price=min((p.total_price for p in exact_new), default=None),
        notes=[note],
    )

    # Candidates: the exact item new, plus substitutes that clear the quality bar.
    candidates: list[Alternative] = []
    rejected = 0

    for price in usable:
        is_substitute = price.kind in (PriceKind.SUBSTITUTE_NEW, PriceKind.SUBSTITUTE_USED)
        if is_substitute:
            qualified = (
                price.rating is not None
                and price.review_count is not None
                and price.rating >= min_rating
                and price.review_count >= min_reviews
            )
            if not qualified:
                rejected += 1
                continue

        candidates.append(
            Alternative(
                price=price.total_price,
                source=price.source,
                title=price.title,
                kind=price.kind,
                url=price.url,
                rating=price.rating,
                review_count=price.review_count,
                is_new=price.kind in (PriceKind.EXACT_NEW, PriceKind.SUBSTITUTE_NEW),
            )
        )

    view.alternatives_considered = len(candidates)
    view.rejected_for_quality = rejected

    if candidates:
        view.best_alternative = min(candidates, key=lambda a: a.price)
        if view.best_alternative.kind in (PriceKind.SUBSTITUTE_NEW, PriceKind.SUBSTITUTE_USED):
            view.notes.append(
                f"cheapest comparable option is a substitute: {view.best_alternative.describe()}"
            )
    if rejected:
        view.notes.append(
            f"{rejected} cheaper option(s) ignored for failing the quality bar "
            f"({min_rating}★ / {min_reviews} reviews)"
        )

    return view


@dataclass
class CeilingDecision:
    """Whether the outside market permits a bid, and how high."""

    allowed: bool
    ceiling: float | None
    reason: str

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "ceiling": round(self.ceiling, 2) if self.ceiling is not None else None,
            "reason": self.reason,
        }


def apply_ceiling(view: MarketView, landed_at_bid: float) -> CeilingDecision:
    """The widget rule: never pay auction prices above the cheapest real option.

    Returns a hard verdict rather than a score, because this is not a preference
    to weigh — if you can buy an equivalent new for less, bidding is simply the
    wrong move regardless of how good the discount-off-retail looks.
    """
    ceiling = view.opportunity_ceiling
    if ceiling is None:
        return CeilingDecision(
            allowed=True, ceiling=None,
            reason="no verified alternative found — bid on comps alone, with care",
        )

    alternative = view.best_alternative
    assert alternative is not None

    if landed_at_bid > alternative.price:
        return CeilingDecision(
            allowed=False, ceiling=ceiling,
            reason=(
                f"landed cost ${landed_at_bid:,.2f} exceeds buying it outright: "
                f"{alternative.describe()}. Don't bid."
            ),
        )

    if landed_at_bid > ceiling:
        saving = alternative.price - landed_at_bid
        return CeilingDecision(
            allowed=False, ceiling=ceiling,
            reason=(
                f"only ${saving:,.2f} cheaper than {alternative.describe()} — "
                f"not worth the pickup, no warranty and no returns. "
                f"Bid below ${ceiling:,.2f}."
            ),
        )

    return CeilingDecision(
        allowed=True, ceiling=ceiling,
        reason=(
            f"comfortably under the best alternative ({alternative.describe()})"
        ),
    )
