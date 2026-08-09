"""Command-line interface.

Start here:
    nellis init            create the database
    nellis doctor          verify we can actually read Nellis (run this first!)
    nellis watch add ...   define what you're hunting
    nellis scan            sweep, value, queue
    nellis serve           dashboard + scheduler
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import func, select

from .config import get_settings
from .db import init_db, session_scope
from .models import Comp, Lot, Valuation, Watch

app = typer.Typer(add_completion=False, help="Nellis Auction deal engine")
watch_app = typer.Typer(help="Manage saved searches")
comps_app = typer.Typer(help="Manage the comps database")
app.add_typer(watch_app, name="watch")
app.add_typer(comps_app, name="comps")

console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    _setup_logging(verbose)


# --------------------------------------------------------------------------


@app.command()
def init() -> None:
    """Create the database schema."""
    init_db()
    settings = get_settings()
    console.print(f"[green]Database ready[/] at {settings.database_url}")
    if not settings.email_configured:
        console.print("[yellow]SMTP not configured[/] — email alerts are disabled. See .env.example")


@app.command()
def doctor(lot_id: str = typer.Option(None, help="A known-good lot ID to test against")) -> None:
    """Check we can actually read Nellis, and which strategy works.

    RUN THIS FIRST on your own machine. The ingestion adapters were built
    against recorded fixtures; this is what confirms them against the live site
    and tells you exactly which fields resolve.
    """
    from .ingest import BlockedError, PoliteClient, SearchFilters, default_chain

    async def _run() -> None:
        settings = get_settings()
        chain = default_chain()
        table = Table("strategy", "search", "detail", "notes", title="Adapter health")
        report: dict = {"base_url": settings.nellis_base_url, "strategies": []}

        async with PoliteClient(settings) as client:
            for adapter in chain.adapters:
                search_result, detail_result, note = "—", "—", ""
                row: dict = {"strategy": adapter.name, "lots": 0, "detail_ok": False}
                try:
                    records = await adapter.search(client, SearchFilters(query="tool"))
                    row["lots"] = len(records)
                    search_result = f"[green]{len(records)} lots[/]" if records else "[red]0[/]"
                    if records:
                        sample = records[0]
                        missing = sample.missing_critical_fields()
                        row["missing_fields"] = missing
                        row["sample_id"] = sample.nellis_id
                        note = (
                            f"[yellow]missing: {', '.join(missing)}[/]"
                            if missing
                            else "[green]all critical fields present[/]"
                        )
                        target = lot_id or sample.nellis_id
                        record = await adapter.fetch_lot(client, target)
                        detail_result = "[green]ok[/]" if record else "[red]none[/]"
                        row["detail_ok"] = record is not None
                except BlockedError as exc:
                    console.print(f"[red]BLOCKED:[/] {exc}")
                    row["error"] = str(exc)
                    report["strategies"].append(row)
                    break
                except Exception as exc:
                    search_result = "[red]error[/]"
                    note = str(exc)[:70]
                    row["error"] = str(exc)[:300]
                report["strategies"].append(row)
                table.add_row(adapter.name, search_result, detail_result, note)

        console.print(table)
        console.print(f"requests used: {client.requests_made}")

        # Also write it to disk — easier to hand over than copying a terminal table.
        import json as json_module

        out = Path("fixtures/live")
        out.mkdir(parents=True, exist_ok=True)
        report_path = out / "doctor.json"
        report_path.write_text(json_module.dumps(report, indent=2))
        console.print(f"[dim]report written to {report_path}[/]")
        console.print(
            Panel(
                "If every strategy shows 0 lots, the site structure changed.\n"
                "Fix order: add new field names to ALIASES in ingest/adapter.py,\n"
                "then re-record fixtures in tests/fixtures/ and re-run pytest.",
                title="If this failed",
                border_style="yellow",
            )
        )

    asyncio.run(_run())


@app.command()
def demo(
    reset: bool = typer.Option(False, "--reset", help="Wipe existing data first"),
) -> None:
    """Seed a realistic dataset so you can see the whole system work offline.

    No credentials, no network, no scraping. Lots and sales history are seeded,
    then run through the REAL valuation engine — so every number shown is
    genuinely computed, not pre-baked.
    """
    from .demo import seed

    async def _run() -> None:
        init_db()
        settings = get_settings()
        with session_scope() as session:
            stats = await seed(session, settings, reset=reset)

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_row("Closed lots (became comps)", str(stats["closed"]))
        table.add_row("Comps in database", str(stats["comps"]))
        table.add_row("Open lots valued", str(stats["open"]))
        table.add_row("[green]Recommended[/]", f"[green]{stats['recommended']}[/]")
        table.add_row("Rejected (guardrails)", str(stats["rejected"]))
        table.add_row("Repair plays flagged", str(stats["repair_flagged"]))
        table.add_row("Queued for bidding", str(stats["queued"]))
        console.print(table)

        console.print(
            Panel(
                "  nellis queue           what to go bid on\n"
                "  nellis digest          render the alert email\n"
                f"  nellis serve           dashboard at http://{settings.web_host}:{settings.web_port}",
                title="Now try",
                border_style="green",
            )
        )
        console.print(
            "[dim]Demo data only. Run `nellis demo --reset` to rebuild, "
            "or delete data/nellis.db before going live.[/]"
        )

    asyncio.run(_run())


@app.command()
def record(
    lot_id: str = typer.Argument(None, help="Specific lot to capture; otherwise auto-picked"),
    query: str = typer.Option("tool", help="Search term to capture results for"),
) -> None:
    """Capture live page data so the parsers can be calibrated against reality.

    Produces fixtures/live/capture.zip — public page HTML plus the JSON payloads
    found inside it, with any credential-shaped fields redacted. Send that file
    over and the adapters can be tuned to the real site.
    """
    from .ingest import BlockedError, PoliteClient
    from .ingest.record import capture

    async def _run() -> None:
        settings = get_settings()
        async with PoliteClient(settings) as client:
            try:
                archive, report = await capture(client, lot_id, query=query)
            except BlockedError as exc:
                console.print(f"[red]Blocked — stopping.[/] {exc}")
                raise typer.Exit(1) from exc

        table = Table("strategy", "lots", "resolved", "missing")
        for probe in report.probes:
            table.add_row(
                probe.strategy,
                f"[green]{probe.lots_found}[/]" if probe.lots_found else "[red]0[/]",
                str(len(probe.resolved_fields)),
                ", ".join(probe.missing_fields) or "[green]none[/]",
            )
        console.print(table)
        for note in report.notes:
            console.print(f"[yellow]! {note}[/]")

        size_kb = archive.stat().st_size / 1024
        console.print(
            Panel(
                f"[bold]{archive}[/]  ({size_kb:,.0f} KB)\n\n"
                "Public page data only; credential-shaped fields are redacted.\n"
                "Send this file over to have the parsers calibrated.",
                title="Capture written",
                border_style="green",
            )
        )

    asyncio.run(_run())


@app.command()
def scan(
    watch_name: str = typer.Option(None, "--watch", "-w", help="Run only this watch"),
    pages: int = typer.Option(2, help="Pages per search term"),
    no_value: bool = typer.Option(False, "--no-value", help="Ingest only, skip valuation"),
) -> None:
    """Sweep watches: find lots, value them, queue the good ones."""
    from .ingest import BlockedError, PoliteClient, default_chain
    from .search.watch import active_watches, run_watch
    from .valuation.engine import ValuationEngine

    async def _run() -> None:
        settings = get_settings()
        init_db()
        with session_scope() as session:
            watches = active_watches(session)
            if watch_name:
                watches = [w for w in watches if w.name == watch_name]
            if not watches:
                console.print("[yellow]No enabled watches.[/] Add one: nellis watch add --help")
                return

            chain = default_chain()
            engine = ValuationEngine(session, settings)
            async with PoliteClient(settings) as client:
                for watch in watches:
                    console.print(f"[bold]Running watch:[/] {watch.name}")
                    try:
                        report = await run_watch(
                            session, watch, client=client, chain=chain,
                            engine=engine, settings=settings,
                            max_pages=pages, value_matches=not no_value,
                        )
                    except BlockedError as exc:
                        console.print(f"[red]Blocked — stopping.[/] {exc}")
                        return
                    console.print(f"  {report.summary()}")
                    for error in report.errors[:5]:
                        console.print(f"  [yellow]! {error}[/]")

                    for result in sorted(
                        [r for r in report.results if r.recommended],
                        key=lambda r: r.projected_profit, reverse=True,
                    )[:10]:
                        console.print(
                            f"  [green]${result.walk_away_max_bid:>7.2f}[/] max  "
                            f"(+${result.projected_profit:.2f})  {result.lot.title[:58]}"
                        )
            console.print(f"[dim]requests used: {client.requests_made}[/]")

    asyncio.run(_run())


@app.command()
def value(nellis_id: str, refresh: bool = typer.Option(False, help="Re-fetch the lot first")) -> None:
    """Value one lot and print the full breakdown."""
    from .ingest import PoliteClient, default_chain
    from .ingest.harvest import upsert_lot
    from .valuation.engine import ValuationEngine

    async def _run() -> None:
        settings = get_settings()
        init_db()
        with session_scope() as session:
            lot = session.scalar(select(Lot).where(Lot.nellis_id == nellis_id))
            if lot is None or refresh:
                async with PoliteClient(settings) as client:
                    record = await default_chain().fetch_lot(client, nellis_id)
                if record is None:
                    console.print(f"[red]Could not fetch lot {nellis_id}[/]")
                    raise typer.Exit(1)
                lot, _ = upsert_lot(session, record)

            engine = ValuationEngine(session, settings)
            result = await engine.value(lot)
            _print_valuation(result)

    asyncio.run(_run())


def _print_valuation(result) -> None:
    lot = result.lot
    console.print()
    console.print(Panel(f"[bold]{lot.title}[/]\n{lot.url}", border_style="blue"))

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("Retail", f"${lot.retail_price:,.2f}" if lot.retail_price else "—")
    table.add_row("Current bid", f"${lot.current_bid:,.2f}")
    table.add_row(
        "Comps",
        f"${result.comp_set.value:,.2f} (n={result.comp_set.count}, "
        f"{', '.join(result.comp_set.sources) or 'none'})"
        if result.comp_set.value
        else "none found",
    )
    table.add_row("Confidence", f"{result.confidence.level.value} — {'; '.join(result.confidence.reasons)}")
    table.add_row("Condition", result.condition.summary)
    if result.repair:
        table.add_row(
            "Repair",
            f"parts ${result.repair.parts_cost:.2f}, "
            f"{result.repair.labor_hours:.1f}h — {result.repair.notes}",
        )
    table.add_row("Resale", f"{result.channel.label} → net ${result.net_resale:,.2f}")
    table.add_row("Margin required", f"{result.effective_margin:.0%}")
    console.print(table)

    color = "green" if result.recommended else "red"
    console.print(
        Panel(
            f"[bold {color}]MAX BID  ${result.walk_away_max_bid:,.2f}[/]\n"
            f"landed ${result.landed_at_max:,.2f} · "
            f"projected profit ${result.projected_profit:,.2f} "
            f"({result.projected_margin:.0%})\n\n{result.reason}",
            title="Verdict",
            border_style=color,
        )
    )
    for warning in result.warnings:
        console.print(f"[yellow]! {warning}[/]")
    console.print("[dim]Enter this max bid on Nellis yourself — this tool never bids.[/]")


# --------------------------------------------------------------------------


@watch_app.command("add")
def watch_add(
    name: str,
    keywords: str = typer.Option("", help="Comma-separated, matches ANY"),
    require_all: str = typer.Option("", help="Comma-separated, must match ALL"),
    exclude: str = typer.Option("", help="Comma-separated exclusions"),
    category: str = typer.Option(""),
    brands: str = typer.Option(""),
    condition: str = typer.Option(""),
    location: str = typer.Option(""),
    min_retail: float = typer.Option(None),
    max_retail: float = typer.Option(None),
    max_bid: float = typer.Option(None, help="Skip lots already bid above this"),
    min_discount: float = typer.Option(None, help="e.g. 60 for 60% off retail"),
    closes_within: int = typer.Option(None, help="Minutes"),
    margin: float = typer.Option(None, help="Target margin %, e.g. 40"),
    channel: str = typer.Option("ebay", help="ebay|local|fb_shipped|mercari|offerup"),
    max_repair: float = typer.Option(None, help="Max acceptable parts cost"),
    no_damaged: bool = typer.Option(False, help="Exclude damaged/incomplete lots"),
) -> None:
    """Create a watch."""
    init_db()
    with session_scope() as session:
        if session.scalar(select(Watch).where(Watch.name == name)):
            console.print(f"[red]A watch named '{name}' already exists.[/]")
            raise typer.Exit(1)
        session.add(
            Watch(
                name=name,
                keywords=keywords or None,
                require_all=require_all or None,
                exclude_terms=exclude or None,
                categories=category or None,
                brands=brands or None,
                conditions=condition or None,
                locations=location or None,
                min_retail=min_retail,
                max_retail=max_retail,
                max_current_bid=max_bid,
                min_discount_pct=(min_discount / 100) if min_discount else None,
                closes_within_minutes=closes_within,
                target_margin=(margin / 100) if margin else None,
                resale_channel=channel,
                max_repair_cost=max_repair,
                include_damaged=not no_damaged,
                enabled=True,
            )
        )
    console.print(f"[green]Created watch[/] '{name}'")


@watch_app.command("list")
def watch_list() -> None:
    """List watches."""
    init_db()
    with session_scope() as session:
        watches = session.scalars(select(Watch)).all()
        if not watches:
            console.print("[yellow]No watches yet.[/]")
            return
        table = Table("name", "on", "keywords", "filters", "last run")
        for watch in watches:
            filters = []
            if watch.max_current_bid:
                filters.append(f"bid≤${watch.max_current_bid:.0f}")
            if watch.min_discount_pct:
                filters.append(f"disc≥{watch.min_discount_pct:.0%}")
            if watch.max_retail:
                filters.append(f"retail≤${watch.max_retail:.0f}")
            if not watch.include_damaged:
                filters.append("no-damaged")
            table.add_row(
                watch.name,
                "[green]yes[/]" if watch.enabled else "[dim]no[/]",
                (watch.keywords or watch.require_all or "—")[:34],
                ", ".join(filters) or "—",
                watch.last_run_at.strftime("%m-%d %H:%M") if watch.last_run_at else "never",
            )
        console.print(table)


@watch_app.command("remove")
def watch_remove(name: str) -> None:
    """Delete a watch."""
    with session_scope() as session:
        watch = session.scalar(select(Watch).where(Watch.name == name))
        if watch is None:
            console.print(f"[red]No watch named '{name}'[/]")
            raise typer.Exit(1)
        session.delete(watch)
    console.print(f"[green]Deleted[/] '{name}'")


# --------------------------------------------------------------------------


@comps_app.command("stats")
def comps_stats() -> None:
    """Show how much pricing data you've accumulated."""
    init_db()
    with session_scope() as session:
        table = Table("source", "comps", "distinct items")
        rows = session.execute(
            select(Comp.source, func.count(Comp.id), func.count(func.distinct(Comp.query_key)))
            .group_by(Comp.source)
        ).all()
        for source, count, distinct in rows:
            table.add_row(source, str(count), str(distinct))
        console.print(table)
        if not rows:
            console.print(
                "[yellow]Empty.[/] Nellis comps build automatically as tracked lots close — "
                "leave `nellis serve` running."
            )


