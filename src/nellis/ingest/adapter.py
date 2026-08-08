"""The adapter boundary — the single place site changes are repaired.

Every strategy returns the same `LotRecord`, so nothing downstream knows or
cares how the data was obtained. When Nellis redesigns, exactly one file in
this package changes and the fixture tests tell you which fields broke.

Extraction is written to be *tolerant* rather than exact: instead of depending
on brittle nested paths like `data.lot.currentBid.amount`, the normalizers walk
the payload looking for known key aliases. A site restructure that renames a
container but keeps field names costs nothing.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from dateutil import parser as date_parser

from .client import IngestError, PoliteClient

log = logging.getLogger(__name__)

MONEY_RE = re.compile(r"-?\$?\s*([0-9][0-9,]*\.?[0-9]*)")


# --------------------------------------------------------------------------
# Normalized record
# --------------------------------------------------------------------------


@dataclass
class LotRecord:
    nellis_id: str
    url: str
    title: str
    description: str | None = None
    category: str | None = None
    brand: str | None = None
    model: str | None = None
    upc: str | None = None
    condition_name: str | None = None
    condition_notes: str | None = None
    retail_price: float | None = None
    current_bid: float = 0.0
    bid_count: int = 0
    buyers_premium_rate: float | None = None
    location: str | None = None
    location_zip: str | None = None
    open_at: datetime | None = None
    close_at: datetime | None = None
    images: list[str] = field(default_factory=list)
    is_closed: bool = False
    final_price: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def missing_critical_fields(self) -> list[str]:
        """Fields without which valuation is meaningless. Used by `nellis doctor`."""
        missing = []
        if not self.title:
            missing.append("title")
        if self.retail_price is None:
            missing.append("retail_price")
        if self.close_at is None and not self.is_closed:
            missing.append("close_at")
        return missing


# --------------------------------------------------------------------------
# Tolerant value coercion
# --------------------------------------------------------------------------


def parse_money(value: Any) -> float | None:
    """Coerce money-ish values. Handles 1234, '1,234.00', '$1,234', {'amount': 12}."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("amount", "value", "price", "cents"):
            if key in value:
                parsed = parse_money(value[key])
                if parsed is not None:
                    # Heuristic: an integer 'cents' field is in cents.
                    return parsed / 100 if key == "cents" else parsed
        return None
    if isinstance(value, str):
        match = MONEY_RE.search(value.replace(",", ""))
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def parse_datetime(value: Any) -> datetime | None:
    """Coerce timestamps. Handles ISO strings and epoch seconds/millis."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            dt = date_parser.parse(value)
        except (ValueError, OverflowError, TypeError):
            return None
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return None


def parse_rate(value: Any) -> float | None:
    """Coerce a percentage. 15 and 0.15 both mean 15%."""
    raw = parse_money(value)
    if raw is None:
        return None
    if raw > 1.0:
        raw = raw / 100.0
    return raw if 0.0 <= raw < 1.0 else None


def deep_find(payload: Any, keys: tuple[str, ...], *, max_depth: int = 12) -> Any:
    """Breadth-first search for the first matching key, case/underscore-insensitive.

    This is what makes the adapters resilient: we look for *field names* anywhere
    in the payload rather than depending on a fixed nesting path.
    """
    wanted = {k.lower().replace("_", "") for k in keys}
    queue: list[tuple[Any, int]] = [(payload, 0)]
    while queue:
        node, depth = queue.pop(0)
        if depth > max_depth:
            continue
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.lower().replace("_", "") in wanted:
                    if value is not None and value != "":
                        return value
            for value in node.values():
                if isinstance(value, (dict, list)):
                    queue.append((value, depth + 1))
        elif isinstance(node, list):
            for item in node[:50]:
                if isinstance(item, (dict, list)):
                    queue.append((item, depth + 1))
    return None


def collect_dicts_with(payload: Any, required: tuple[str, ...], *, max_depth: int = 12) -> list[dict]:
    """Find every dict that looks like a lot — i.e. contains all `required` keys."""
    wanted = {k.lower().replace("_", "") for k in required}
    found: list[dict] = []
    queue: list[tuple[Any, int]] = [(payload, 0)]
    while queue:
        node, depth = queue.pop(0)
        if depth > max_depth:
            continue
        if isinstance(node, dict):
            present = {k.lower().replace("_", "") for k in node if isinstance(k, str)}
            if wanted <= present:
                found.append(node)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    queue.append((value, depth + 1))
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    queue.append((item, depth + 1))
    return found


# Field aliases. When Nellis renames something, add the new name here first —
# it is usually the entire fix.
ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "lotId", "productId", "itemId"),
    "title": ("title", "name", "productName", "lotTitle"),
    "description": ("description", "productDescription", "details", "longDescription"),
    "category": ("category", "categoryName", "department", "taxonomy"),
    "brand": ("brand", "manufacturer", "brandName"),
    "model": ("model", "modelNumber", "mpn"),
    "upc": ("upc", "gtin", "barcode", "ean"),
    "condition": ("condition", "conditionName", "itemCondition", "conditionTitle"),
    "condition_notes": ("conditionNotes", "conditionDescription", "notes", "conditionDetails"),
    "retail": ("retailPrice", "msrp", "retail", "estimatedRetailPrice", "compareAtPrice"),
    "current_bid": ("currentPrice", "currentBid", "bidAmount", "highBid", "price"),
    "bid_count": ("bidCount", "bids", "numBids", "totalBids"),
    "premium": ("buyersPremium", "buyerPremium", "premiumRate", "bpRate"),
    "location": ("location", "locationName", "store", "warehouse", "site"),
    "zip": ("zip", "zipCode", "postalCode"),
    "open_at": ("openTime", "startTime", "openDate", "startsAt"),
    "close_at": ("closeTime", "endTime", "closeDate", "endsAt", "closingTime", "expectedCloseTime"),
    "images": ("images", "photos", "imageUrls", "media"),
    "closed": ("isClosed", "closed", "ended", "isEnded"),
    "final": ("finalPrice", "winningBid", "soldPrice", "hammerPrice"),
}


def record_from_payload(node: dict, base_url: str) -> LotRecord | None:
    """Build a LotRecord from any dict that looks like a lot."""
    lot_id = deep_find(node, ALIASES["id"])
    title = deep_find(node, ALIASES["title"])
    if lot_id is None or not title:
        return None

    images_raw = deep_find(node, ALIASES["images"]) or []
    images: list[str] = []
    if isinstance(images_raw, list):
        for item in images_raw[:12]:
            if isinstance(item, str):
                images.append(item)
            elif isinstance(item, dict):
                url = item.get("url") or item.get("src") or item.get("fullPath")
                if isinstance(url, str):
                    images.append(url)

    closed_flag = deep_find(node, ALIASES["closed"])
    final_price = parse_money(deep_find(node, ALIASES["final"]))

    return LotRecord(
        nellis_id=str(lot_id),
        url=f"{base_url.rstrip('/')}/p/{lot_id}",
        title=str(title).strip(),
        description=_as_text(deep_find(node, ALIASES["description"])),
        category=_as_text(deep_find(node, ALIASES["category"])),
        brand=_as_text(deep_find(node, ALIASES["brand"])),
        model=_as_text(deep_find(node, ALIASES["model"])),
        upc=_as_text(deep_find(node, ALIASES["upc"])),
        condition_name=_as_text(deep_find(node, ALIASES["condition"])),
        condition_notes=_as_text(deep_find(node, ALIASES["condition_notes"])),
        retail_price=parse_money(deep_find(node, ALIASES["retail"])),
        current_bid=parse_money(deep_find(node, ALIASES["current_bid"])) or 0.0,
        bid_count=int(parse_money(deep_find(node, ALIASES["bid_count"])) or 0),
        buyers_premium_rate=parse_rate(deep_find(node, ALIASES["premium"])),
        location=_as_text(deep_find(node, ALIASES["location"])),
        location_zip=_as_text(deep_find(node, ALIASES["zip"])),
        open_at=parse_datetime(deep_find(node, ALIASES["open_at"])),
        close_at=parse_datetime(deep_find(node, ALIASES["close_at"])),
        images=images,
        is_closed=bool(closed_flag) or final_price is not None,
        final_price=final_price,
        raw=node if len(str(node)) < 60_000 else {"_truncated": True},
    )


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("name", "title", "value", "text"):
            if key in value and isinstance(value[key], str):
                return value[key].strip() or None
        return None
    if isinstance(value, list):
        parts = [_as_text(v) for v in value[:5]]
        joined = ", ".join(p for p in parts if p)
        return joined or None
    return str(value)


# --------------------------------------------------------------------------
# Strategy interface
# --------------------------------------------------------------------------


@dataclass
class SearchFilters:
    """Server-side filters, best-effort. Anything unsupported is applied locally."""

    query: str | None = None
    category: str | None = None
    location: str | None = None
    page: int = 1
    per_page: int = 60


class NellisAdapter(ABC):
    """One way of reading Nellis. Implementations are tried in priority order."""

    name: str = "abstract"
    priority: int = 100

    @abstractmethod
    async def search(self, client: PoliteClient, filters: SearchFilters) -> list[LotRecord]:
        ...

    @abstractmethod
    async def fetch_lot(self, client: PoliteClient, lot_id: str) -> LotRecord | None:
        ...


class AdapterChain:
    """Tries each adapter in priority order; first non-empty result wins.

    A failure in one strategy is logged and demoted, never fatal — except for
    BlockedError, which aborts everything by design.
    """

    def __init__(self, adapters: list[NellisAdapter]):
        self.adapters = sorted(adapters, key=lambda a: a.priority)
        self.last_successful: str | None = None

    async def search(self, client: PoliteClient, filters: SearchFilters) -> list[LotRecord]:
        errors: list[str] = []
        for adapter in self.adapters:
            try:
                results = await adapter.search(client, filters)
            except IngestError:
                raise
            except Exception as exc:  # strategy-specific parse failures
                log.warning("adapter %s search failed: %s", adapter.name, exc)
                errors.append(f"{adapter.name}: {exc}")
                continue
            if results:
                self.last_successful = adapter.name
                log.info("adapter %s returned %d lots", adapter.name, len(results))
                return results
            errors.append(f"{adapter.name}: 0 results")
        log.error("all adapters failed: %s", "; ".join(errors))
        return []

    async def fetch_lot(self, client: PoliteClient, lot_id: str) -> LotRecord | None:
        for adapter in self.adapters:
            try:
                record = await adapter.fetch_lot(client, lot_id)
            except IngestError:
                raise
            except Exception as exc:
                log.warning("adapter %s fetch_lot failed: %s", adapter.name, exc)
                continue
            if record is not None:
                self.last_successful = adapter.name
                return record
        return None
