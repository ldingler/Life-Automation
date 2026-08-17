"""The outside market: what this thing costs to buy elsewhere, right now.

`Comp` records what something *sold for* second-hand. This package records what
you'd *pay to get one today* — which is the number that decides whether bidding
makes sense at all. A lot at 30% off a verified retail price is still a bad buy
if an equally good product sells new for less than the bid.
"""

from .alternatives import (
    MIN_SUBSTITUTE_RATING,
    MIN_SUBSTITUTE_REVIEWS,
    NEW_ALTERNATIVE_MARGIN,
    Alternative,
    CeilingDecision,
    MarketView,
    apply_ceiling,
    build_market_view,
    verify_retail,
)
from .lookup import market_view_for_lot, record_prices

__all__ = [
    "market_view_for_lot",
    "record_prices",
    "MIN_SUBSTITUTE_RATING",
    "MIN_SUBSTITUTE_REVIEWS",
    "NEW_ALTERNATIVE_MARGIN",
    "Alternative",
    "CeilingDecision",
    "MarketView",
    "apply_ceiling",
    "build_market_view",
    "verify_retail",
]
