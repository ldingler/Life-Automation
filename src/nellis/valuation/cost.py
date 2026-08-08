"""Landed cost and the walk-away max bid solver.

This is the arithmetic the whole system exists to get right. Two directions:

  forward:  hammer price      -> what it actually costs you
  inverse:  acceptable profit -> the highest hammer price that still clears it

The inverse is the number you type into Nellis' proxy-bid field. Everything
else in the codebase is in service of computing it accurately.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Nellis proxy-bids in $1 steps. Larger lots step up; override if you observe
# different behavior in your categories.
BID_INCREMENTS: tuple[tuple[float, float], ...] = (
    (100.0, 1.0),
    (500.0, 5.0),
    (1000.0, 10.0),
    (float("inf"), 25.0),
)


def bid_increment(amount: float) -> float:
    for ceiling, step in BID_INCREMENTS:
        if amount < ceiling:
            return step
    return BID_INCREMENTS[-1][1]


def floor_to_increment(amount: float) -> float:
    """Round a computed bid DOWN to a legal step.

    Down, never up: rounding up would silently push you past the margin you
    asked for.
    """
    if amount <= 0:
        return 0.0
    step = bid_increment(amount)
    return math.floor(amount / step) * step


@dataclass(frozen=True)
class LandedCost:
    """Full out-of-pocket cost of winning at `hammer`."""

    hammer: float
    buyers_premium: float
    tax: float
    pickup: float

    @property
    def total(self) -> float:
        return self.hammer + self.buyers_premium + self.tax + self.pickup

    def breakdown(self) -> dict[str, float]:
        return {
            "hammer": round(self.hammer, 2),
            "buyers_premium": round(self.buyers_premium, 2),
            "tax": round(self.tax, 2),
            "pickup": round(self.pickup, 2),
            "total": round(self.total, 2),
        }


def landed_cost(
    hammer: float,
    *,
    bp_rate: float,
    tax_rate: float,
    pickup: float = 0.0,
) -> LandedCost:
    """What you actually pay.

    Sales tax applies to the hammer price *plus* the buyer's premium — this is
    the step people forget, and at 15% BP + 6.625% tax it is ~22.6% on top of
    every bid.
    """
    premium = hammer * bp_rate
    tax = (hammer + premium) * tax_rate
    return LandedCost(hammer=hammer, buyers_premium=premium, tax=tax, pickup=pickup)


def cost_multiplier(bp_rate: float, tax_rate: float) -> float:
    """Every $1 of hammer price costs this much landed."""
    return (1.0 + bp_rate) * (1.0 + tax_rate)


def walk_away_max_bid(
    net_resale: float,
    *,
    target_margin: float,
    bp_rate: float,
    tax_rate: float,
    pickup: float = 0.0,
    round_to_increment: bool = True,
) -> float:
    """Highest hammer price that still hits `target_margin` on `net_resale`.

    Derivation:
        profit  = net_resale - landed(H)
        require   profit >= target_margin * net_resale
        =>      landed(H) <= net_resale * (1 - target_margin)
        =>  H*(1+bp)*(1+tax) + pickup <= net_resale * (1 - target_margin)
        =>  H <= (net_resale * (1 - target_margin) - pickup) / ((1+bp)*(1+tax))

    Margin is measured against resale value, not cost — it answers "what
    fraction of the sale price do I keep?", which is the number that matters
    when comparing deals of different sizes.
    """
    if net_resale <= 0:
        return 0.0
    budget = net_resale * (1.0 - target_margin) - pickup
    if budget <= 0:
        return 0.0
    raw = budget / cost_multiplier(bp_rate, tax_rate)
    return floor_to_increment(raw) if round_to_increment else max(0.0, raw)


def profit_at(
    hammer: float,
    net_resale: float,
    *,
    bp_rate: float,
    tax_rate: float,
    pickup: float = 0.0,
) -> float:
    return net_resale - landed_cost(hammer, bp_rate=bp_rate, tax_rate=tax_rate, pickup=pickup).total


def margin_at(
    hammer: float,
    net_resale: float,
    *,
    bp_rate: float,
    tax_rate: float,
    pickup: float = 0.0,
) -> float:
    if net_resale <= 0:
        return 0.0
    return profit_at(hammer, net_resale, bp_rate=bp_rate, tax_rate=tax_rate, pickup=pickup) / net_resale
