"""APScheduler wiring."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..config import Settings, get_settings
from .jobs import (
    JobResult,
    harvest_closes,
    notify_closing_soon,
    refresh_tracked,
    revalue_open_lots,
    send_daily_digest,
    sweep_watches,
)

log = logging.getLogger(__name__)

__all__ = [
    "JobResult",
    "build_scheduler",
    "harvest_closes",
    "notify_closing_soon",
    "refresh_tracked",
    "revalue_open_lots",
    "send_daily_digest",
    "sweep_watches",
]


async def _run(job) -> None:
    try:
        result = await job()
        level = logging.INFO if result.ok else logging.ERROR
        log.log(level, "[%s] %s", result.name, result.detail)
    except Exception:
        log.exception("job %s crashed", getattr(job, "__name__", job))


def build_scheduler(settings: Settings | None = None) -> AsyncIOScheduler:
    settings = settings or get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    scheduler.add_job(
        _run, IntervalTrigger(minutes=settings.watch_sweep_minutes),
        args=[sweep_watches], id="sweep_watches", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        _run, IntervalTrigger(minutes=settings.snapshot_poll_minutes),
        args=[refresh_tracked], id="refresh_tracked", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        _run, IntervalTrigger(minutes=settings.harvest_interval_minutes),
        args=[harvest_closes], id="harvest_closes", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        _run, IntervalTrigger(minutes=max(10, settings.snapshot_poll_minutes * 2)),
        args=[revalue_open_lots], id="revalue", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        _run, CronTrigger(hour=settings.digest_hour_local, minute=0),
        args=[send_daily_digest], id="digest", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        _run, IntervalTrigger(minutes=30),
        args=[notify_closing_soon], id="closing_soon", max_instances=1, coalesce=True,
    )
    return scheduler
