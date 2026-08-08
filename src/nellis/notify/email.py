"""Email notifications.

Renders Jinja2 templates and sends them over SMTP. With SMTP unconfigured the
notifier degrades to "render but don't send", which is also what `--dry-run`
uses — you can develop and preview every email without a mail server.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..valuation.engine import ValuationResult
from . import dedupe

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"


@dataclass
class RenderedEmail:
    subject: str
    html: str
    text: str

    def preview_path(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", self.subject.lower())[:60].strip("-")
        path = directory / f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{slug}.html"
        path.write_text(self.html)
        return path


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def humanize_delta(target: datetime | None, now: datetime | None = None) -> str | None:
    if target is None:
        return None
    now = now or datetime.now(UTC)
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    seconds = (target - now).total_seconds()
    if seconds < 0:
        return "closed"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"
    return f"{int(seconds // 86400)}d {int((seconds % 86400) // 3600)}h"


def _to_item(result: ValuationResult) -> dict:
    payload = result.as_dict()
    payload["closes_in"] = humanize_delta(result.lot.close_at)
    return payload


def _plain_text(items: list[dict]) -> str:
    lines = []
    for item in items:
        lot, verdict = item["lot"], item["verdict"]
        lines.append(f"{lot['title']}")
        lines.append(f"  Current ${lot['current_bid']:.2f} | MAX BID ${verdict['walk_away_max_bid']:.2f}")
        lines.append(
            f"  Profit ${verdict['projected_profit']:.2f} "
            f"({verdict['projected_margin']*100:.0f}%) | confidence {item['confidence']['level']}"
        )
        lines.append(f"  {lot['url']}")
        lines.append("")
    lines.append("This tool does not place bids. Enter the max bid on Nellis yourself.")
    return "\n".join(lines)


class EmailNotifier:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.env = _environment()

    # -- rendering -------------------------------------------------------

    def render_digest(
        self, results: list[ValuationResult], *, exposure: dict | None = None
    ) -> RenderedEmail:
        items = [_to_item(r) for r in results]
        total_profit = sum(i["verdict"]["projected_profit"] for i in items)
        subject = (
            f"{len(items)} Nellis deal{'s' if len(items) != 1 else ''} "
            f"· ${total_profit:,.0f} projected profit"
            if items
            else "Nellis digest · no qualifying deals"
        )
        headroom = 0.0
        if exposure:
            headroom = max(0.0, self.settings.max_open_exposure - exposure["total_exposure"])

        html = self.env.get_template("digest.html").render(
            subject=subject,
            heading="Deals clearing your margin",
            subheading=datetime.now(UTC).strftime("%A, %B %-d · %H:%M UTC"),
            items=items,
            exposure=exposure,
            headroom=headroom,
        )
        return RenderedEmail(subject=subject, html=html, text=_plain_text(items))

    def render_closing_soon(self, results: list[ValuationResult]) -> RenderedEmail:
        items = [_to_item(r) for r in results]
        subject = f"Closing soon · {len(items)} lot{'s' if len(items) != 1 else ''} under your max"
        html = self.env.get_template("closing_soon.html").render(
            subject=subject,
            heading="Closing soon",
            subheading="Still below your walk-away price",
            items=items,
        )
        return RenderedEmail(subject=subject, html=html, text=_plain_text(items))

    # -- sending ---------------------------------------------------------

    async def send(self, email: RenderedEmail) -> bool:
        settings = self.settings
        if not settings.email_configured:
            log.warning("SMTP not configured — skipping send of %r", email.subject)
            return False

        message = EmailMessage()
        message["From"] = settings.email_from
        message["To"] = ", ".join(settings.email_recipients)
        message["Subject"] = email.subject
        message.set_content(email.text)
        message.add_alternative(email.html, subtype="html")

        try:
            await aiosmtplib.send(
                message,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username or None,
                password=settings.smtp_password or None,
                start_tls=settings.smtp_use_tls,
            )
        except (aiosmtplib.SMTPException, OSError) as exc:
            log.error("email send failed: %s", exc)
            return False

        log.info("sent %r to %s", email.subject, settings.email_recipients)
        return True


async def send_digest(
    session: Session,
    results: list[ValuationResult],
    *,
    settings: Settings | None = None,
    dry_run: bool = False,
    exposure: dict | None = None,
) -> RenderedEmail | None:
    """Send the deal digest, filtering anything already alerted on."""
    settings = settings or get_settings()
    notifier = EmailNotifier(settings)

    fresh: list[ValuationResult] = []
    keys: list[tuple[str, ValuationResult]] = []
    for result in results:
        if result.projected_profit < settings.notify_min_profit:
            continue
        key = dedupe.deal_key(result.lot.nellis_id, result.walk_away_max_bid)
        if dedupe.already_sent(session, key):
            continue
        fresh.append(result)
        keys.append((key, result))

    if not fresh:
        log.info("digest: nothing new to report")
        return None

    fresh.sort(key=lambda r: r.projected_profit, reverse=True)
    email = notifier.render_digest(fresh, exposure=exposure)

    if dry_run:
        return email

    if await notifier.send(email):
        for key, result in keys:
            dedupe.record_sent(
                session, "deal", key, lot_id=result.lot.id, subject=email.subject
            )
    return email


async def send_closing_soon(
    session: Session,
    results: list[ValuationResult],
    *,
    settings: Settings | None = None,
    dry_run: bool = False,
) -> RenderedEmail | None:
    settings = settings or get_settings()
    notifier = EmailNotifier(settings)

    fresh = []
    for result in results:
        key = dedupe.closing_key(result.lot.nellis_id)
        if dedupe.already_sent(session, key):
            continue
        fresh.append((key, result))

    if not fresh:
        return None

    email = notifier.render_closing_soon([r for _, r in fresh])
    if dry_run:
        return email

    if await notifier.send(email):
        for key, result in fresh:
            dedupe.record_sent(
                session, "closing", key, lot_id=result.lot.id, subject=email.subject
            )
    return email
