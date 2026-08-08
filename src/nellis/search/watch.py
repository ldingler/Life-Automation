"""Watch execution: sweep Nellis for a saved search, persist, value, queue."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..ingest import AdapterChain, PoliteClient, SearchFilters, default_chain
from ..ingest.harvest import record_snapshot, upsert_lot
from ..models import BidQueueEntry, Lot, QueueStatus, Watch, WatchMatch
from ..valuation.engine import ValuationEngine, ValuationResult
from .matcher import match_reasons

log = logging.getLogger(__name__)


@dataclass
class SweepReport:
    watch_name: str
    seen: int = 0
    new_lots: int = 0
    matched: int = 0
    valued: int = 0
    recommended: int = 0
    queued: int = 0
    blocked: int = 0
    errors: list[str] = field(default_factory=list)
    results: list[ValuationResult] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.watch_name}: saw {self.seen}, {self.new_lots} new, "
            f"{self.matched} matched, {self.recommended} recommended, {self.queued} queued"
        )


def _search_terms(watch: Watch) -> list[str]:
    """Server-side query terms. Required terms first — they narrow the most."""
    from .matcher import _terms

    required = _terms(watch.require_all)
    keywords = _terms(watch.keywords)
    if required:
        return [" ".join(required[:2])]
    return keywords[:4] or [""]


async def run_watch(
    session: Session,
    watch: Watch,
    *,
    client: PoliteClient,
    chain: AdapterChain | None = None,
    engine: ValuationEngine | None = None,
    settings: Settings | None = None,
    max_pages: int = 2,
    value_matches: bool = True,
) -> SweepReport:
    """Execute one watch end to end."""
    settings = settings or get_settings()
    chain = chain or default_chain()
    engine = engine or ValuationEngine(session, settings)
    report = SweepReport(watch_name=watch.name)
    now = datetime.now(UTC)

    for term in _search_terms(watch):
        for page in range(1, max_pages + 1):
            filters = SearchFilters(
                query=term or None,
                category=(watch.categories or "").split(",")[0].strip() or None,
                page=page,
            )
            try:
                records = await chain.search(client, filters)
            except Exception as exc:
                report.errors.append(f"search '{term}' p{page}: {exc}")
                raise

            if not records:
                break
            report.seen += len(records)

            for record in records:
                lot, is_new = upsert_lot(session, record)
                if is_new:
                    report.new_lots += 1
                record_snapshot(session, lot)

                if match_reasons(lot, watch, now):
                    continue
                report.matched += 1
                _link_match(session, watch, lot)

                if not value_matches:
                    continue
                try:
                    result = await engine.value(
                        lot,
                        target_margin=watch.target_margin,
                        channel_key=watch.resale_channel,
                        max_repair_cost=watch.max_repair_cost,
                    )
                except Exception as exc:
                    report.errors.append(f"valuation {lot.nellis_id}: {exc}")
                    continue

                report.valued += 1
                report.results.append(result)
                if result.recommended:
                    report.recommended += 1
                    if _enqueue(session, result):
                        report.queued += 1
                elif result.exposure is not None and not result.exposure.allowed:
                    report.blocked += 1
                    _enqueue(session, result, blocked=True)

    watch.last_run_at = now
    session.flush()
    log.info(report.summary())
    return report


def _link_match(session: Session, watch: Watch, lot: Lot) -> None:
    exists = session.scalar(
        select(WatchMatch.id).where(WatchMatch.watch_id == watch.id, WatchMatch.lot_id == lot.id)
    )
    if not exists:
        session.add(WatchMatch(watch_id=watch.id, lot_id=lot.id))


def _enqueue(session: Session, result: ValuationResult, *, blocked: bool = False) -> bool:
    """Add or refresh a queue entry. Never places a bid — this is a suggestion."""
    entry = session.scalar(
        select(BidQueueEntry).where(BidQueueEntry.lot_id == result.lot.id)
    )
    status = QueueStatus.BLOCKED if blocked else QueueStatus.PENDING

    if entry is not None:
        # Don't resurrect something the human already decided on.
        if entry.status in (QueueStatus.CONFIRMED, QueueStatus.SKIPPED):
            return False
        entry.suggested_max_bid = result.walk_away_max_bid
        entry.projected_profit = result.projected_profit
        entry.exposure_if_won = result.landed_at_max
        entry.rank_score = _rank_score(result)
        entry.status = status
        entry.block_reason = (
            result.exposure.reason if blocked and result.exposure else None
        )
        return False

    session.add(
        BidQueueEntry(
            lot_id=result.lot.id,
            suggested_max_bid=result.walk_away_max_bid,
            projected_profit=result.projected_profit,
            exposure_if_won=result.landed_at_max,
            rank_score=_rank_score(result),
            status=status,
            block_reason=result.exposure.reason if blocked and result.exposure else None,
        )
    )
    return not blocked


def _rank_score(result: ValuationResult) -> float:
    """Profit per dollar of exposure, scaled by confidence.

    Ranking on raw profit would favor big-ticket lots that tie up all your
    capital. Profit-per-dollar is what actually compounds.
    """
    if result.landed_at_max <= 0:
        return 0.0
    efficiency = result.projected_profit / result.landed_at_max
    weights = {"high": 1.0, "medium": 0.8, "low": 0.55, "none": 0.2}
    return efficiency * weights.get(result.confidence.level.value, 0.5)


def active_watches(session: Session) -> list[Watch]:
    return list(session.scalars(select(Watch).where(Watch.enabled.is_(True))).all())
