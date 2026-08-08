"""JSON API consumed by the browser extension.

Contract note that matters: this API *suggests* and *records*. `confirm` means
"I placed this bid myself" — it writes a Commitment so exposure tracking stays
accurate. Nothing here talks to Nellis or places a bid.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session_factory
from ..models import (
    BidQueueEntry,
    Commitment,
    CommitmentStatus,
    Lot,
    PortfolioItem,
    QueueStatus,
    Valuation,
)
from ..valuation.exposure import current_exposure

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["queue"])


def get_db() -> Session:
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def require_token(x_api_token: str | None = Header(default=None)) -> None:
    """Shared-secret auth. Only enforced when API_TOKEN is configured."""
    expected = get_settings().api_token
    if expected and x_api_token != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Token")


class QueueItem(BaseModel):
    queue_id: int
    nellis_id: str
    title: str
    url: str
    current_bid: float
    retail_price: float | None = None
    suggested_max_bid: float
    projected_profit: float | None = None
    projected_margin: float | None = None
    comp_value: float | None = None
    comp_count: int = 0
    confidence: str = "none"
    condition: str | None = None
    close_at: datetime | None = None
    exposure_if_won: float | None = None
    rank_score: float = 0.0
    status: str
    block_reason: str | None = None
    repair_notes: str | None = None
    missing_parts: list[str] = Field(default_factory=list)
    reason: str | None = None


class ExposureOut(BaseModel):
    total_exposure: float
    open_lots: int
    max_exposure: float
    headroom: float
    by_category: dict[str, float]


class ConfirmIn(BaseModel):
    max_bid: float | None = None
    note: str | None = None


def _latest_valuation(session: Session, lot_id: int) -> Valuation | None:
    return session.scalar(
        select(Valuation)
        .where(Valuation.lot_id == lot_id)
        .order_by(Valuation.computed_at.desc())
        .limit(1)
    )


def _to_item(session: Session, entry: BidQueueEntry) -> QueueItem | None:
    lot = session.get(Lot, entry.lot_id)
    if lot is None:
        return None
    valuation = _latest_valuation(session, lot.id)
    return QueueItem(
        queue_id=entry.id,
        nellis_id=lot.nellis_id,
        title=lot.title,
        url=lot.url,
        current_bid=lot.current_bid or 0.0,
        retail_price=lot.retail_price,
        suggested_max_bid=entry.suggested_max_bid,
        projected_profit=entry.projected_profit,
        projected_margin=valuation.projected_margin if valuation else None,
        comp_value=valuation.comp_value if valuation else None,
        comp_count=valuation.comp_count if valuation else 0,
        confidence=valuation.confidence.value if valuation else "none",
        condition=lot.condition_name,
        close_at=lot.close_at,
        exposure_if_won=entry.exposure_if_won,
        rank_score=entry.rank_score,
        status=entry.status.value,
        block_reason=entry.block_reason,
        repair_notes=valuation.repair_notes if valuation else None,
        missing_parts=(valuation.missing_parts or []) if valuation else [],
        reason=valuation.reason if valuation else None,
    )


@router.get("/queue", response_model=list[QueueItem])
def read_queue(
    session: Session = Depends(get_db),
    _: None = Depends(require_token),
    include_blocked: bool = Query(default=False),
    limit: int = Query(default=50, le=200),
) -> list[QueueItem]:
    """Pending recommendations, best profit-per-dollar first."""
    statuses = [QueueStatus.PENDING]
    if include_blocked:
        statuses.append(QueueStatus.BLOCKED)

    entries = session.scalars(
        select(BidQueueEntry)
        .where(BidQueueEntry.status.in_(statuses))
        .order_by(BidQueueEntry.rank_score.desc())
        .limit(limit)
    ).all()

    items = []
    now = datetime.now(UTC)
    for entry in entries:
        item = _to_item(session, entry)
        if item is None:
            continue
        # Drop anything already closed out from under us.
        if item.close_at is not None:
            close_at = (
                item.close_at.replace(tzinfo=UTC)
                if item.close_at.tzinfo is None
                else item.close_at
            )
            if close_at < now:
                entry.status = QueueStatus.EXPIRED
                continue
        items.append(item)
    return items


@router.get("/lot/{nellis_id}", response_model=QueueItem)
def read_lot(
    nellis_id: str,
    session: Session = Depends(get_db),
    _: None = Depends(require_token),
) -> QueueItem:
    """Valuation for one lot — what the extension shows on a lot page."""
    lot = session.scalar(select(Lot).where(Lot.nellis_id == nellis_id))
    if lot is None:
        raise HTTPException(status_code=404, detail="lot not tracked yet")

    entry = session.scalar(select(BidQueueEntry).where(BidQueueEntry.lot_id == lot.id))
    if entry is not None:
        item = _to_item(session, entry)
        if item is not None:
            return item

    valuation = _latest_valuation(session, lot.id)
    if valuation is None:
        raise HTTPException(status_code=404, detail="lot not valued yet")

    return QueueItem(
        queue_id=0,
        nellis_id=lot.nellis_id,
        title=lot.title,
        url=lot.url,
        current_bid=lot.current_bid or 0.0,
        retail_price=lot.retail_price,
        suggested_max_bid=valuation.walk_away_max_bid,
        projected_profit=valuation.projected_profit,
        projected_margin=valuation.projected_margin,
        comp_value=valuation.comp_value,
        comp_count=valuation.comp_count,
        confidence=valuation.confidence.value,
        condition=lot.condition_name,
        close_at=lot.close_at,
        exposure_if_won=valuation.landed_at_max,
        status="pending",
        repair_notes=valuation.repair_notes,
        missing_parts=valuation.missing_parts or [],
        reason=valuation.reason,
    )


@router.post("/queue/{queue_id}/confirm", response_model=ExposureOut)
def confirm_bid(
    queue_id: int,
    payload: ConfirmIn,
    session: Session = Depends(get_db),
    _: None = Depends(require_token),
) -> ExposureOut:
    """Record that YOU placed this max bid on Nellis.

    This does not bid. It books the commitment so exposure stays correct.
    """
    entry = session.get(BidQueueEntry, queue_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="queue entry not found")

    lot = session.get(Lot, entry.lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="lot not found")

    max_bid = payload.max_bid if payload.max_bid is not None else entry.suggested_max_bid
    settings = get_settings()
    bp = lot.buyers_premium_rate or settings.default_buyers_premium

    from ..valuation.cost import landed_cost

    landed = landed_cost(
        max_bid, bp_rate=bp, tax_rate=settings.sales_tax_rate, pickup=settings.pickup_cost
    )

    entry.status = QueueStatus.CONFIRMED
    entry.acted_at = datetime.now(UTC)

    commitment = session.scalar(select(Commitment).where(Commitment.lot_id == lot.id))
    if commitment is None:
        commitment = Commitment(
            lot_id=lot.id,
            max_bid=max_bid,
            landed_at_max=landed.total,
            category=lot.category,
            status=CommitmentStatus.LEADING,
        )
        session.add(commitment)
    else:
        commitment.max_bid = max_bid
        commitment.landed_at_max = landed.total
        commitment.status = CommitmentStatus.LEADING
    session.flush()

    return _exposure_out(session)


@router.post("/queue/{queue_id}/skip", response_model=ExposureOut)
def skip_entry(
    queue_id: int,
    session: Session = Depends(get_db),
    _: None = Depends(require_token),
) -> ExposureOut:
    entry = session.get(BidQueueEntry, queue_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="queue entry not found")
    entry.status = QueueStatus.SKIPPED
    entry.acted_at = datetime.now(UTC)
    session.flush()
    return _exposure_out(session)


@router.get("/exposure", response_model=ExposureOut)
def read_exposure(
    session: Session = Depends(get_db), _: None = Depends(require_token)
) -> ExposureOut:
    return _exposure_out(session)


def _exposure_out(session: Session) -> ExposureOut:
    settings = get_settings()
    state = current_exposure(session)
    return ExposureOut(
        total_exposure=round(state.total, 2),
        open_lots=state.lot_count,
        max_exposure=settings.max_open_exposure,
        headroom=round(max(0.0, settings.max_open_exposure - state.total), 2),
        by_category={k: round(v, 2) for k, v in state.by_category.items()},
    )


class ResolveIn(BaseModel):
    won: bool
    hammer_price: float | None = None


@router.post("/commitment/{nellis_id}/resolve")
def resolve_commitment(
    nellis_id: str,
    payload: ResolveIn,
    session: Session = Depends(get_db),
    _: None = Depends(require_token),
) -> dict:
    """Close out a commitment once the lot ends — frees exposure."""
    lot = session.scalar(select(Lot).where(Lot.nellis_id == nellis_id))
    if lot is None:
        raise HTTPException(status_code=404, detail="lot not found")
    commitment = session.scalar(select(Commitment).where(Commitment.lot_id == lot.id))
    if commitment is None:
        raise HTTPException(status_code=404, detail="no commitment for this lot")

    commitment.status = CommitmentStatus.WON if payload.won else CommitmentStatus.LOST
    commitment.resolved_at = datetime.now(UTC)

    if payload.won:
        hammer = payload.hammer_price or commitment.max_bid
        settings = get_settings()
        from ..valuation.cost import landed_cost

        landed = landed_cost(
            hammer,
            bp_rate=lot.buyers_premium_rate or settings.default_buyers_premium,
            tax_rate=settings.sales_tax_rate,
            pickup=settings.pickup_cost,
        )
        valuation = _latest_valuation(session, lot.id)
        session.add(
            PortfolioItem(
                lot_id=lot.id,
                hammer_price=hammer,
                landed_cost=landed.total,
                projected_profit=valuation.projected_profit if valuation else None,
            )
        )
    session.flush()
    return {"status": commitment.status.value, "exposure": _exposure_out(session).model_dump()}
