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
from .classify import Rejected, classify_all, classify_candidate
from .jobs import (
    LeasedJob,
    LookupResult,
    Pacing,
    check_pacing,
    complete,
    enqueue_for_lot,
    lease_next,
    mark_blocked,
    mark_failed,
    priority_for_lot,
    queue_status,
    reclaim_stale,
    search_url,
)
from .lookup import market_view_for_lot, record_prices

__all__ = [
    "market_view_for_lot",
    "record_prices",
    "Rejected",
    "classify_all",
    "classify_candidate",
    "LeasedJob",
    "LookupResult",
    "Pacing",
    "check_pacing",
    "complete",
    "enqueue_for_lot",
    "lease_next",
    "mark_blocked",
    "mark_failed",
    "priority_for_lot",
    "queue_status",
    "reclaim_stale",
    "search_url",
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
