"""How much a purchase actually saved.

Savings is a subtraction, and the whole question is what you subtract from.
Liquidation listings routinely carry an inflated "retail" price, so measuring
against it produces a flattering number that is not true — and for MSS these
figures land in company expense records, where an indefensible number is worse
than no number.

So the reference value is chosen by strength of evidence and always recorded
alongside the figure:

    COMPS            what the thing actually sells for second-hand. Strongest.
    RETAIL_VERIFIED  stated retail, corroborated by comps within tolerance.
    RETAIL_STATED    stated retail, uncorroborated. Discounted, and flagged.
    NONE             nothing defensible to compare against — no claim made.

Cost is always the full landed cost: hammer + 15% buyer's premium + sales tax on
both. Comparing a hammer price to retail would overstate every saving by about
23%, which is the single easiest way to fool yourself here.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import PortfolioItem, ValueBasis
from ..valuation.cost import landed_cost

# How far stated retail may exceed comps before it stops being credible.
RETAIL_CREDIBILITY_RATIO = 2.5
# Applied to uncorroborated stated retail, so a savings claim leans conservative.
UNVERIFIED_RETAIL_HAIRCUT = 0.7


@dataclass
class SavingsResult:
    reference_value: float | None
    basis: ValueBasis
    landed_cost: float
    savings: float | None
    savings_pct: float | None
    note: str

    @property
    def is_defensible(self) -> bool:
        return self.basis in (ValueBasis.COMPS, ValueBasis.RETAIL_VERIFIED)

    def as_dict(self) -> dict:
        return {
            "reference_value": round(self.reference_value, 2)
            if self.reference_value is not None
            else None,
            "basis": self.basis.value,
            "landed_cost": round(self.landed_cost, 2),
            "savings": round(self.savings, 2) if self.savings is not None else None,
            "savings_pct": round(self.savings_pct, 3) if self.savings_pct is not None else None,
            "defensible": self.is_defensible,
            "note": self.note,
        }


def choose_reference(
    *, retail_price: float | None, comp_value: float | None, comp_count: int = 0
) -> tuple[float | None, ValueBasis, str]:
    """Pick what to measure savings against, and say why."""
    has_comps = comp_value is not None and comp_value > 0 and comp_count >= 3

    if has_comps:
        if retail_price and retail_price <= comp_value * RETAIL_CREDIBILITY_RATIO:
            # Both agree. Use comps — what it actually resells for is the more
            # honest measure of what you'd otherwise have paid second-hand.
            return comp_value, ValueBasis.RETAIL_VERIFIED, (
                f"{comp_count} comparable sales, consistent with the ${retail_price:,.0f} "
                "retail claim"
            )
        if retail_price:
            return comp_value, ValueBasis.COMPS, (
                f"stated retail ${retail_price:,.0f} looks inflated against "
                f"{comp_count} comparable sales — measured against comps instead"
            )
        return comp_value, ValueBasis.COMPS, f"{comp_count} comparable sales"

    if retail_price and retail_price > 0:
        conservative = retail_price * UNVERIFIED_RETAIL_HAIRCUT
        return conservative, ValueBasis.RETAIL_STATED, (
            f"no comps yet — using {UNVERIFIED_RETAIL_HAIRCUT:.0%} of the stated "
            f"${retail_price:,.0f} retail, which is unverified"
        )

    return None, ValueBasis.NONE, "no retail price and no comps — savings can't be claimed"


def compute_savings(
    *,
    hammer_price: float,
    retail_price: float | None,
    comp_value: float | None = None,
    comp_count: int = 0,
    bp_rate: float = 0.15,
    tax_rate: float = 0.06625,
    pickup: float = 0.0,
    repair_spend: float = 0.0,
    landed_override: float | None = None,
) -> SavingsResult:
    """Savings for one purchase, against the strongest available reference."""
    landed = (
        landed_override
        if landed_override is not None
        else landed_cost(hammer_price, bp_rate=bp_rate, tax_rate=tax_rate, pickup=pickup).total
    )
    landed += repair_spend

    reference, basis, note = choose_reference(
        retail_price=retail_price, comp_value=comp_value, comp_count=comp_count
    )

    if reference is None:
        return SavingsResult(None, basis, landed, None, None, note)

    savings = reference - landed
    pct = (savings / reference) if reference > 0 else None
    return SavingsResult(reference, basis, landed, savings, pct, note)


def apply_to_item(
    item: PortfolioItem,
    *,
    retail_price: float | None,
    comp_value: float | None = None,
    comp_count: int = 0,
    bp_rate: float = 0.15,
    tax_rate: float = 0.06625,
    pickup: float = 0.0,
) -> SavingsResult:
    """Compute and persist savings onto a portfolio row."""
    result = compute_savings(
        hammer_price=item.hammer_price,
        retail_price=retail_price,
        comp_value=comp_value,
        comp_count=comp_count,
        bp_rate=bp_rate,
        tax_rate=tax_rate,
        pickup=pickup,
        repair_spend=item.repair_spend or 0.0,
        landed_override=item.landed_cost,
    )
    item.reference_value = result.reference_value
    item.value_basis = result.basis
    item.savings = result.savings
    return result


@dataclass
class SavingsTotals:
    """Portfolio-wide savings, split by how trustworthy the basis is."""

    defensible_savings: float = 0.0
    unverified_savings: float = 0.0
    total_spent: float = 0.0
    item_count: int = 0
    defensible_count: int = 0
    unverified_count: int = 0
    unpriced_count: int = 0

    @property
    def headline_savings(self) -> float:
        """The number to lead with — only what's actually defensible."""
        return self.defensible_savings

    @property
    def total_savings_including_unverified(self) -> float:
        return self.defensible_savings + self.unverified_savings

    @property
    def effective_discount(self) -> float | None:
        basis = self.total_spent + self.defensible_savings
        return self.defensible_savings / basis if basis > 0 else None

    def as_dict(self) -> dict:
        return {
            "headline_savings": round(self.headline_savings, 2),
            "unverified_savings": round(self.unverified_savings, 2),
            "total_including_unverified": round(self.total_savings_including_unverified, 2),
            "total_spent": round(self.total_spent, 2),
            "items": self.item_count,
            "defensible_items": self.defensible_count,
            "unverified_items": self.unverified_count,
            "unpriced_items": self.unpriced_count,
            "effective_discount": round(self.effective_discount, 3)
            if self.effective_discount is not None
            else None,
        }


def total_savings(items: list[PortfolioItem]) -> SavingsTotals:
    """Roll savings up, keeping verified and unverified apart.

    They are deliberately not summed into one figure by default: mixing a
    comps-backed saving with one measured off an unverified retail claim makes
    the total no more trustworthy than its weakest input.
    """
    totals = SavingsTotals()
    for item in items:
        totals.item_count += 1
        totals.total_spent += (item.landed_cost or 0.0) + (item.repair_spend or 0.0)

        if item.savings is None:
            totals.unpriced_count += 1
            continue
        if item.value_basis in (ValueBasis.COMPS, ValueBasis.RETAIL_VERIFIED):
            totals.defensible_savings += item.savings
            totals.defensible_count += 1
        else:
            totals.unverified_savings += item.savings
            totals.unverified_count += 1
    return totals