@comps_app.command("import")
def comps_import(path: Path) -> None:
    """Import comps from a CSV (columns: title,price,shipping,condition,sold_at,url)."""
    import csv as csv_module

    from .normalize import normalize_item_key

    init_db()
    added = 0
    with session_scope() as session, path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv_module.DictReader(handle):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            title, raw_price = row.get("title"), row.get("price")
            if not title or not raw_price:
                continue
            try:
                price = float(raw_price.replace("$", "").replace(",", ""))
            except ValueError:
                continue
            session.add(
                Comp(
                    source="csv",
                    query_key=normalize_item_key(title),
                    title=title,
                    price=price,
                    shipping=float(row.get("shipping") or 0) or 0.0,
                    condition=row.get("condition") or None,
                    is_sold=True,
                    url=row.get("url") or None,
                )
            )
            added += 1
    console.print(f"[green]Imported {added} comps[/] from {path}")


# --------------------------------------------------------------------------


@app.command()
def digest(dry_run: bool = typer.Option(True, help="Render without sending")) -> None:
    """Build the deal digest email."""
    from .scheduler.jobs import send_daily_digest

    async def _run() -> None:
        init_db()
        result = await send_daily_digest(dry_run=dry_run)
        console.print(f"[bold]{result.detail}[/]")
        if dry_run:
            from .notify.email import EmailNotifier
            from .valuation.engine import ValuationEngine  # noqa: F401

            settings = get_settings()
            with session_scope() as session:
                recent = session.scalars(
                    select(Valuation)
                    .where(Valuation.recommended.is_(True))
                    .order_by(Valuation.projected_profit.desc())
                    .limit(20)
                ).all()
                if not recent:
                    console.print("[yellow]Nothing recommended yet — run `nellis scan` first.[/]")
                    return
                engine = ValuationEngine(session, settings, offline=True)
                results = []
                seen = set()
                for record in recent:
                    if record.lot_id in seen:
                        continue
                    seen.add(record.lot_id)
                    lot = session.get(Lot, record.lot_id)
                    if lot is not None:
                        results.append(await engine.value(lot))
                email = EmailNotifier(settings).render_digest([r for r in results if r.recommended])
                path = email.preview_path(Path("data/previews"))
                console.print(f"[green]Preview written[/] → {path}")

    asyncio.run(_run())


