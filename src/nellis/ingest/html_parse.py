"""Strategy 3: heuristic DOM parsing.

Last resort before a real browser. Rather than depend on CSS class names (which
change with every redesign and which we cannot verify without live access), this
walks anchors that look like lot links and reads money/text out of the
surrounding card by shape. Less precise than the JSON strategies, but it
degrades gracefully instead of returning nothing.
"""

from __future__ import annotations

import logging
import re

from selectolax.parser import HTMLParser, Node

from .adapter import LotRecord, NellisAdapter, SearchFilters, parse_money
from .client import PoliteClient

log = logging.getLogger(__name__)

LOT_HREF_RE = re.compile(r"/p/(\d+)")
RETAIL_HINT_RE = re.compile(r"(retail|msrp|value|worth)", re.IGNORECASE)
BID_HINT_RE = re.compile(r"(current|bid)", re.IGNORECASE)
MONEY_TEXT_RE = re.compile(r"\$\s?[0-9][0-9,]*(?:\.[0-9]{2})?")


def _card_root(node: Node, levels: int = 4) -> Node:
    """Climb to a plausible card container so sibling price text is in scope."""
    current = node
    for _ in range(levels):
        parent = current.parent
        if parent is None or parent.tag in ("body", "html"):
            break
        current = parent
    return current


def _nearest_hint_distance(text: str, pattern: re.Pattern[str], position: int) -> int | None:
    """Distance from `position` back to the closest preceding label match.

    Labels precede their values ("Current Bid $41.00"), so we look backward.
    Looking forward as well causes the *next* field's label to steal this
    field's value when both sit in the same text run.
    """
    best: int | None = None
    window_start = max(0, position - 48)
    for match in pattern.finditer(text, window_start, position):
        distance = position - match.end()
        if best is None or distance < best:
            best = distance
    return best


def _money_near(root: Node) -> tuple[float | None, float | None]:
    """Split money strings in a card into (current_bid, retail) by nearby wording."""
    text = root.text(separator=" ", strip=True) or ""
    amounts = [parse_money(m) for m in MONEY_TEXT_RE.findall(text)]
    amounts = [a for a in amounts if a is not None]
    if not amounts:
        return None, None

    current: float | None = None
    retail: float | None = None
    for match in MONEY_TEXT_RE.finditer(text):
        value = parse_money(match.group(0))
        if value is None:
            continue
        retail_distance = _nearest_hint_distance(text, RETAIL_HINT_RE, match.start())
        bid_distance = _nearest_hint_distance(text, BID_HINT_RE, match.start())
        if retail_distance is None and bid_distance is None:
            continue
        # Closest label wins; ties go to retail, which is the more distinctive word.
        prefers_retail = bid_distance is None or (
            retail_distance is not None and retail_distance <= bid_distance
        )
        if prefers_retail and retail is None:
            retail = value
        elif not prefers_retail and current is None:
            current = value

    # Fall back on the usual layout: the smaller figure is the bid, larger is retail.
    if current is None and retail is None and amounts:
        ordered = sorted(amounts)
        current = ordered[0]
        retail = ordered[-1] if len(ordered) > 1 else None
    elif current is None:
        candidates = [a for a in amounts if retail is None or a != retail]
        current = min(candidates) if candidates else None
    elif retail is None:
        candidates = [a for a in amounts if a > current]
        retail = max(candidates) if candidates else None

    return current, retail


def _title_near(root: Node, anchor: Node) -> str | None:
    text = (anchor.text(strip=True) or "").strip()
    if len(text) > 5:
        return text
    img = root.css_first("img[alt]")
    if img:
        alt = (img.attributes.get("alt") or "").strip()
        if len(alt) > 5:
            return alt
    for tag in ("h1", "h2", "h3", "h4"):
        heading = root.css_first(tag)
        if heading:
            heading_text = (heading.text(strip=True) or "").strip()
            if len(heading_text) > 5:
                return heading_text
    return None


def parse_lot_cards(html: str, base_url: str) -> list[LotRecord]:
    tree = HTMLParser(html)
    records: dict[str, LotRecord] = {}

    for anchor in tree.css("a[href]"):
        href = anchor.attributes.get("href") or ""
        match = LOT_HREF_RE.search(href)
        if not match:
            continue
        lot_id = match.group(1)
        if lot_id in records:
            continue

        root = _card_root(anchor)
        title = _title_near(root, anchor)
        if not title:
            continue
        current, retail = _money_near(root)
        image = root.css_first("img[src]")

        records[lot_id] = LotRecord(
            nellis_id=lot_id,
            url=f"{base_url.rstrip('/')}/p/{lot_id}",
            title=title,
            retail_price=retail,
            current_bid=current or 0.0,
            images=[image.attributes.get("src")] if image and image.attributes.get("src") else [],
            raw={"_source": "html-dom"},
        )

    return list(records.values())


class HtmlDomAdapter(NellisAdapter):
    name = "html-dom"
    priority = 30

    async def search(self, client: PoliteClient, filters: SearchFilters) -> list[LotRecord]:
        params = {}
        if filters.query:
            params["query"] = filters.query
        if filters.page > 1:
            params["page"] = filters.page
        fetched = await client.get("/search", params=params)
        return parse_lot_cards(fetched.text, client.settings.nellis_base_url)

    async def fetch_lot(self, client: PoliteClient, lot_id: str) -> LotRecord | None:
        fetched = await client.get(f"/p/{lot_id}")
        records = parse_lot_cards(fetched.text, client.settings.nellis_base_url)
        for record in records:
            if record.nellis_id == str(lot_id):
                return record

        # Detail pages may not self-link; parse the document as one big card.
        tree = HTMLParser(fetched.text)
        body = tree.css_first("body")
        if body is None:
            return None
        title_node = tree.css_first("h1") or tree.css_first("title")
        title = title_node.text(strip=True) if title_node else None
        if not title:
            return None
        current, retail = _money_near(body)
        return LotRecord(
            nellis_id=str(lot_id),
            url=f"{client.settings.nellis_base_url.rstrip('/')}/p/{lot_id}",
            title=title,
            retail_price=retail,
            current_bid=current or 0.0,
            raw={"_source": "html-dom-detail"},
        )
