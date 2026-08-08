"""Persistence and close-price harvesting.

The close harvester is the most valuable thing in this codebase. Every time a
lot ends, its final price becomes a comp keyed by normalized item identity —
which is how the system learns what Nellis buyers *actually* pay, as opposed to
what an item is theoretically worth. That dataset is thin on day one and is the
system's durable advantage after a few weeks.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Comp, Lot, LotSnapshot
from ..normalize import normalize_item_key
from .adapter import LotRecord

log = logging.getLogger(__name__)

_UPDATABLE = (
    "title", "description", "category", "brand", "model", "upc",
    "condition_name", "condition_notes", "retail_price", "buyers_premium_rate",
    "location", "location_zip", "open_at", "close_at", "images", "url",
)


def upsert_lot(session: Session, record: LotRecord) -> tuple[Lot, bool]:
    """Insert or update a lot. Returns (lot, is_new)."""
    lot = session.scalar(select(Lot).where(Lot.nellis_id == record.nellis_id))
    is_new = lot is None
    if lot is None:
        lot = Lot(nellis_id=record.nellis_id, url=record.url, title=record.title)
        session.add(lot)

    for attr in _UPDATABLE:
        value = getattr(record, attr, None)
        if value is not None and value != []:
            setattr(lot, attr, value)

    # Bid only ratchets up; a stale/partial read must never lower it.
    if record.current_bid and record.current_bid >= (lot.current_bid or 0.0):
        lot.current_bid = record.current_bid
    if record.bid_count and record.bid_count >= (lot.bid_count or 0):
        lot.bid_count = record.bid_count

    if record.raw:
        lot.raw = record.raw
    lot.last_seen = datetime.now(UTC)

    if record.is_closed and not lot.is_closed:
        close_lot(session, lot, record.final_price or lot.current_bid)

    session.flush()
    return lot, is_new


def record_snapshot(session: Session, lot: Lot) -> LotSnapshot | None:
    """Append a time-series point, skipping no-op duplicates."""
    latest = session.scalar(
        select(LotSnapshot)
        .where(LotSnapshot.lot_id == lot.id)
        .order_by(LotSnapshot.observed_at.desc())
        .limit(1)
    )
    if (
        latest is not None
        and latest.current_bid == lot.current_bid
        and latest.bid_count == lot.bid_count
    ):
        return None

    snapshot = LotSnapshot(
        lot_id=lot.id,
        current_bid=lot.current_bid,
        bid_count=lot.bid_count,
        close_at=lot.close_at,
    )
    session.add(snapshot)
    return snapshot


def close_lot(session: Session, lot: Lot, final_price: float | None) -> Comp | None:
    """Mark a lot closed and convert its final price into a comp."""
    lot.is_closed = True
    lot.closed_at = datetime.now(UTC)
    if final_price is not None:
        lot.final_price = final_price
    if lot.final_price is None:
        return None

    key = normalize_item_key(lot.title, brand=lot.brand, model=lot.model, upc=lot.upc)
    existing = session.scalar(
        select(Comp).where(
            Comp.source == "nellis",
            Comp.query_key == key,
            Comp.url == lot.url,
        )
    )
    if existing is not None:
        return existing

    comp = Comp(
        source="nellis",
        query_key=key,
        title=lot.title,
        price=lot.final_price,
        shipping=0.0,
        condition=lot.condition_name,
        is_sold=True,
        sold_at=lot.closed_at,
        url=lot.url,
        raw={"retail_price": lot.retail_price, "category": lot.category},
    )
    session.add(comp)
    log.info("harvested comp: %s -> $%.2f (%s)", lot.title[:60], lot.final_price, key)
    return comp


def lots_due_for_refresh(
    session: Session, *, closing_soon_minutes: int, stale_minutes: int, limit: int = 200
) -> list[Lot]:
    """Open lots worth re-polling, closest-to-close first.

    Lots inside the closing window are always due; everything else is due once
    its last observation goes stale. This keeps the request budget spent where
    prices actually move.
    """
    now = datetime.now(UTC)
    soon = now + timedelta(minutes=closing_soon_minutes)
    stale_before = now - timedelta(minutes=stale_minutes)

    rows = session.scalars(
        select(Lot).where(Lot.is_closed.is_(False)).order_by(Lot.close_at.asc()).limit(limit * 3)
    ).all()

    due: list[Lot] = []
    for lot in rows:
        last_seen = _aware(lot.last_seen)
        close_at = _aware(lot.close_at)
        if close_at is not None and close_at <= now:
            due.append(lot)  # past close — needs a final read
        elif close_at is not None and close_at <= soon:
            due.append(lot)
        elif last_seen is None or last_seen <= stale_before:
            due.append(lot)
        if len(due) >= limit:
            break
    return due


def sweep_expired(session: Session, grace_minutes: int = 20) -> int:
    """Close out lots whose end time passed and that we can no longer observe.

    Without a final read we cannot know the hammer price, so we mark them closed
    using the last bid we saw. Flagged in `raw` so these weaker comps are
    distinguishable from confirmed ones.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=grace_minutes)
    stale = session.scalars(
        select(Lot).where(Lot.is_closed.is_(False), Lot.close_at.is_not(None))
    ).all()

    count = 0
    for lot in stale:
        close_at = _aware(lot.close_at)
        if close_at is None or close_at > cutoff:
            continue
        comp = close_lot(session, lot, lot.current_bid)
        if comp is not None and comp.raw is not None:
            comp.raw = {**comp.raw, "inferred_final": True}
        count += 1
    return count


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
