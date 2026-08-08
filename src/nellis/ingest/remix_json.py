"""Strategies 1 & 2: structured JSON extraction.

Nellis is a Remix app, so the server ships loader data to the browser as JSON
embedded in the page (`window.__remixContext`). Reading that is far more stable
than scraping rendered DOM — field names survive visual redesigns.

Strategy 1 (`EmbeddedJsonAdapter`) parses that blob out of ordinary page HTML.
Strategy 2 (`RemixDataAdapter`) hits `?_data=<routeId>` to get the same JSON
without the HTML wrapper — cheaper, but depends on route IDs, which we learn
from strategy 1 rather than hardcoding.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .adapter import (
    LotRecord,
    NellisAdapter,
    SearchFilters,
    collect_dicts_with,
    record_from_payload,
)
from .client import PoliteClient

log = logging.getLogger(__name__)

# Script payloads that commonly carry loader/state data.
_ASSIGN_RE = re.compile(
    r"window\.(?:__remixContext|__remixRouteModules|__NEXT_DATA__|__INITIAL_STATE__|__APP_STATE__)"
    r"\s*=\s*(\{.*?\});?\s*(?:</script>|window\.)",
    re.DOTALL,
)
_SCRIPT_JSON_RE = re.compile(
    r'<script[^>]*type=["\'](?:application/json|application/ld\+json)["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# A dict is "lot-shaped" if it carries an identifier plus a title-ish field.
_LOT_SHAPES: tuple[tuple[str, ...], ...] = (
    ("id", "currentPrice"),
    ("id", "retailPrice"),
    ("id", "title"),
    ("lotId", "title"),
    ("productId", "name"),
)


def extract_json_blobs(html: str) -> list[Any]:
    """Pull every plausible JSON payload out of a page."""
    blobs: list[Any] = []

    for match in _ASSIGN_RE.finditer(html):
        parsed = _loads_balanced(match.group(1))
        if parsed is not None:
            blobs.append(parsed)

    for match in _SCRIPT_JSON_RE.finditer(html):
        text = match.group(1).strip()
        if not text:
            continue
        try:
            blobs.append(json.loads(text))
        except json.JSONDecodeError:
            continue

    return blobs


def _loads_balanced(text: str) -> Any:
    """Parse a JSON object that a greedy regex may have over- or under-captured."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[: index + 1])
                except json.JSONDecodeError:
                    return None
    return None


def lots_from_blobs(blobs: list[Any], base_url: str) -> list[LotRecord]:
    """Find every lot-shaped dict across all blobs and normalize it."""
    seen: dict[str, LotRecord] = {}
    for blob in blobs:
        for shape in _LOT_SHAPES:
            for node in collect_dicts_with(blob, shape):
                record = record_from_payload(node, base_url)
                if record is None:
                    continue
                # Prefer the richest version of a duplicate.
                existing = seen.get(record.nellis_id)
                if existing is None or _richness(record) > _richness(existing):
                    seen[record.nellis_id] = record
    return list(seen.values())


def _richness(record: LotRecord) -> int:
    fields = (
        record.retail_price,
        record.close_at,
        record.description,
        record.condition_name,
        record.category,
    )
    return sum(1 for f in fields if f is not None)


def discover_route_ids(html: str) -> list[str]:
    """Learn Remix route IDs from the page instead of hardcoding them."""
    ids = set(re.findall(r'"(routes/[A-Za-z0-9_.$\-/]+)"', html))
    return sorted(ids)


class EmbeddedJsonAdapter(NellisAdapter):
    """Strategy 1: parse loader JSON out of normal page HTML. Most robust."""

    name = "embedded-json"
    priority = 10

    async def search(self, client: PoliteClient, filters: SearchFilters) -> list[LotRecord]:
        params: dict[str, Any] = {}
        if filters.query:
            params["query"] = filters.query
        if filters.category:
            params["category"] = filters.category
        if filters.location:
            params["location"] = filters.location
        if filters.page > 1:
            params["page"] = filters.page

        fetched = await client.get("/search", params=params)
        blobs = extract_json_blobs(fetched.text)
        return lots_from_blobs(blobs, client.settings.nellis_base_url)

    async def fetch_lot(self, client: PoliteClient, lot_id: str) -> LotRecord | None:
        fetched = await client.get(f"/p/{lot_id}")
        blobs = extract_json_blobs(fetched.text)
        records = lots_from_blobs(blobs, client.settings.nellis_base_url)
        for record in records:
            if record.nellis_id == str(lot_id):
                return record
        return records[0] if records else None


class RemixDataAdapter(NellisAdapter):
    """Strategy 2: `?_data=<routeId>` loader endpoint. Cheaper when it works."""

    name = "remix-data"
    priority = 20

    def __init__(self) -> None:
        self._search_routes: list[str] = []
        self._lot_routes: list[str] = []

    async def _ensure_routes(self, client: PoliteClient, path: str, kind: str) -> list[str]:
        cached = self._search_routes if kind == "search" else self._lot_routes
        if cached:
            return cached
        fetched = await client.get(path)
        discovered = discover_route_ids(fetched.text)
        needle = "search" if kind == "search" else "p."
        ranked = [r for r in discovered if needle in r] or discovered
        if kind == "search":
            self._search_routes = ranked[:4]
            return self._search_routes
        self._lot_routes = ranked[:4]
        return self._lot_routes

    async def search(self, client: PoliteClient, filters: SearchFilters) -> list[LotRecord]:
        routes = await self._ensure_routes(client, "/search", "search")
        for route in routes:
            params: dict[str, Any] = {"_data": route}
            if filters.query:
                params["query"] = filters.query
            if filters.category:
                params["category"] = filters.category
            if filters.page > 1:
                params["page"] = filters.page
            try:
                fetched = await client.get("/search", params=params, accept_json=True)
                payload = fetched.json()
            except (ValueError, Exception) as exc:  # noqa: B014 - json or transport
                log.debug("route %s not usable: %s", route, exc)
                continue
            records = lots_from_blobs([payload], client.settings.nellis_base_url)
            if records:
                return records
        return []

    async def fetch_lot(self, client: PoliteClient, lot_id: str) -> LotRecord | None:
        routes = await self._ensure_routes(client, f"/p/{lot_id}", "lot")
        for route in routes:
            try:
                fetched = await client.get(
                    f"/p/{lot_id}", params={"_data": route}, accept_json=True
                )
                payload = fetched.json()
            except Exception as exc:
                log.debug("route %s not usable: %s", route, exc)
                continue
            records = lots_from_blobs([payload], client.settings.nellis_base_url)
            for record in records:
                if record.nellis_id == str(lot_id):
                    return record
            if records:
                return records[0]
        return None
