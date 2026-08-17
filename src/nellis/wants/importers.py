"""Getting demand signals in.

None of the three sources Logan asked for has usable programmatic access:

  * **Nellis purchase/return/watchlist history** sits behind his login, and
    automated authentication is exactly what their ToS prohibits.
  * **Amazon cart and saved-for-later** have no public API and heavy anti-bot.
  * **Alexa/Echo lists** had an API until Amazon shut the List Management REST
    API down on 1 July 2024. There is no replacement.

So the primary path is the browser extension, which already runs in his own
logged-in browser: when he visits one of those pages, it reads what is on screen
and POSTs it to the local engine. No stored credentials, no automated login,
nothing that touches a ToS boundary — the data is his, already rendered, in a
session he opened himself.

Every route lands in the same `DemandSignal` shape, so scoring never has to know
where a signal came from. Paste and CSV importers exist for the same reason: any
of these can be filled in by hand when automation isn't available or wanted.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from dateutil import parser as date_parser
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DemandSignal, ReplenishmentClass, SignalSource, WantItem
from ..normalize import normalize_item_key
from .replenishment import infer_class

log = logging.getLogger(__name__)

PRICE_RE = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]{2})?)")
QTY_RE = re.compile(r"(?:qty|quantity|x)\s*[:.]?\s*(\d{1,3})\b", re.IGNORECASE)


@dataclass
class ImportResult:
    source: str
    added: int = 0
    skipped: int = 0
    wants_created: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []

    def summary(self) -> str:
        return (
            f"{self.source}: {self.added} new signal(s), {self.skipped} already known"
            + (f", {self.wants_created} want(s) created" if self.wants_created else "")
        )


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = PRICE_RE.search(str(value))
    if match:
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            return None
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def _to_date(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if value:
        try:
            parsed = date_parser.parse(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (ValueError, TypeError, OverflowError):
            pass
    return datetime.now(UTC)


def record_signal(
    session: Session,
    *,
    source: str,
    title: str,
    quantity: int = 1,
    price: float | None = None,
    condition: str | None = None,
    category: str | None = None,
    brand: str | None = None,
    occurred_at: datetime | None = None,
    external_id: str | None = None,
    raw: dict | None = None,
) -> DemandSignal | None:
    """Store one signal, skipping anything already imported.

    Dedupe is on (source, external_id). Without an external id we synthesise one
    from source + item key + date, so re-importing the same export doesn't
    double-count a purchase and wrongly deepen suppression.
    """
    title = (title or "").strip()
    if not title:
        return None

    key = normalize_item_key(title, brand=brand)
    occurred = occurred_at or datetime.now(UTC)
    ident = external_id or f"{key}:{occurred:%Y-%m-%d}:{int((price or 0) * 100)}"

    existing = session.scalar(
        select(DemandSignal).where(
            DemandSignal.source == source, DemandSignal.external_id == ident
        )
    )
    if existing is not None:
        return None

    signal = DemandSignal(
        source=source,
        query_key=key,
        title=title[:512],
        quantity=max(1, int(quantity or 1)),
        price_paid=price,
        condition=condition,
        category=category,
        brand=brand,
        occurred_at=occurred,
        external_id=ident,
        raw=raw,
    )
    session.add(signal)
    return signal


# --------------------------------------------------------------------------
# Structured import (what the browser extension posts)
# --------------------------------------------------------------------------


def import_records(
    session: Session, source: str, records: list[dict], *, create_wants: bool = False
) -> ImportResult:
    """Import a list of `{title, price, quantity, date, ...}` dicts.

    This is the shape the extension captures and the shape the CSV importer
    normalizes to, so there is exactly one code path into the database.
    """
    result = ImportResult(source=source)

    for record in records:
        try:
            title = (record.get("title") or record.get("name") or "").strip()
            if not title:
                result.skipped += 1
                continue

            signal = record_signal(
                session,
                source=source,
                title=title,
                quantity=int(_to_float(record.get("quantity")) or 1),
                price=_to_float(record.get("price") or record.get("price_paid")),
                condition=record.get("condition") or None,
                category=record.get("category") or None,
                brand=record.get("brand") or None,
                occurred_at=_to_date(record.get("date") or record.get("occurred_at")),
                external_id=record.get("id") or record.get("external_id"),
                raw=record if len(str(record)) < 8000 else None,
            )
            if signal is None:
                result.skipped += 1
                continue
            result.added += 1

            if create_wants and _ensure_want(session, signal):
                result.wants_created += 1
        except Exception as exc:  # one bad row must not sink the import
            result.errors.append(f"{record.get('title', '?')}: {exc}")

    session.flush()
    log.info(result.summary())
    return result


def _ensure_want(session: Session, signal: DemandSignal) -> bool:
    """Turn a wanting-signal into a want-list entry if one doesn't exist.

    Only for sources that mean "I want this" — a purchase says you already had
    the want and satisfied it, so it must not create a new one.
    """
    if signal.intent_weight < 0.8:
        return False

    existing = session.scalar(select(WantItem).where(WantItem.query_key == signal.query_key))
    if existing is not None:
        return False

    session.add(
        WantItem(
            label=signal.title[:200],
            query_key=signal.query_key,
            keywords=signal.title[:200],
            category=signal.category,
            replenishment=infer_class(
                signal.title, category=signal.category, price=signal.price_paid
            ),
            source=signal.source,
            priority=signal.intent_weight,
            notes=f"auto-created from {signal.source}",
        )
    )
    return True


# --------------------------------------------------------------------------
# CSV / paste import (works today, no browser needed)
# --------------------------------------------------------------------------


def import_csv(
    session: Session, source: str, text: str, *, create_wants: bool = False
) -> ImportResult:
    """Import a CSV export. Columns are matched loosely and case-insensitively."""
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in row.items()})
    return import_records(session, source, rows, create_wants=create_wants)


def import_plain_list(
    session: Session, source: str, text: str, *, create_wants: bool = True
) -> ImportResult:
    """Import a plain newline-separated list — an Alexa list pasted verbatim.

    Handles the shapes these actually come in: bullets, trailing quantities,
    inline prices. Deliberately forgiving, because the alternative is Logan
    reformatting a shopping list by hand.
    """
    records: list[dict] = []
    for line in text.splitlines():
        cleaned = line.strip().lstrip("-•*·").strip()
        if not cleaned or cleaned.lower() in {"shopping list", "to-do", "to do"}:
            continue

        quantity = 1
        qty_match = QTY_RE.search(cleaned)
        if qty_match:
            quantity = int(qty_match.group(1))
            cleaned = QTY_RE.sub("", cleaned).strip()

        # A leading count: "3 boxes of screws"
        leading = re.match(r"^(\d{1,3})\s+(?=[a-zA-Z])", cleaned)
        if leading and not qty_match:
            quantity = int(leading.group(1))
            cleaned = cleaned[leading.end():].strip()

        price = _to_float(PRICE_RE.search(cleaned).group(0)) if PRICE_RE.search(cleaned) else None
        cleaned = PRICE_RE.sub("", cleaned).strip(" -–—,")

        if cleaned:
            records.append({"title": cleaned, "quantity": quantity, "price": price})

    return import_records(session, source, records, create_wants=create_wants)


# --------------------------------------------------------------------------
# Querying
# --------------------------------------------------------------------------


def history_for(session: Session, query_key: str) -> list[DemandSignal]:
    """Purchase/return history for one item family, for satiation."""
    from ..normalize import key_family

    family = key_family(query_key)
    return list(
        session.scalars(
            select(DemandSignal)
            .where(
                DemandSignal.query_key.like(f"{family}%"),
                DemandSignal.source.in_([
                    SignalSource.NELLIS_PURCHASE.value,
                    SignalSource.NELLIS_RETURN.value,
                    SignalSource.AMAZON_ORDER.value,
                ]),
            )
            .order_by(DemandSignal.occurred_at.desc())
        ).all()
    )


def wanting_signals(session: Session, *, limit: int = 500) -> list[DemandSignal]:
    """Signals that express a want rather than a completed purchase."""
    return list(
        session.scalars(
            select(DemandSignal)
            .where(
                DemandSignal.source.in_([
                    SignalSource.ALEXA_LIST.value,
                    SignalSource.AMAZON_CART.value,
                    SignalSource.AMAZON_SAVED.value,
                    SignalSource.NELLIS_WATCHLIST.value,
                    SignalSource.MANUAL.value,
                ])
            )
            .order_by(DemandSignal.occurred_at.desc())
            .limit(limit)
        ).all()
    )


def infer_replenishment_for_wants(session: Session) -> int:
    """Fill in replenishment classes for wants that haven't been hand-set.

    `replenishment_locked` is respected: once Logan corrects a guess, inference
    must never quietly overwrite it. Otherwise the classification would be a
    chore he has to redo.
    """
    updated = 0
    for want in session.scalars(select(WantItem).where(WantItem.replenishment_locked.is_(False))):
        guess = infer_class(want.label, category=want.category, price=want.max_worth_to_me)
        if guess != want.replenishment:
            want.replenishment = guess
            updated += 1
    session.flush()
    return updated


def set_replenishment(
    session: Session, want_id: int, replenishment: ReplenishmentClass
) -> WantItem | None:
    """Hand-correct a class and lock it against future inference."""
    want = session.get(WantItem, want_id)
    if want is None:
        return None
    want.replenishment = replenishment
    want.replenishment_locked = True
    session.flush()
    return want
