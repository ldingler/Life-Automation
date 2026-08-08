"""Scheduled jobs.

Four loops, each with a distinct job:

  sweep_watches    find new lots matching your saved searches, value them
  refresh_tracked  re-poll open lots — faster as they approach close
  harvest_closes   capture final prices into the comps database
  send_digest      one email, on your schedule, with everything that qualified

Request budget is finite, so `refresh_tracked` deliberately spends it on lots
near close (where prices actually move) rather than polling everything evenly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ..config import Settings, get_settings
from ..db import session_scope
from ..ingest import BlockedError, PoliteClient, default_chain
from ..ingest.harvest import (
    lots_due_for_refresh,
    record_snapshot,
    sweep_expired,
    upsert_lot,
)
from ..models import Lot, Valuation
from ..notify.email import send_closing_soon, send_digest
from ..search.watch import active_watches, run_watch
from ..valuation.engine import ValuationEngine
from ..valuation.exposure import current_exposure

log = logging.getLogger(__name__)


@dataclass
class JobResult:
    name: str
    ok: bool = True
    detail: str = ""
    counts: dict[str, int] = field(default_factory=dict)


async def sweep_watches(settings: Settings | None = None) -> JobResult:
    settings = settings or get_settings()
    result = JobResult(name="sweep_watches")

    with session_scope() as session:
        watches = active_watches(session)
        if not watches:
            result.detail = "no enabled watches"
            return result

        chain = default_chain()
        engine = ValuationEngine(session, settings)
        totals = {"seen": 0, "new": 0, "matched": 0, "recommended": 0, "queued": 0}

        async with PoliteClient(settings) as client:
            for watch in watches:
                try:
                    report = await run_watch(
                        session, watch, client=client, chain=chain, engine=engine, settings=settings
                    )
                except BlockedError as exc:
                    log.error("aborting sweep: %s", exc)
                    result.ok = False
                    result.detail = str(exc)
                    break
                totals["seen"] += report.seen
                totals["new"] += report.new_lots
                totals["matched"] += report.matched
                totals["recommended"] += report.recommended
                totals["queued"] += report.queued

        result.counts = totals
        result.detail = (
            f"{totals['seen']} lots seen, {totals['new']} new, "
            f"{totals['recommended']} recommended"
        )
    return result


async def refresh_tracked(settings: Settings | None = None) -> JobResult:
    """Re-poll open lots, prioritizing those near close."""
    settings = settings or get_settings()
    result = JobResult(name="refresh_tracked")

    with session_scope() as session:
        due = lots_due_for_refresh(
            session,
            closing_soon_minutes=settings.closing_soon_minutes,
            stale_minutes=settings.snapshot_poll_minutes,
            limit=120,
        )
        if not due:
            result.detail = "nothing due"
            return result

        chain = default_chain()
        updated = 0
        closed = 0

        async with PoliteClient(settings) as client:
            for lot in due:
                try:
                    record = await chain.fetch_lot(client, lot.nellis_id)
                except BlockedError as exc:
                    log.error("aborting refresh: %s", exc)
                    result.ok = False
                    result.detail = str(exc)
                    break
                except Exception as exc:
                    log.warning("refresh failed for %s: %s", lot.nellis_id, exc)
                    continue

                if record is None:
                    continue
                refreshed, _ = upsert_lot(session, record)
                record_snapshot(session, refreshed)
                updated += 1
                if refreshed.is_closed:
                    closed += 1

        closed += sweep_expired(session)
        result.counts = {"updated": updated, "closed": closed}
        result.detail = f"{updated} refreshed, {closed} closed"
    return result


async def harvest_closes(settings: Settings | None = None) -> JobResult:
    """Backstop: convert any lot past its close time into a comp."""
    settings = settings or get_settings()
    result = JobResult(name="harvest_closes")
    with session_scope() as session:
        count = sweep_expired(session)
        result.counts = {"harvested": count}
        result.detail = f"{count} lots harvested into comps"
    return result


async def revalue_open_lots(settings: Settings | None = None, limit: int = 80) -> JobResult:
    """Re-run valuation on open lots whose bids moved."""
    settings = settings or get_settings()
    result = JobResult(name="revalue")

    with session_scope() as session:
        lots = session.scalars(
            select(Lot)
            .where(Lot.is_closed.is_(False), Lot.close_at.is_not(None))
            .order_by(Lot.close_at.asc())
            .limit(limit)
        ).all()

        engine = ValuationEngine(session, settings, offline=True)
        count = 0
        for lot in lots:
            try:
                await engine.value(lot)
                count += 1
            except Exception as exc:
                log.warning("revalue failed for %s: %s", lot.nellis_id, exc)

        result.counts = {"revalued": count}
        result.detail = f"{count} lots revalued"
    return result


async def send_daily_digest(settings: Settings | None = None, dry_run: bool = False) -> JobResult:
    """Email everything that cleared the bar since the last digest."""
    settings = settings or get_settings()
    result = JobResult(name="digest")

    with session_scope() as session:
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        recent = session.scalars(
            select(Valuation)
            .where(Valuation.recommended.is_(True), Valuation.computed_at >= cutoff)
            .order_by(Valuation.projected_profit.desc())
            .limit(40)
        ).all()

        # Keep only the newest valuation per lot.
        seen: set[int] = set()
        results = []
        engine = ValuationEngine(session, settings, offline=True)
        for record in recent:
            if record.lot_id in seen:
                continue
            seen.add(record.lot_id)
            lot = session.get(Lot, record.lot_id)
            if lot is None or lot.is_closed:
                continue
            try:
                results.append(await engine.value(lot, check_exposure_caps=True))
            except Exception as exc:
                log.warning("digest revalue failed for %s: %s", lot.nellis_id, exc)

        results = [r for r in results if r.recommended]
        exposure = current_exposure(session).as_dict()
        email = await send_digest(
            session, results, settings=settings, dry_run=dry_run, exposure=exposure
        )
        result.counts = {"lots": len(results)}
        result.detail = email.subject if email else "nothing to send"
    return result


async def notify_closing_soon(settings: Settings | None = None, dry_run: bool = False) -> JobResult:
    settings = settings or get_settings()
    result = JobResult(name="closing_soon")

    with session_scope() as session:
        now = datetime.now(UTC)
        horizon = now + timedelta(minutes=settings.closing_soon_minutes)
        lots = session.scalars(
            select(Lot)
            .where(
                Lot.is_closed.is_(False),
                Lot.close_at.is_not(None),
                Lot.close_at <= horizon,
                Lot.close_at > now,
            )
            .order_by(Lot.close_at.asc())
            .limit(25)
        ).all()

        engine = ValuationEngine(session, settings, offline=True)
        results = []
        for lot in lots:
            try:
                valued = await engine.value(lot)
            except Exception as exc:
                log.warning("closing-soon valuation failed for %s: %s", lot.nellis_id, exc)
                continue
            if valued.recommended and valued.walk_away_max_bid > (lot.current_bid or 0):
                results.append(valued)

        email = await send_closing_soon(session, results, settings=settings, dry_run=dry_run)
        result.counts = {"lots": len(results)}
        result.detail = email.subject if email else "nothing to send"
    return result
