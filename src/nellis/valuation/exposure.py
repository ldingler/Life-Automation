"""Exposure control — the risk manager.

Proxy bidding means every open max bid is a live commitment. Put maxes on
twenty lots and you can win twenty lots. The failure mode that actually costs
money is not bidding too high on one item; it is bidding correctly on many
items simultaneously and winning more than you can pay for or move.

This module answers, before anything enters the bid queue: *if everything I am
currently leading goes my way, plus this, what do I owe?*
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Commitment, CommitmentStatus, Lot


@dataclass
class ExposureState:
    total: float
    lot_count: int
    by_category: dict[str, float] = field(default_factory=dict)
    commitments: list[Commitment] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total_exposure": round(self.total, 2),
            "open_lots": self.lot_count,
            "by_category": {k: round(v, 2) for k, v in self.by_category.items()},
        }


@dataclass
class ExposureDecision:
    allowed: bool
    worst_case_total: float
    headroom: float
    reason: str | None = None
    overlapping_lots: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "worst_case_total": round(self.worst_case_total, 2),
            "headroom": round(self.headroom, 2),
            "reason": self.reason,
            "overlapping_lots": self.overlapping_lots,
        }


def current_exposure(session: Session) -> ExposureState:
    """Everything you are currently on the hook for."""
    rows = session.scalars(
        select(Commitment).where(
            Commitment.status.in_([CommitmentStatus.LEADING, CommitmentStatus.OUTBID])
        )
    ).all()

    # An outbid commitment is not zero risk — you can be re-outbid back into the
    # lead at any moment, up to your max. Count it in full.
    total = sum(row.landed_at_max for row in rows)
    by_category: dict[str, float] = {}
    for row in rows:
        key = (row.category or "uncategorized").lower()
        by_category[key] = by_category.get(key, 0.0) + row.landed_at_max

    return ExposureState(
        total=total, lot_count=len(rows), by_category=by_category, commitments=list(rows)
    )


def find_overlapping(session: Session, lot: Lot, window_minutes: int) -> list[Lot]:
    """Open commitments closing near this lot — correlated win risk."""
    if lot.close_at is None:
        return []
    close_at = _aware(lot.close_at)
    if close_at is None:
        return []
    window = timedelta(minutes=window_minutes)

    rows = session.scalars(
        select(Lot)
        .join(Commitment, Commitment.lot_id == Lot.id)
        .where(
            Commitment.status.in_([CommitmentStatus.LEADING, CommitmentStatus.OUTBID]),
            Lot.id != lot.id,
            Lot.is_closed.is_(False),
        )
    ).all()

    overlapping = []
    for row in rows:
        other_close = _aware(row.close_at)
        if other_close is not None and abs(other_close - close_at) <= window:
            overlapping.append(row)
    return overlapping


def check_exposure(
    session: Session,
    lot: Lot,
    landed_at_max: float,
    *,
    max_total: float,
    max_lots: int,
    max_per_category: float,
    overlap_window_minutes: int = 30,
) -> ExposureDecision:
    """Would committing to this lot breach a cap?"""
    state = current_exposure(session)

    # Re-bidding a lot you already hold replaces that commitment, not adds to it.
    existing = session.scalar(select(Commitment).where(Commitment.lot_id == lot.id))
    baseline = state.total - (existing.landed_at_max if existing else 0.0)
    worst_case = baseline + landed_at_max
    headroom = max_total - worst_case

    category = (lot.category or "uncategorized").lower()
    category_total = state.by_category.get(category, 0.0)
    if existing and (existing.category or "uncategorized").lower() == category:
        category_total -= existing.landed_at_max
    category_worst = category_total + landed_at_max

    overlapping = find_overlapping(session, lot, overlap_window_minutes)
    overlap_ids = [row.nellis_id for row in overlapping]

    if worst_case > max_total:
        return ExposureDecision(
            allowed=False,
            worst_case_total=worst_case,
            headroom=headroom,
            reason=(
                f"Would put total exposure at ${worst_case:,.2f}, over your "
                f"${max_total:,.2f} cap. Win the open lots or lower the cap first."
            ),
            overlapping_lots=overlap_ids,
        )

    if not existing and state.lot_count + 1 > max_lots:
        return ExposureDecision(
            allowed=False,
            worst_case_total=worst_case,
            headroom=headroom,
            reason=f"Already tracking {state.lot_count} open bids (max {max_lots}).",
            overlapping_lots=overlap_ids,
        )

    if category_worst > max_per_category:
        return ExposureDecision(
            allowed=False,
            worst_case_total=worst_case,
            headroom=headroom,
            reason=(
                f"'{category}' exposure would hit ${category_worst:,.2f}, over the "
                f"${max_per_category:,.2f} per-category cap."
            ),
            overlapping_lots=overlap_ids,
        )

    reason = None
    if overlapping:
        reason = (
            f"{len(overlapping)} other open bid(s) close within "
            f"{overlap_window_minutes} min — you could win them together."
        )

    return ExposureDecision(
        allowed=True,
        worst_case_total=worst_case,
        headroom=headroom,
        reason=reason,
        overlapping_lots=overlap_ids,
    )


def rank_by_capital_efficiency(entries: list[tuple[float, float]]) -> list[int]:
    """Order candidates by profit per dollar of exposure.

    When the cap binds, this is how we choose what to cut: keep the deals that
    make the most money per dollar tied up, not simply the biggest headline
    profit. Returns indices, best first.
    """
    scored = [
        (index, (profit / exposure) if exposure > 0 else 0.0)
        for index, (profit, exposure) in enumerate(entries)
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [index for index, _ in scored]


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
