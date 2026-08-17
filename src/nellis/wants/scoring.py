"""Want-mode scoring: "do I want this?" rather than "can I flip this?"

The resale engine solves for the highest bid that still clears a profit margin.
That is the wrong question for something you actually need. A shed you want is
worth buying at a price that would be a terrible flip, and running personal
purchases through resale math would reject exactly the things you were looking
for.

So want-mode asks a different question, and answers with a different number:

    resale mode   most I can pay and still profit reselling
    want mode     most I'd rationally pay rather than buy it new elsewhere

The ceiling is whichever is lower of what you said it's worth to you, and a
target discount against its real retail price. Comps still matter — they stop
"60% off retail" being a good deal on something whose retail is fiction — but
they inform the ceiling instead of dictating it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..models import Lot, ReplenishmentClass, WantItem
from ..valuation.condition import ConditionReport
from ..valuation.cost import floor_to_increment, landed_cost
from .replenishment import Satiation


@dataclass
class WantVerdict:
    """What want-mode concluded about one lot."""

    interest_score: float
    suggested_max_bid: float
    landed_at_max: float
    worth_ceiling: float
    discount_vs_retail: float | None
    satiation: Satiation
    matched_via: str
    explanation: str
    suppressed: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "interest_score": round(self.interest_score, 3),
            "suggested_max_bid": round(self.suggested_max_bid, 2),
            "landed_at_max": round(self.landed_at_max, 2),
            "worth_ceiling": round(self.worth_ceiling, 2),
            "discount_vs_retail": round(self.discount_vs_retail, 3)
            if self.discount_vs_retail is not None
            else None,
            "satiation_multiplier": self.satiation.multiplier,
            "satiation_reason": self.satiation.reason,
            "matched_via": self.matched_via,
            "explanation": self.explanation,
            "suppressed": self.suppressed,
            "warnings": self.warnings,
        }


# Below this, an item is hidden from the default feed rather than merely ranked
# low. It still appears with "show satisfied" turned on.
SUPPRESS_BELOW = 0.35


def condition_penalty(report: ConditionReport, want: WantItem | None) -> tuple[float, list[str]]:
    """How much a lot's condition reduces personal interest.

    Sharper than the resale version. Reselling damaged goods is a business
    decision; using them yourself is a preference, and most people don't want a
    broken one of the thing they went looking for.
    """
    warnings: list[str] = []
    if report.is_fatal:
        return 0.0, ["non-functional — no use to you even at $0"]

    penalty = report.condition_multiplier
    if report.missing_parts:
        parts = ", ".join(report.missing_parts[:3])
        if want is not None and not want.accept_damaged:
            return 0.0, [f"missing {parts}, and this want is marked complete-only"]
        warnings.append(f"missing {parts} — you'd need to source them")
        penalty *= 0.75
    if report.is_untested:
        warnings.append("untested — condition is a gamble")
        penalty *= 0.9
    return max(0.0, min(1.0, penalty)), warnings


def score_lot(
    lot: Lot,
    *,
    want: WantItem | None,
    satiation: Satiation,
    condition: ConditionReport,
    intent_weight: float,
    comp_value: float | None,
    settings,
    matched_via: str,
    now: datetime | None = None,
) -> WantVerdict:
    """Decide whether Logan wants this lot, and the most he should pay for it."""
    now = now or datetime.now(UTC)
    warnings: list[str] = []

    # ---- what is it worth to you? ---------------------------------------
    # Prefer an explicit number. Fall back to a discount off retail, but sanity
    # check retail against comps, because inflated "retail" is the oldest trick
    # in liquidation and the whole point is not to be fooled by it.
    retail = lot.retail_price
    target_discount = want.target_discount_vs_retail if want else 0.5

    if retail and comp_value and retail > comp_value * 2.5:
        warnings.append(
            f"stated retail ${retail:,.0f} looks inflated — comps say ~${comp_value:,.0f}"
        )
        retail = comp_value * 1.3

    ceiling_candidates: list[float] = []
    if want is not None and want.max_worth_to_me:
        ceiling_candidates.append(want.max_worth_to_me)
    if retail:
        ceiling_candidates.append(retail * (1.0 - target_discount))
    if comp_value:
        # Never rationally pay more than the going second-hand rate.
        ceiling_candidates.append(comp_value)

    if not ceiling_candidates:
        return WantVerdict(
            interest_score=0.0, suggested_max_bid=0.0, landed_at_max=0.0,
            worth_ceiling=0.0, discount_vs_retail=None, satiation=satiation,
            matched_via=matched_via, suppressed=True,
            explanation="No retail price, no comps and no stated worth — can't price this.",
            warnings=["nothing to value this against"],
        )

    worth_ceiling = min(ceiling_candidates)

    # ---- condition ------------------------------------------------------
    cond_multiplier, cond_warnings = condition_penalty(condition, want)
    warnings.extend(cond_warnings)
    worth_ceiling *= cond_multiplier

    # ---- solve for the bid ----------------------------------------------
    # worth_ceiling is what it's worth to you LANDED, so invert premium and tax
    # to get the hammer price. Same identity the resale engine uses; different
    # ceiling feeding it.
    bp_rate = lot.buyers_premium_rate or settings.default_buyers_premium
    multiplier = (1.0 + bp_rate) * (1.0 + settings.sales_tax_rate)
    raw_max = max(0.0, (worth_ceiling - settings.pickup_cost) / multiplier)
    suggested = floor_to_increment(raw_max)
    landed = landed_cost(
        suggested, bp_rate=bp_rate, tax_rate=settings.sales_tax_rate,
        pickup=settings.pickup_cost,
    ).total

    discount = None
    if lot.retail_price and lot.retail_price > 0:
        discount = max(0.0, 1.0 - (landed / lot.retail_price))

    # ---- interest --------------------------------------------------------
    priority = want.priority if want else 1.0
    base = intent_weight * priority * cond_multiplier
    interest = base * satiation.multiplier

    # A bigger gap between worth and current price is a better opportunity.
    current = lot.current_bid or 0.0
    if suggested > 0 and current < suggested:
        headroom = min(1.0, (suggested - current) / suggested)
        interest *= 0.6 + 0.4 * headroom
    elif current >= suggested:
        interest *= 0.15
        warnings.append(
            f"already bid to ${current:,.2f}, above your ${suggested:,.2f} ceiling"
        )

    suppressed = interest < SUPPRESS_BELOW or suggested <= 0

    explanation = _explain(
        lot, want, satiation, suggested, current, discount, matched_via, suppressed
    )

    return WantVerdict(
        interest_score=round(max(0.0, interest), 4),
        suggested_max_bid=suggested,
        landed_at_max=landed,
        worth_ceiling=worth_ceiling,
        discount_vs_retail=discount,
        satiation=satiation,
        matched_via=matched_via,
        explanation=explanation,
        suppressed=suppressed,
        warnings=warnings,
    )


def _explain(
    lot: Lot,
    want: WantItem | None,
    satiation: Satiation,
    suggested: float,
    current: float,
    discount: float | None,
    matched_via: str,
    suppressed: bool,
) -> str:
    if suggested <= 0:
        return "Not worth buying at any price in its current condition."

    label = want.label if want else lot.title[:48]
    parts = [f"Matches '{label}' ({matched_via})."]

    if discount is not None:
        parts.append(f"About {discount:.0%} off retail landed at your max.")
    parts.append(f"Bid up to ${suggested:,.2f} (currently ${current:,.2f}).")

    if satiation.is_suppressed:
        verb = "Hidden" if suppressed else "Ranked down"
        parts.append(f"{verb}: {satiation.reason}.")

    return " ".join(parts)


def resurfacing_note(replenishment: ReplenishmentClass, satiation: Satiation) -> str | None:
    """Human-readable note about when a suppressed item comes back.

    Suppression should be visible and finite. An item that silently vanishes
    forever is indistinguishable from a bug.
    """
    if not satiation.is_suppressed:
        return None
    from .replenishment import days_until_resurface

    days = days_until_resurface(replenishment)
    if days is None:
        return "Suppressed indefinitely — mark it as needed again to override."
    if days <= 1:
        return "Will resurface almost immediately."
    if days < 60:
        return f"Resurfaces in roughly {int(days)} days."
    return f"Resurfaces in roughly {days / 30:.0f} months."
