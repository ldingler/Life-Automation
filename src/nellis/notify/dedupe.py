"""Notification deduplication.

An alerting system that cries wolf gets filtered to spam, and then it may as
well not exist. Rules:

  * one alert per (kind, lot, price-bucket) — re-alert only when something
    materially changed, not on every poll
  * closing-soon fires once per lot, ever
  * digests are keyed by day
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Alert

log = logging.getLogger(__name__)


def deal_key(lot_nellis_id: str, max_bid: float) -> str:
    """Bucket by $5 of recommended bid so small drifts don't re-alert."""
    bucket = int(max_bid // 5)
    return f"deal:{lot_nellis_id}:{bucket}"


def closing_key(lot_nellis_id: str) -> str:
    return f"closing:{lot_nellis_id}"


def digest_key(when: datetime | None = None) -> str:
    when = when or datetime.now(UTC)
    return f"digest:{when:%Y-%m-%d}"


def outbid_key(lot_nellis_id: str, current_bid: float) -> str:
    return f"outbid:{lot_nellis_id}:{int(current_bid)}"


def already_sent(session: Session, key: str, *, within_hours: int | None = None) -> bool:
    query = select(Alert).where(Alert.dedupe_key == key)
    alert = session.scalar(query)
    if alert is None:
        return False
    if within_hours is None:
        return True
    sent_at = alert.sent_at
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - sent_at < timedelta(hours=within_hours)


def record_sent(
    session: Session, kind: str, key: str, *, lot_id: int | None = None, subject: str | None = None
) -> Alert:
    alert = Alert(kind=kind, dedupe_key=key, lot_id=lot_id, subject=subject)
    session.add(alert)
    session.flush()
    return alert


def filter_unsent(session: Session, candidates: list[tuple[str, object]]) -> list[object]:
    """Given (key, payload) pairs, return payloads not yet alerted on."""
    return [payload for key, payload in candidates if not already_sent(session, key)]
