"""Valuation orchestrator.

Pipeline, in order:

    comps          -> what does this item actually sell for?
    confidence     -> how much do we trust that number?
    condition      -> what's wrong with this one?
    repair         -> is fixing it worth it, and what does that cost?
    resale         -> what do we net after fees and shipping?
    cost solver    -> the highest bid that still clears our margin
    exposure       -> can we afford to commit to it right now?

The output is one number you act on — `walk_away_max_bid` — plus the full
breakdown that justifies it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Confidence, Lot, Valuation
from .comps import CompsService, ItemQuery
from .comps.base import CompSet
from .condition import ConditionReport, analyze_condition
from .confidence import ConfidenceReport, score_confidence
from .cost import landed_cost, profit_at, walk_away_max_bid
from .exposure import ExposureDecision, check_exposure
from .repair import RepairAssessment, assess_repair
from .repair.base import PartsProvider
from .repair.providers import CatalogPartsProvider, CompositePartsProvider, quote_parts
from .resale import ResaleChannel, get_channel, net_proceeds

log = logging.getLogger(__name__)


@dataclass
class ValuationResult:
    """Everything the engine concluded, in one object."""

    lot: Lot
    comp_set: CompSet
    confidence: ConfidenceReport
    condition: ConditionReport
    repair: RepairAssessment | None
    channel: ResaleChannel

    gross_value: float
    net_resale: float
    effective_margin: float
    walk_away_max_bid: float
    landed_at_max: float
    projected_profit: float
    projected_margin: float
    profit_at_current_bid: float

    recommended: bool
    reason: str
    exposure: ExposureDecision | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "lot": {
                "nellis_id": self.lot.nellis_id,
                "title": self.lot.title,
                "url": self.lot.url,
                "current_bid": self.lot.current_bid,
                "retail_price": self.lot.retail_price,
                "close_at": self.lot.close_at.isoformat() if self.lot.close_at else None,
                "category": self.lot.category,
                "condition": self.lot.condition_name,
            },
            "comps": self.comp_set.as_dict(),
            "confidence": self.confidence.as_dict(),
            "condition_analysis": {
                "multiplier": self.condition.condition_multiplier,
                "summary": self.condition.summary,
                "signals": self.condition.signal_dicts,
                "missing_parts": self.condition.missing_parts,
            },
            "repair": self.repair.as_dict() if self.repair else None,
            "resale": {
                "channel": self.channel.label,
                "gross_value": round(self.gross_value, 2),
                "net_resale": round(self.net_resale, 2),
            },
            "verdict": {
                "walk_away_max_bid": round(self.walk_away_max_bid, 2),
                "landed_at_max": round(self.landed_at_max, 2),
                "projected_profit": round(self.projected_profit, 2),
                "projected_margin": round(self.projected_margin, 4),
                "profit_at_current_bid": round(self.profit_at_current_bid, 2),
                "effective_margin_required": round(self.effective_margin, 4),
                "recommended": self.recommended,
                "reason": self.reason,
            },
            "exposure": self.exposure.as_dict() if self.exposure else None,
            "warnings": self.warnings,
        }


class ValuationEngine:
    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        comps_service: CompsService | None = None,
        parts_provider: PartsProvider | None = None,
        *,
        offline: bool = False,
    ):
        self.session = session
        self.settings = settings or get_settings()
        self.comps = comps_service or CompsService(session, self.settings, offline=offline)
        self.parts = parts_provider or self._default_parts_provider(offline)

    def _default_parts_provider(self, offline: bool) -> PartsProvider:
        providers: list[PartsProvider] = []
        if not offline:
            from .repair.providers import EbayPartsProvider

            providers.append(EbayPartsProvider(self.session, self.settings))
        providers.append(CatalogPartsProvider(self.session))
        return CompositePartsProvider(providers)

    async def value(
        self,
        lot: Lot,
        *,
        target_margin: float | None = None,
        channel_key: str | None = None,
        check_exposure_caps: bool = True,
        max_repair_cost: float | None = None,
    ) -> ValuationResult:
        settings = self.settings
        warnings: list[str] = []

        # 1. What does it sell for?
        query = ItemQuery(
            title=lot.title,
            brand=lot.brand,
            model=lot.model,
            upc=lot.upc,
            category=lot.category,
        )
        comp_set = await self.comps.collect(query)

        # Fall back to a fraction of stated retail when we have no comps at all.
        functional_value = comp_set.value
        if functional_value is None:
            if lot.retail_price:
                functional_value = lot.retail_price * 0.35
                warnings.append(
                    "No comps found — falling back to 35% of stated retail. "
                    "Treat this number as a placeholder, not a valuation."
                )
            else:
                functional_value = 0.0
                warnings.append("No comps and no retail price — cannot value this lot.")

        # 2. How much do we trust it?
        confidence = score_confidence(
            comp_set,
            min_sample=settings.comps_min_sample,
            low_bump=settings.low_confidence_margin_bump,
            med_bump=settings.med_confidence_margin_bump,
        )

        # 3. What's wrong with this one?
        condition = analyze_condition(
            lot.title, lot.condition_name, lot.condition_notes, lot.description
        )

        # 4. Is fixing it worth it?
        repair: RepairAssessment | None = None
        if condition.missing_parts or condition.is_fatal:
            quotes, unpriced = await quote_parts(
                self.parts,
                condition.missing_parts,
                item_context=" ".join(filter(None, [lot.brand, lot.model])),
            )
            repair = assess_repair(
                functional_value=functional_value,
                condition_multiplier=condition.condition_multiplier,
                quotes=quotes,
                unpriced_parts=unpriced,
                is_fatal=condition.is_fatal,
                labor_rate=settings.labor_rate_per_hour,
                max_repair_cost=max_repair_cost or settings.__dict__.get("max_repair_cost"),
            )

        # 5. Decide as-is vs repaired, then net it out.
        # Parts and labor are subtracted exactly once, inside net_proceeds.
        if repair is not None and repair.is_worth_repairing:
            gross_value = functional_value * 0.94
            parts_cost = repair.parts_cost
            labor_hours = repair.labor_hours
        else:
            gross_value = functional_value * condition.condition_multiplier
            parts_cost = 0.0
            labor_hours = 0.0

        channel = get_channel(channel_key or settings.__dict__.get("default_channel"))
        proceeds = net_proceeds(
            gross_value,
            channel=channel,
            category=lot.category,
            parts_cost=parts_cost,
            labor_hours=labor_hours,
            labor_rate=settings.labor_rate_per_hour,
        )
        net_resale = proceeds.net

        # 6. Solve for the bid.
        base_margin = target_margin if target_margin is not None else settings.target_margin
        effective_margin = min(0.95, base_margin + confidence.margin_bump)

        bp_rate = lot.buyers_premium_rate or settings.default_buyers_premium
        tax_rate = settings.sales_tax_rate

        max_bid = walk_away_max_bid(
            net_resale,
            target_margin=effective_margin,
            bp_rate=bp_rate,
            tax_rate=tax_rate,
            pickup=settings.pickup_cost,
        )

        # Fatal damage forces the bid to zero rather than merely un-recommending
        # it. The scrap-value math still produces a plausible-looking number, and
        # a confident "$83" sitting next to "compressor is shot" is a trap — the
        # figure travels into emails and the extension panel, away from the
        # reason text that explains it.
        #
        # Profit is zeroed alongside it: with no bid there is no trade, and
        # leaving the pre-zeroing profit in place would report "max $0, profit
        # $170" — an incoherent pairing that invites someone to go bid anyway.
        landed = landed_cost(max_bid, bp_rate=bp_rate, tax_rate=tax_rate, pickup=settings.pickup_cost)
        if condition.is_fatal:
            max_bid = 0.0
            landed = landed_cost(0.0, bp_rate=bp_rate, tax_rate=tax_rate, pickup=0.0)
            projected_profit = 0.0
            projected_margin = 0.0
        else:
            projected_profit = net_resale - landed.total
            projected_margin = projected_profit / net_resale if net_resale > 0 else 0.0
        current_profit = profit_at(
            lot.current_bid or 0.0,
            net_resale,
            bp_rate=bp_rate,
            tax_rate=tax_rate,
            pickup=settings.pickup_cost,
        )

        # 7. Can we afford the commitment?
        exposure: ExposureDecision | None = None
        if check_exposure_caps and max_bid > 0:
            exposure = check_exposure(
                self.session,
                lot,
                landed.total,
                max_total=settings.max_open_exposure,
                max_lots=settings.max_open_lots,
                max_per_category=settings.max_per_category_exposure,
                overlap_window_minutes=settings.overlap_window_minutes,
            )

        recommended, reason = self._verdict(
            lot, max_bid, projected_profit, confidence, condition, exposure, settings
        )

        result = ValuationResult(
            lot=lot,
            comp_set=comp_set,
            confidence=confidence,
            condition=condition,
            repair=repair,
            channel=channel,
            gross_value=gross_value,
            net_resale=net_resale,
            effective_margin=effective_margin,
            walk_away_max_bid=max_bid,
            landed_at_max=landed.total,
            projected_profit=projected_profit,
            projected_margin=projected_margin,
            profit_at_current_bid=current_profit,
            recommended=recommended,
            reason=reason,
            exposure=exposure,
            warnings=warnings,
        )
        self._persist(result)
        return result

    def _verdict(
        self,
        lot: Lot,
        max_bid: float,
        projected_profit: float,
        confidence: ConfidenceReport,
        condition: ConditionReport,
        exposure: ExposureDecision | None,
        settings: Settings,
    ) -> tuple[bool, str]:
        current = lot.current_bid or 0.0

        # Fatal is checked first, before the generic max_bid <= 0 case. Fatal
        # damage zeroes the bid, so a later check would swallow it and report
        # "worth less than the cost of buying it" — true in effect, but it hides
        # the actual reason and reads as a pricing conclusion rather than a
        # condition one.
        if condition.is_fatal:
            return False, "Fatal damage — not worth pursuing except for parts."
        if max_bid <= 0:
            return False, "No viable bid — the item is worth less than the cost of buying it."
        if max_bid <= current:
            return False, (
                f"Already past your max. Bidding is at ${current:,.2f}; "
                f"your walk-away is ${max_bid:,.2f}."
            )
        if projected_profit < settings.min_profit_dollars:
            return False, (
                f"Too thin — projected profit ${projected_profit:,.2f} is under your "
                f"${settings.min_profit_dollars:,.2f} floor."
            )
        if confidence.level == Confidence.NONE:
            return False, "No comparable sales — cannot justify a bid."
        if exposure is not None and not exposure.allowed:
            return False, exposure.reason or "Blocked by exposure caps."

        note = ""
        if exposure is not None and exposure.reason:
            note = f" Note: {exposure.reason}"
        return True, (
            f"Bid up to ${max_bid:,.2f}. Projected profit ${projected_profit:,.2f} "
            f"({confidence.level.value} confidence).{note}"
        )

    def _persist(self, result: ValuationResult) -> Valuation:
        record = Valuation(
            lot_id=result.lot.id,
            computed_at=datetime.now(UTC),
            comp_value=result.comp_set.value,
            comp_count=result.comp_set.count,
            comp_sources=result.comp_set.sources,
            comp_spread=result.comp_set.spread,
            confidence=result.confidence.level,
            damage_signals=result.condition.signal_dicts,
            missing_parts=result.condition.missing_parts,
            parts_cost=result.repair.parts_cost if result.repair else 0.0,
            labor_hours=result.repair.labor_hours if result.repair else 0.0,
            easy_fix_score=result.repair.easy_fix_score if result.repair else None,
            repair_notes=result.repair.notes if result.repair else None,
            resale_channel=result.channel.key,
            net_resale=result.net_resale,
            walk_away_max_bid=result.walk_away_max_bid,
            landed_at_max=result.landed_at_max,
            projected_profit=result.projected_profit,
            projected_margin=result.projected_margin,
            profit_at_current_bid=result.profit_at_current_bid,
            recommended=result.recommended,
            reason=result.reason,
        )
        self.session.add(record)
        self.session.flush()
        return record
