"""FastAPI dashboard. Server-rendered, HTMX for the few interactive bits."""

from __future__ import annotations

import logging
import statistics
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..api.queue import get_db
from ..api.queue import router as api_router
from ..config import get_settings
from ..db import init_db
from ..models import (
    BidQueueEntry,
    Comp,
    Lot,
    LotSnapshot,
    PortfolioItem,
    QueueStatus,
    Valuation,
    Watch,
    WatchMatch,
)
from ..notify.email import humanize_delta
from ..valuation.cost import cost_multiplier, landed_cost
from ..valuation.exposure import current_exposure
from ..valuation.resale import CHANNELS

log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app() -> FastAPI:
    app = FastAPI(title="Nellis Deal Engine", docs_url="/api/docs")
    app.include_router(api_router)

    @app.on_event("startup")
    def _startup() -> None:
        init_db()

    def base_context(request: Request, session: Session, page: str) -> dict:
        settings = get_settings()
        return {
            "request": request,
            "page": page,
            "exposure": current_exposure(session),
            "max_exposure": settings.max_open_exposure,
            "api_token": settings.api_token or "",
        }

    # ---- deal feed ------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def feed(
        request: Request,
        session: Session = Depends(get_db),
        min_profit: float = 0.0,
        confidence: str = "",
        sort: str = "efficiency",
        blocked: str = "",
    ):
        show_blocked = bool(blocked)
        statuses = [QueueStatus.PENDING] + ([QueueStatus.BLOCKED] if show_blocked else [])
        entries = session.scalars(
            select(BidQueueEntry).where(BidQueueEntry.status.in_(statuses))
        ).all()

        items = []
        now = datetime.now(UTC)
        for entry in entries:
            lot = session.get(Lot, entry.lot_id)
            if lot is None or lot.is_closed:
                continue
            valuation = _latest_valuation(session, lot.id)
            if valuation is None:
                continue
            if (valuation.projected_profit or 0) < min_profit:
                continue
            if confidence and valuation.confidence.value != confidence:
                continue
            items.append(
                {
                    "lot": lot,
                    "valuation": valuation,
                    "entry": entry,
                    "closes_in": humanize_delta(lot.close_at, now),
                }
            )

        if sort == "profit":
            items.sort(key=lambda i: i["valuation"].projected_profit or 0, reverse=True)
        elif sort == "closing":
            items.sort(key=lambda i: i["lot"].close_at or datetime.max.replace(tzinfo=UTC))
        else:
            items.sort(key=lambda i: i["entry"].rank_score, reverse=True)

        context = base_context(request, session, "feed")
        context.update(
            items=items, min_profit=min_profit, confidence=confidence,
            sort=sort, show_blocked=show_blocked,
        )
        return TEMPLATES.TemplateResponse(request, "index.html", context)

    # ---- lot detail -----------------------------------------------------

    @app.get("/lot/{nellis_id}", response_class=HTMLResponse)
    def lot_detail(request: Request, nellis_id: str, session: Session = Depends(get_db)):
        lot = session.scalar(select(Lot).where(Lot.nellis_id == nellis_id))
        if lot is None:
            return RedirectResponse("/", status_code=303)

        settings = get_settings()
        valuation = _latest_valuation(session, lot.id)
        bp_rate = lot.buyers_premium_rate or settings.default_buyers_premium
        cost = landed_cost(
            valuation.walk_away_max_bid if valuation else 0.0,
            bp_rate=bp_rate,
            tax_rate=settings.sales_tax_rate,
            pickup=settings.pickup_cost,
        )
        snapshots = session.scalars(
            select(LotSnapshot)
            .where(LotSnapshot.lot_id == lot.id)
            .order_by(LotSnapshot.observed_at.desc())
            .limit(25)
        ).all()

        context = base_context(request, session, "feed")
        context.update(
            lot=lot, v=valuation, cost=cost, bp_rate=bp_rate,
            tax_rate=settings.sales_tax_rate,
            multiplier=cost_multiplier(bp_rate, settings.sales_tax_rate),
            snapshots=snapshots,
            closes_in=humanize_delta(lot.close_at),
        )
        return TEMPLATES.TemplateResponse(request, "lot.html", context)

    # ---- watches --------------------------------------------------------

    @app.get("/watches", response_class=HTMLResponse)
    def list_watches(request: Request, session: Session = Depends(get_db)):
        watches = session.scalars(select(Watch).order_by(Watch.created_at.desc())).all()
        for watch in watches:
            watch.match_count = session.scalar(
                select(func.count(WatchMatch.id)).where(WatchMatch.watch_id == watch.id)
            )
        context = base_context(request, session, "watches")
        context.update(
            watches=watches, channels=CHANNELS,
            default_margin=get_settings().target_margin,
        )
        return TEMPLATES.TemplateResponse(request, "watches.html", context)

    @app.post("/watches")
    def create_watch(
        session: Session = Depends(get_db),
        name: str = Form(...),
        keywords: str = Form(""),
        require_all: str = Form(""),
        exclude_terms: str = Form(""),
        categories: str = Form(""),
        conditions: str = Form(""),
        brands: str = Form(""),
        locations: str = Form(""),
        min_retail: str = Form(""),
        max_retail: str = Form(""),
        max_current_bid: str = Form(""),
        min_discount_pct: str = Form(""),
        closes_within_minutes: str = Form(""),
        target_margin: str = Form(""),
        resale_channel: str = Form("ebay"),
        max_repair_cost: str = Form(""),
        include_damaged: str = Form(""),
    ):
        session.add(
            Watch(
                name=name.strip(),
                keywords=keywords.strip() or None,
                require_all=require_all.strip() or None,
                exclude_terms=exclude_terms.strip() or None,
                categories=categories.strip() or None,
                conditions=conditions.strip() or None,
                brands=brands.strip() or None,
                locations=locations.strip() or None,
                min_retail=_num(min_retail),
                max_retail=_num(max_retail),
                max_current_bid=_num(max_current_bid),
                min_discount_pct=_pct(min_discount_pct),
                closes_within_minutes=int(_num(closes_within_minutes) or 0) or None,
                target_margin=_pct(target_margin),
                resale_channel=resale_channel or None,
                max_repair_cost=_num(max_repair_cost),
                include_damaged=bool(include_damaged),
                enabled=True,
            )
        )
        return RedirectResponse("/watches", status_code=303)

    @app.post("/watches/{watch_id}/toggle")
    def toggle_watch(watch_id: int, session: Session = Depends(get_db)):
        watch = session.get(Watch, watch_id)
        if watch is not None:
            watch.enabled = not watch.enabled
        return RedirectResponse("/watches", status_code=303)

    @app.post("/watches/{watch_id}/delete")
    def delete_watch(watch_id: int, session: Session = Depends(get_db)):
        watch = session.get(Watch, watch_id)
        if watch is not None:
            session.delete(watch)
        return RedirectResponse("/watches", status_code=303)

    # ---- portfolio ------------------------------------------------------

    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio(request: Request, session: Session = Depends(get_db)):
        rows = session.scalars(select(PortfolioItem).order_by(PortfolioItem.created_at.desc())).all()
        items = []
        for row in rows:
            lot = session.get(Lot, row.lot_id)
            items.append(
                {
                    "nellis_id": lot.nellis_id if lot else "?",
                    "title": lot.title if lot else "(unknown lot)",
                    "hammer_price": row.hammer_price,
                    "landed_cost": row.landed_cost,
                    "repair_spend": row.repair_spend,
                    "sold_price": row.sold_price,
                    "realized_profit": row.realized_profit,
                }
            )

        sold = [r for r in rows if r.sold_price is not None]
        held = [r for r in rows if r.sold_price is None]
        revenue = sum(r.sold_price for r in sold)
        sold_cost = sum(r.landed_cost + r.repair_spend for r in sold)
        realized = sum(r.realized_profit or 0.0 for r in sold)
        projected_on_sold = sum(r.projected_profit or 0.0 for r in sold)

        stats = {
            "sold_count": len(sold),
            "held_count": len(held),
            "held_cost": sum(r.landed_cost for r in held),
            "revenue": revenue,
            "sold_cost": sold_cost,
            "realized_profit": realized,
            "realized_margin": (realized / revenue) if revenue else 0.0,
            "projected_on_sold": projected_on_sold,
            "forecast_delta": realized - projected_on_sold,
        }

        context = base_context(request, session, "portfolio")
        context.update(items=items, stats=stats)
        return TEMPLATES.TemplateResponse(request, "portfolio.html", context)

    @app.post("/portfolio/sell")
    def record_sale(
        session: Session = Depends(get_db),
        nellis_id: str = Form(...),
        sold_price: float = Form(...),
        sold_fees: float = Form(0.0),
        repair_spend: float = Form(0.0),
        sold_channel: str = Form(""),
    ):
        lot = session.scalar(select(Lot).where(Lot.nellis_id == nellis_id.strip()))
        if lot is not None:
            item = session.scalar(
                select(PortfolioItem).where(PortfolioItem.lot_id == lot.id)
            )
            if item is not None:
                item.sold_price = sold_price
                item.sold_fees = sold_fees
                item.repair_spend = repair_spend
                item.sold_channel = sold_channel or None
                item.sold_at = datetime.now(UTC)
        return RedirectResponse("/portfolio", status_code=303)

    # ---- analytics ------------------------------------------------------

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics(request: Request, session: Session = Depends(get_db)):
        stats = {
            "nellis_comps": session.scalar(
                select(func.count(Comp.id)).where(Comp.source == "nellis")
            ) or 0,
            "external_comps": session.scalar(
                select(func.count(Comp.id)).where(Comp.source != "nellis")
            ) or 0,
            "distinct_items": session.scalar(
                select(func.count(func.distinct(Comp.query_key)))
            ) or 0,
            "lots": session.scalar(select(func.count(Lot.id))) or 0,
            "open_lots": session.scalar(
                select(func.count(Lot.id)).where(Lot.is_closed.is_(False))
            ) or 0,
        }

        closed = session.scalars(
            select(Lot)
            .where(Lot.is_closed.is_(True), Lot.final_price.is_not(None))
            .order_by(Lot.closed_at.desc())
            .limit(400)
        ).all()

        grouped: dict[str, list[Lot]] = {}
        for lot in closed:
            grouped.setdefault(lot.category or "uncategorized", []).append(lot)

        by_category = []
        for category, lots in sorted(grouped.items(), key=lambda kv: -len(kv[1]))[:12]:
            prices = [lot.final_price for lot in lots]
            ratios = [
                lot.final_price / lot.retail_price
                for lot in lots
                if lot.retail_price and lot.retail_price > 0
            ]
            by_category.append(
                {
                    "category": category,
                    "count": len(lots),
                    "median_price": statistics.median(prices),
                    "pct_of_retail": statistics.median(ratios) if ratios else None,
                }
            )

        recent = [
            {
                "title": lot.title,
                "retail_price": lot.retail_price,
                "final_price": lot.final_price,
                "pct": (lot.final_price / lot.retail_price) if lot.retail_price else None,
                "closed_at": lot.closed_at,
            }
            for lot in closed[:30]
        ]

        context = base_context(request, session, "analytics")
        context.update(stats=stats, by_category=by_category, recent=recent)
        return TEMPLATES.TemplateResponse(request, "analytics.html", context)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "since": datetime.now(UTC).isoformat()}

    return app


def _latest_valuation(session: Session, lot_id: int) -> Valuation | None:
    return session.scalar(
        select(Valuation)
        .where(Valuation.lot_id == lot_id)
        .order_by(Valuation.computed_at.desc())
        .limit(1)
    )


def _num(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _pct(raw: str) -> float | None:
    """Accept 40 or 0.40 and normalize to a fraction."""
    value = _num(raw)
    if value is None:
        return None
    return value / 100.0 if value > 1 else value


app = create_app()
