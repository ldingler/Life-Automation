"""The lookup queue: what to check, when the browser is allowed to check it.

None of the sites that matter — Amazon, Walmart, Facebook Marketplace — will
give out a usable price API. So verification runs the only way that needs no
credentials and misrepresents nothing: the operator's own browser opens the
same search page they'd open by hand, reads it, and closes it.

That makes pacing the whole design problem. A person comparison-shopping does a
handful of searches an hour, not two hundred, so the queue enforces exactly
that shape:

  * one search at a time, never concurrent
  * a floor on the gap between any two searches, and a much larger floor
    between two searches at the *same* site
  * a hard daily ceiling
  * and if a site puts up a CAPTCHA or an "unusual activity" wall, that site
    goes quiet for hours

The last one is the important one. A block is a site saying *stop*, and the
response is to stop — not to slow down slightly and try again, and not to work
around it. If verification coverage suffers because of that, the honest answer
is thinner coverage, which the dashboard shows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import LookupJob, LookupSite, LookupStatus, Lot, MarketPrice
from ..normalize import key_family, normalize_item_key, search_query
from .classify import classify_all
from .lookup import record_prices

log = logging.getLogger(__name__)

TERMINAL = (LookupStatus.DONE, LookupStatus.EMPTY, LookupStatus.BLOCKED, LookupStatus.FAILED)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# Building the search URL
# --------------------------------------------------------------------------


def search_url(site: LookupSite, query: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    templates = {
        LookupSite.AMAZON: settings.amazon_search_template,
        LookupSite.WALMART: settings.walmart_search_template,
        LookupSite.FACEBOOK: settings.facebook_search_template,
    }
    return templates[site].format(q=quote_plus(query))


# --------------------------------------------------------------------------
# Enqueueing
# --------------------------------------------------------------------------


def priority_for_lot(lot: Lot, *, now: datetime | None = None) -> float:
    """How much a price answer is worth for this lot, right now.

    Dollars at stake, sharply boosted as the close approaches — a $400 lot
    closing in twenty minutes is the one case where a late answer is the same
    as no answer.
    """
    now = now or _now()
    stake = max(lot.current_bid or 0.0, (lot.retail_price or 0.0) * 0.25, 10.0)
    close = _aware(lot.close_at)
    if close is None:
        return stake
    minutes = (close - now).total_seconds() / 60.0
    if minutes <= 0:
        return 0.0
    if minutes <= 60:
        return stake * 4.0
    if minutes <= 360:
        return stake * 2.0
    return stake


def has_fresh_prices(session: Session, query_key: str, *, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    cutoff = _now() - timedelta(days=settings.market_price_ttl_days)
    family = key_family(query_key)
    row = session.scalar(
        select(MarketPrice.id)
        .where(MarketPrice.query_key.like(f"{family}%"), MarketPrice.observed_at >= cutoff)
        .limit(1)
    )
    return row is not None


def enqueue_for_lot(
    session: Session,
    lot: Lot,
    *,
    sites: list[LookupSite] | None = None,
    force: bool = False,
    settings: Settings | None = None,
) -> list[LookupJob]:
    """Queue price lookups for one lot. Idempotent.

    Skips sites that already have a job in flight, and skips the lot entirely
    when recent prices are already on file — re-checking a price we looked up
    yesterday spends the day's search budget on nothing.
    """
    settings = settings or get_settings()
    if not settings.lookup_enabled:
        return []

    sites = sites or [LookupSite(s) for s in settings.enabled_lookup_sites if _valid(s)]
    key = normalize_item_key(lot.title, brand=lot.brand, model=lot.model, upc=lot.upc)

    if not force and has_fresh_prices(session, key, settings=settings):
        return []

    query = search_query(lot.title, brand=lot.brand, model=lot.model)
    if not query.strip():
        return []

    in_flight = {
        job.site
        for job in session.scalars(
            select(LookupJob).where(
                LookupJob.query_key == key,
                LookupJob.status.in_((LookupStatus.PENDING, LookupStatus.LEASED)),
            )
        )
    }

    created: list[LookupJob] = []
    priority = priority_for_lot(lot)
    for site in sites:
        if site in in_flight:
            continue
        job = LookupJob(
            lot_id=lot.id,
            query_key=key,
            query=query,
            site=site,
            status=LookupStatus.PENDING,
            priority=priority,
        )
        session.add(job)
        created.append(job)

    session.flush()
    return created


def _valid(value: str) -> bool:
    try:
        LookupSite(value)
    except ValueError:
        log.warning("unknown lookup site %r in LOOKUP_SITES, ignoring", value)
        return False
    return True


# --------------------------------------------------------------------------
# Pacing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Pacing:
    """Why the browser may or may not search right now."""

    allowed: bool
    reason: str
    retry_after_seconds: float = 0.0


def _last_activity(session: Session, *, site: LookupSite | None = None) -> datetime | None:
    stmt = select(LookupJob.leased_at).where(LookupJob.leased_at.is_not(None))
    if site is not None:
        stmt = stmt.where(LookupJob.site == site)
    stmt = stmt.order_by(LookupJob.leased_at.desc()).limit(1)
    return _aware(session.scalar(stmt))


def _blocked_until(
    session: Session, site: LookupSite, settings: Settings, now: datetime
) -> datetime | None:
    last = session.scalar(
        select(LookupJob.finished_at)
        .where(LookupJob.site == site, LookupJob.status == LookupStatus.BLOCKED)
        .order_by(LookupJob.finished_at.desc())
        .limit(1)
    )
    last = _aware(last)
    if last is None:
        return None
    until = last + timedelta(minutes=settings.lookup_block_cooldown_minutes)
    return until if until > now else None


def check_pacing(
    session: Session,
    site: LookupSite | None = None,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> Pacing:
    settings = settings or get_settings()
    now = now or _now()

    if not settings.lookup_enabled:
        return Pacing(False, "lookups are switched off (LOOKUP_ENABLED=false)")

    day_ago = now - timedelta(days=1)
    used = session.scalar(
        select(LookupJob.id).where(LookupJob.leased_at >= day_ago).limit(1)
    )
    if used is not None:
        count = len(
            session.scalars(select(LookupJob.id).where(LookupJob.leased_at >= day_ago)).all()
        )
        if count >= settings.lookup_max_per_day:
            return Pacing(
                False,
                f"daily search ceiling reached ({count}/{settings.lookup_max_per_day})",
                retry_after_seconds=3600.0,
            )

    last = _last_activity(session)
    if last is not None:
        gap = (now - last).total_seconds()
        if gap < settings.lookup_min_seconds_between:
            wait = settings.lookup_min_seconds_between - gap
            return Pacing(False, "too soon since the last search", retry_after_seconds=wait)

    if site is not None:
        blocked = _blocked_until(session, site, settings, now)
        if blocked is not None:
            wait = (blocked - now).total_seconds()
            return Pacing(
                False,
                f"{site.value} asked us to stop; backing off until {blocked:%H:%M}",
                retry_after_seconds=wait,
            )

        site_last = _last_activity(session, site=site)
        if site_last is not None:
            gap = (now - site_last).total_seconds()
            if gap < settings.lookup_site_min_seconds:
                wait = settings.lookup_site_min_seconds - gap
                return Pacing(
                    False, f"too soon since the last {site.value} search", retry_after_seconds=wait
                )

    return Pacing(True, "ok")


# --------------------------------------------------------------------------
# Leasing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LeasedJob:
    job_id: int
    site: str
    query: str
    query_key: str
    url: str
    lot_id: str | None
    lot_title: str | None


def reclaim_stale(
    session: Session, *, now: datetime | None = None, settings: Settings | None = None
) -> int:
    """Put back jobs the browser took and never reported on.

    A closed tab, a reloaded extension, a laptop lid — all of them leave a job
    leased forever otherwise.
    """
    settings = settings or get_settings()
    now = now or _now()
    cutoff = now - timedelta(seconds=settings.lookup_lease_timeout_seconds)

    stale = session.scalars(
        select(LookupJob).where(
            LookupJob.status == LookupStatus.LEASED, LookupJob.leased_at < cutoff
        )
    ).all()

    for job in stale:
        if job.attempts >= settings.lookup_max_attempts:
            job.status = LookupStatus.FAILED
            job.note = "browser never reported back"
            job.finished_at = now
        else:
            job.status = LookupStatus.PENDING
            job.leased_at = None
    session.flush()
    return len(stale)


def lease_next(
    session: Session,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> tuple[LeasedJob | None, Pacing]:
    """Hand the browser at most one job. Never more.

    Returns the job and the pacing verdict, so the extension can be told *why*
    it got nothing and how long to wait — a silent empty response would just
    make it poll harder.
    """
    settings = settings or get_settings()
    now = now or _now()

    reclaim_stale(session, now=now, settings=settings)

    pacing = check_pacing(session, None, now=now, settings=settings)
    if not pacing.allowed:
        return None, pacing

    candidates = session.scalars(
        select(LookupJob)
        .where(LookupJob.status == LookupStatus.PENDING)
        .order_by(LookupJob.priority.desc(), LookupJob.created_at)
        .limit(25)
    ).all()

    if not candidates:
        return None, Pacing(True, "nothing queued", retry_after_seconds=60.0)

    last_reason = "every queued site is in its cooldown window"
    longest_wait = 60.0
    for job in candidates:
        site_pacing = check_pacing(session, job.site, now=now, settings=settings)
        if not site_pacing.allowed:
            last_reason = site_pacing.reason
            longest_wait = min(longest_wait, max(30.0, site_pacing.retry_after_seconds))
            continue

        job.status = LookupStatus.LEASED
        job.leased_at = now
        job.attempts += 1
        session.flush()

        lot = session.get(Lot, job.lot_id) if job.lot_id else None
        return (
            LeasedJob(
                job_id=job.id,
                site=job.site.value,
                query=job.query,
                query_key=job.query_key,
                url=search_url(job.site, job.query, settings),
                lot_id=lot.nellis_id if lot else None,
                lot_title=lot.title if lot else None,
            ),
            Pacing(True, "leased"),
        )

    return None, Pacing(False, last_reason, retry_after_seconds=longest_wait)


# --------------------------------------------------------------------------
# Reporting back
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LookupResult:
    job_id: int
    status: str
    recorded: int
    rejected: list[str]


def complete(
    session: Session,
    job: LookupJob,
    candidates: list[dict],
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> LookupResult:
    """Record what the browser saw."""
    settings = settings or get_settings()
    now = now or _now()

    lot = session.get(Lot, job.lot_id) if job.lot_id else None
    lot_title = lot.title if lot else job.query

    kept, rejects = classify_all(
        candidates,
        lot_title=lot_title,
        brand=lot.brand if lot else None,
        model=lot.model if lot else None,
        limit=settings.lookup_max_results_per_search,
    )
    for record in kept:
        record.setdefault("source", job.site.value)
        record["source"] = job.site.value

    added = record_prices(session, job.query_key, kept) if kept else 0

    job.recorded = added
    job.finished_at = now
    job.status = LookupStatus.DONE if added else LookupStatus.EMPTY
    if not added:
        job.note = (
            f"{len(candidates)} results, none usable: {rejects[0]}"
            if rejects
            else "search returned no results"
        )
    session.flush()

    return LookupResult(job.id, job.status.value, added, rejects[:10])


def mark_blocked(
    session: Session, job: LookupJob, reason: str, *, now: datetime | None = None
) -> LookupJob:
    """A site said stop. Stop — for that whole site, not just this job.

    Deliberately not a retry path. The cooldown is enforced in `check_pacing`
    against the most recent BLOCKED job for the site, so this one record quiets
    every future lookup there.
    """
    job.status = LookupStatus.BLOCKED
    job.note = reason[:512]
    job.finished_at = now or _now()
    session.flush()
    log.warning("%s blocked us (%s) — backing off that site", job.site.value, reason)
    return job


def mark_failed(
    session: Session,
    job: LookupJob,
    reason: str,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> LookupJob:
    settings = settings or get_settings()
    now = now or _now()
    if job.attempts < settings.lookup_max_attempts:
        job.status = LookupStatus.PENDING
        job.leased_at = None
    else:
        job.status = LookupStatus.FAILED
        job.finished_at = now
    job.note = reason[:512]
    session.flush()
    return job


# --------------------------------------------------------------------------
# Status, for the dashboard
# --------------------------------------------------------------------------


def queue_status(
    session: Session, *, now: datetime | None = None, settings: Settings | None = None
) -> dict:
    settings = settings or get_settings()
    now = now or _now()

    counts: dict[str, int] = {}
    for status in LookupStatus:
        rows = session.scalars(
            select(LookupJob.id).where(LookupJob.status == status)
        ).all()
        counts[status.value] = len(rows)

    day_ago = now - timedelta(days=1)
    today = len(
        session.scalars(select(LookupJob.id).where(LookupJob.leased_at >= day_ago)).all()
    )

    sites = []
    for site in LookupSite:
        blocked = _blocked_until(session, site, settings, now)
        last = _last_activity(session, site=site)
        sites.append(
            {
                "site": site.value,
                "enabled": site.value in settings.enabled_lookup_sites,
                "last_search": last.isoformat() if last else None,
                "blocked_until": blocked.isoformat() if blocked else None,
            }
        )

    return {
        "enabled": settings.lookup_enabled,
        "counts": counts,
        "searches_last_24h": today,
        "daily_limit": settings.lookup_max_per_day,
        "sites": sites,
    }
