"""Confidence scoring.

Every estimate carries how much to trust it. Thin or scattered comps don't
block a deal — they *widen the required margin*, so uncertainty is priced in
rather than hand-waved. That keeps the system usable on day one, when the
Nellis comps database is still empty.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Confidence
from .comps.base import CompSet


@dataclass(frozen=True)
class ConfidenceReport:
    level: Confidence
    score: float  # 0..1
    margin_bump: float
    reasons: list[str]

    def as_dict(self) -> dict:
        return {
            "level": self.level.value,
            "score": round(self.score, 3),
            "margin_bump": round(self.margin_bump, 3),
            "reasons": self.reasons,
        }


def score_confidence(
    comp_set: CompSet,
    *,
    min_sample: int = 3,
    low_bump: float = 0.15,
    med_bump: float = 0.07,
) -> ConfidenceReport:
    reasons: list[str] = []

    if comp_set.value is None or comp_set.count == 0:
        return ConfidenceReport(
            level=Confidence.NONE,
            score=0.0,
            margin_bump=low_bump * 2,
            reasons=["no comparable sales found"],
        )

    # Sample size — the dominant term.
    if comp_set.count >= 12:
        sample_score = 1.0
    elif comp_set.count >= 6:
        sample_score = 0.75
    elif comp_set.count >= min_sample:
        sample_score = 0.5
    else:
        sample_score = 0.25
        reasons.append(f"only {comp_set.count} comp(s)")

    # Dispersion — tight clusters are trustworthy, wide ones are not.
    if comp_set.spread is None:
        spread_score = 0.6
    elif comp_set.spread <= 0.25:
        spread_score = 1.0
    elif comp_set.spread <= 0.5:
        spread_score = 0.75
    elif comp_set.spread <= 0.9:
        spread_score = 0.45
        reasons.append(f"wide price spread ({comp_set.spread:.0%} of value)")
    else:
        spread_score = 0.2
        reasons.append(f"very wide price spread ({comp_set.spread:.0%} of value)")

    # Recency.
    age = comp_set.median_age_days
    if age is None:
        age_score = 0.5
    elif age <= 30:
        age_score = 1.0
    elif age <= 60:
        age_score = 0.8
    elif age <= 90:
        age_score = 0.6
    else:
        age_score = 0.35
        reasons.append(f"comps are stale (median {age:.0f} days old)")

    # Source agreement — sold data beats asking prices.
    sold_sources = {s for s in comp_set.sources if s in ("nellis", "csv", "ebay_sold")}
    if sold_sources and len(comp_set.sources) > 1:
        source_score = 1.0
    elif sold_sources:
        source_score = 0.85
    else:
        source_score = 0.45
        reasons.append("only active listings (asking prices), no confirmed sales")

    score = 0.40 * sample_score + 0.25 * spread_score + 0.20 * age_score + 0.15 * source_score

    if score >= 0.78:
        level, bump = Confidence.HIGH, 0.0
    elif score >= 0.55:
        level, bump = Confidence.MEDIUM, med_bump
    else:
        level, bump = Confidence.LOW, low_bump

    if not reasons:
        reasons.append(
            f"{comp_set.count} comps from {', '.join(comp_set.sources)}, tightly clustered"
        )

    return ConfidenceReport(level=level, score=score, margin_bump=bump, reasons=reasons)
