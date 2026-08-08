"""Parts-sourcing interface and repair economics."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Rough time-to-fix by part type. Used to price your labor honestly — a repair
# that "only costs $8 in parts" is not free if it eats an hour.
LABOR_HOURS: dict[str, float] = {
    "manual": 0.05, "screws": 0.15, "hardware": 0.25, "bolts": 0.2,
    "lid": 0.05, "cover": 0.1, "cap": 0.05, "remote": 0.05, "remote control": 0.05,
    "charger": 0.05, "power cord": 0.1, "cord": 0.1, "cable": 0.05, "adapter": 0.05,
    "battery": 0.25, "filter": 0.15, "filters": 0.15, "blade": 0.3, "blades": 0.35,
    "hose": 0.2, "handle": 0.4, "knob": 0.2, "shelf": 0.1, "tray": 0.05,
    "basket": 0.05, "rack": 0.1, "drawer": 0.3, "bag": 0.05, "case": 0.05,
    "stand": 0.3, "mount": 0.35, "bracket": 0.3, "wheel": 0.4, "wheels": 0.6,
    "gasket": 0.5, "seal": 0.5, "belt": 0.6, "pump": 1.0, "motor": 2.0,
}
DEFAULT_LABOR_HOURS = 0.35


@dataclass
class PartQuote:
    part: str
    price: float
    source: str
    availability: str = "unknown"
    url: str | None = None
    confident: bool = True

    def as_dict(self) -> dict:
        return {
            "part": self.part,
            "price": round(self.price, 2),
            "source": self.source,
            "availability": self.availability,
            "url": self.url,
        }


class PartsProvider(ABC):
    """Prices a replacement part. Implementations may be catalogs or live lookups."""

    name: str = "abstract"
    enabled: bool = True

    @abstractmethod
    async def quote(self, part: str, *, item_context: str = "") -> PartQuote | None:
        ...


@dataclass
class RepairAssessment:
    parts_cost: float
    labor_hours: float
    quotes: list[PartQuote] = field(default_factory=list)
    unpriced_parts: list[str] = field(default_factory=list)
    repaired_value: float = 0.0
    as_is_value: float = 0.0
    value_uplift: float = 0.0
    easy_fix_score: float | None = None
    is_worth_repairing: bool = False
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "parts_cost": round(self.parts_cost, 2),
            "labor_hours": round(self.labor_hours, 2),
            "quotes": [q.as_dict() for q in self.quotes],
            "unpriced_parts": self.unpriced_parts,
            "repaired_value": round(self.repaired_value, 2),
            "as_is_value": round(self.as_is_value, 2),
            "value_uplift": round(self.value_uplift, 2),
            "easy_fix_score": round(self.easy_fix_score, 2)
            if self.easy_fix_score is not None
            else None,
            "is_worth_repairing": self.is_worth_repairing,
            "notes": self.notes,
        }


def labor_for(part: str) -> float:
    return LABOR_HOURS.get(part.lower().strip(), DEFAULT_LABOR_HOURS)


def assess_repair(
    *,
    functional_value: float,
    condition_multiplier: float,
    quotes: list[PartQuote],
    unpriced_parts: list[str],
    is_fatal: bool,
    labor_rate: float = 25.0,
    max_repair_cost: float | None = None,
) -> RepairAssessment:
    """Decide whether making the item whole pays.

    `as_is_value`   — what it fetches broken/incomplete
    `repaired_value`— what it fetches whole, minus the cost of getting there
    We recommend repair only when the second beats the first by a real margin.
    """
    parts_cost = sum(q.price for q in quotes)
    labor_hours = sum(labor_for(q.part) for q in quotes) + (
        DEFAULT_LABOR_HOURS * len(unpriced_parts)
    )
    labor_cost = labor_hours * labor_rate

    as_is_value = functional_value * condition_multiplier

    if is_fatal:
        return RepairAssessment(
            parts_cost=parts_cost,
            labor_hours=labor_hours,
            quotes=quotes,
            unpriced_parts=unpriced_parts,
            repaired_value=as_is_value,
            as_is_value=as_is_value,
            value_uplift=0.0,
            easy_fix_score=None,
            is_worth_repairing=False,
            notes="Fatal damage detected — priced as parts/scrap, no repair path assumed.",
        )

    # Repaired items don't fetch full retail; "repaired" carries its own discount.
    repaired_gross = functional_value * 0.94
    repaired_net = repaired_gross - parts_cost - labor_cost
    uplift = repaired_net - as_is_value

    total_repair_cost = parts_cost + labor_cost
    easy_fix_score = (uplift / total_repair_cost) if total_repair_cost > 0.5 else None

    worth_it = uplift > 0 and (max_repair_cost is None or parts_cost <= max_repair_cost)

    if unpriced_parts:
        note = (
            f"Could not price: {', '.join(unpriced_parts)}. "
            "Estimate assumes these are cheap — verify before bidding."
        )
    elif worth_it and easy_fix_score and easy_fix_score >= 2.0:
        note = (
            f"Easy fix: ${parts_cost:.2f} in parts + {labor_hours:.1f}h recovers "
            f"${uplift:.2f} of value."
        )
    elif worth_it:
        note = f"Repair is marginally profitable (+${uplift:.2f})."
    else:
        note = "Repair does not pay — value it as-is."

    return RepairAssessment(
        parts_cost=parts_cost,
        labor_hours=labor_hours,
        quotes=quotes,
        unpriced_parts=unpriced_parts,
        repaired_value=max(as_is_value, repaired_net) if worth_it else as_is_value,
        as_is_value=as_is_value,
        value_uplift=max(0.0, uplift),
        easy_fix_score=easy_fix_score,
        is_worth_repairing=worth_it,
        notes=note,
    )