@app.command()
def serve(
    scheduler: bool = typer.Option(True, help="Also run background jobs"),
    host: str = typer.Option(None),
    port: int = typer.Option(None),
) -> None:
    """Run the dashboard (and by default the scheduler)."""
    import uvicorn

    settings = get_settings()
    init_db()

    if scheduler:
        from .web.app import app as web_app

        @web_app.on_event("startup")
        async def _start_jobs() -> None:
            from .scheduler import build_scheduler

            build_scheduler(settings).start()
            log_msg = "background jobs started"
            console.print(f"[green]{log_msg}[/]")

    console.print(
        f"[bold]Dashboard:[/] http://{host or settings.web_host}:{port or settings.web_port}"
    )
    uvicorn.run(
        "nellis.web.app:app",
        host=host or settings.web_host,
        port=port or settings.web_port,
        log_level="info",
    )


@app.command()
def refresh() -> None:
    """Re-poll tracked open lots and harvest any that closed."""
    from .scheduler.jobs import refresh_tracked

    async def _run() -> None:
        init_db()
        result = await refresh_tracked()
        console.print(f"[bold]{result.detail}[/]")

    asyncio.run(_run())


@app.command()
def queue(limit: int = typer.Option(20)) -> None:
    """Show the pending bid queue — what to go enter on Nellis."""
    from .models import BidQueueEntry, QueueStatus
    from .valuation.exposure import current_exposure

    init_db()
    with session_scope() as session:
        entries = session.scalars(
            select(BidQueueEntry)
            .where(BidQueueEntry.status == QueueStatus.PENDING)
            .order_by(BidQueueEntry.rank_score.desc())
            .limit(limit)
        ).all()
        if not entries:
            console.print("[yellow]Queue is empty.[/] Run `nellis scan`.")
            return

        table = Table("max bid", "profit", "lot", "closes", title="Enter these on Nellis yourself")
        for entry in entries:
            lot = session.get(Lot, entry.lot_id)
            if lot is None:
                continue
            from .notify.email import humanize_delta

            table.add_row(
                f"[green]${entry.suggested_max_bid:,.2f}[/]",
                f"${entry.projected_profit or 0:,.2f}",
                lot.title[:56],
                humanize_delta(lot.close_at) or "—",
            )
        console.print(table)

        state = current_exposure(session)
        settings = get_settings()
        console.print(
            f"\nOpen exposure: [bold]${state.total:,.2f}[/] / ${settings.max_open_exposure:,.2f} "
            f"across {state.lot_count} live bid(s)"
        )


if __name__ == "__main__":
    app()
