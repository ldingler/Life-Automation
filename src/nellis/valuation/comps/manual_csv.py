"""Comps from your own CSV files, and from an optional third-party feed.

Two providers live here, both covering sources with no usable public API:

  * `ManualCsvProvider` — you drop price sheets into `data/comps/*.csv`. This is
    the supported path for Facebook Marketplace, which blocks scraping and has
    no public listings API, and for any category where your own knowledge beats
    a marketplace median.

  * `LocalFeedProvider` — reads an RSS/JSON feed URL you supply. Craigslist
    discontinued its native RSS feeds, so if you want local comps automated you
    point this at a feed service of your choosing. Unset by default.

CSV columns (header required, extras ignored):
    title,price,shipping,condition,sold_at,url,source
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import httpx
from dateutil import parser as date_parser

from ...config import Settings
from ...normalize import key_family, normalize_item_key
from .base import CompPoint, CompsProvider, ItemQuery, score_similarity

log = logging.getLogger(__name__)

PRICE_RE = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]{2})?)")


class ManualCsvProvider(CompsProvider):
    name = "csv"

    def __init__(self, directory: Path):
        self.directory = directory
        self._cache: list[dict] | None = None
        self.enabled = True

    def _load(self) -> list[dict]:
        if self._cache is not None:
            return self._cache
        rows: list[dict] = []
        if self.directory.exists():
            for path in sorted(self.directory.glob("*.csv")):
                try:
                    with path.open(newline="", encoding="utf-8-sig") as handle:
                        for row in csv.DictReader(handle):
                            normalized = {
                                (k or "").strip().lower(): (v or "").strip()
                                for k, v in row.items()
                            }
                            if normalized.get("title") and normalized.get("price"):
                                rows.append(normalized)
                except (OSError, csv.Error) as exc:
                    log.warning("could not read %s: %s", path, exc)
        self._cache = rows
        log.info("loaded %d manual comps from %s", len(rows), self.directory)
        return rows

    async def fetch(self, query: ItemQuery, *, limit: int = 40) -> list[CompPoint]:
        family = key_family(query.key)
        points: list[CompPoint] = []
        for row in self._load():
            title = row["title"]
            row_key = key_family(normalize_item_key(title))
            similarity = score_similarity(query, title)
            if row_key != family and similarity < 0.45:
                continue
            price = _to_float(row.get("price"))
            if price is None:
                continue
            points.append(
                CompPoint(
                    price=price,
                    shipping=_to_float(row.get("shipping")) or 0.0,
                    source="csv",
                    title=title,
                    condition=row.get("condition") or None,
                    is_sold=True,
                    sold_at=_to_date(row.get("sold_at")),
                    url=row.get("url") or None,
                    similarity=similarity,
                )
            )
            if len(points) >= limit:
                break
        return points


class LocalFeedProvider(CompsProvider):
    """Reads an operator-supplied RSS/Atom feed of local listings."""

    name = "feed"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client
        self.enabled = bool(settings.local_comps_feed_url)

    async def fetch(self, query: ItemQuery, *, limit: int = 40) -> list[CompPoint]:
        if not self.enabled or not self.settings.local_comps_feed_url:
            return []
        from ...normalize import search_query

        url = self.settings.local_comps_feed_url.replace(
            "{query}", search_query(query.title, brand=query.brand, model=query.model)
        )
        client = self._client or httpx.AsyncClient(timeout=20.0)
        try:
            response = await client.get(url, headers={"User-Agent": self.settings.user_agent})
            if response.status_code != 200:
                log.warning("local feed returned %s", response.status_code)
                return []
            return self._parse_feed(response.text, query, limit)
        except (httpx.HTTPError, ElementTree.ParseError) as exc:
            log.warning("local feed failed: %s", exc)
            return []
        finally:
            if self._client is None:
                await client.aclose()

    def _parse_feed(self, text: str, query: ItemQuery, limit: int) -> list[CompPoint]:
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError:
            return []

        points: list[CompPoint] = []
        # Handle both RSS (<item>) and Atom (<entry>) without namespace assumptions.
        nodes = [n for n in root.iter() if n.tag.split("}")[-1] in ("item", "entry")]
        for node in nodes[: limit * 2]:
            fields = {child.tag.split("}")[-1]: child for child in node}
            title = (fields.get("title").text if fields.get("title") is not None else "") or ""
            body = " ".join(
                (child.text or "") for tag, child in fields.items() if tag in ("description", "summary", "content")
            )
            match = PRICE_RE.search(title) or PRICE_RE.search(body)
            if not match:
                continue
            price = _to_float(match.group(1))
            if price is None or price <= 0:
                continue
            similarity = score_similarity(query, title)
            if similarity < 0.45:
                continue
            link_node = fields.get("link")
            link = (link_node.text or link_node.get("href")) if link_node is not None else None
            points.append(
                CompPoint(
                    price=price,
                    source="feed",
                    title=title,
                    is_sold=False,  # local listings are asking prices
                    sold_at=_to_date(
                        fields["pubDate"].text if fields.get("pubDate") is not None else None
                    ),
                    url=link,
                    similarity=similarity,
                )
            )
            if len(points) >= limit:
                break
        return points


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _to_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
