"""Ingestion parser tests. Fully offline — fixtures only, no network."""

from __future__ import annotations

import json

import httpx
import pytest

from nellis.ingest.adapter import (
    parse_datetime,
    parse_money,
    parse_rate,
    record_from_payload,
)
from nellis.ingest.client import BlockedError, BudgetExhausted, IngestError, PoliteClient
from nellis.ingest.html_parse import parse_lot_cards
from nellis.ingest.remix_json import (
    EmbeddedJsonAdapter,
    discover_route_ids,
    extract_json_blobs,
    lots_from_blobs,
)

from .conftest import fixture_text


class TestCoercion:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (1234, 1234.0),
            ("1,234.50", 1234.5),
            ("$99", 99.0),
            ({"amount": 42.5}, 42.5),
            ({"cents": 1250}, 12.5),
            (None, None),
            (True, None),
            ("free", None),
        ],
    )
    def test_parse_money(self, raw, expected):
        assert parse_money(raw) == expected

    def test_parse_rate_accepts_both_conventions(self):
        assert parse_rate(15) == 0.15
        assert parse_rate(0.15) == 0.15
        assert parse_rate(150) is None

    def test_parse_datetime_iso_and_epoch(self):
        assert parse_datetime("2026-08-09T18:30:00Z").year == 2026
        assert parse_datetime(1754765400).year == 2025
        assert parse_datetime(1754765400000).year == 2025
        assert parse_datetime("not a date") is None


class TestEmbeddedJson:
    def test_extracts_all_lots_from_remix_context(self):
        blobs = extract_json_blobs(fixture_text("search_page.html"))
        assert blobs, "should find the __remixContext payload"
        records = lots_from_blobs(blobs, "https://www.nellisauction.com")
        assert len(records) == 4

        drill = next(r for r in records if r.nellis_id == "1001")
        assert drill.retail_price == 299.0
        assert drill.current_bid == 41.0
        assert drill.bid_count == 7
        assert drill.brand == "DeWalt"
        assert drill.buyers_premium_rate == 0.15
        assert drill.close_at is not None
        assert drill.url.endswith("/p/1001")
        assert drill.missing_critical_fields() == []

    def test_detail_page_yields_description(self):
        blobs = extract_json_blobs(fixture_text("lot_page.html"))
        records = lots_from_blobs(blobs, "https://www.nellisauction.com")
        blender = next(r for r in records if r.nellis_id == "1002")
        assert "Missing the lid" in (blender.description or "")

    def test_closed_lot_detected(self):
        payload = json.loads(fixture_text("closed_lots.json"))
        records = lots_from_blobs([payload], "https://www.nellisauction.com")
        assert len(records) == 3
        assert all(r.is_closed for r in records)
        assert {r.final_price for r in records} == {96.0, 104.0, 88.0}

    def test_route_discovery_finds_real_ids(self):
        routes = discover_route_ids(fixture_text("search_page.html"))
        assert "routes/search" in routes

    def test_record_requires_id_and_title(self):
        assert record_from_payload({"currentPrice": 5}, "https://x") is None
        assert record_from_payload({"id": 1, "title": ""}, "https://x") is None

    def test_deep_search_survives_restructuring(self):
        """A renamed container must not break extraction — that's the whole point."""
        nested = {"data": {"page": {"widgets": [{"payload": {
            "id": 77, "title": "Thing", "currentPrice": 9.0, "retailPrice": 50.0}}]}}}
        records = lots_from_blobs([nested], "https://x")
        assert len(records) == 1
        assert records[0].retail_price == 50.0


class TestHtmlDomFallback:
    def test_parses_cards_without_any_json(self):
        records = parse_lot_cards(
            fixture_text("search_page_nojson.html"), "https://www.nellisauction.com"
        )
        assert len(records) == 4
        drill = next(r for r in records if r.nellis_id == "1001")
        assert drill.current_bid == 41.0
        assert drill.retail_price == 299.0
        assert "DeWalt" in drill.title


class TestPoliteClient:
    async def _client(self, handler):
        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport)
        client = PoliteClient(client=http)
        await client.__aenter__()
        return client

    @pytest.mark.asyncio
    async def test_blocked_status_aborts_the_run(self):
        client = await self._client(lambda req: httpx.Response(429, text="slow down"))
        with pytest.raises(BlockedError, match="back off"):
            await client.get("/search")

    @pytest.mark.asyncio
    async def test_403_also_aborts(self):
        client = await self._client(lambda req: httpx.Response(403, text="no"))
        with pytest.raises(BlockedError):
            await client.get("/search")

    @pytest.mark.asyncio
    async def test_repeated_500s_abort_the_run(self):
        """Nellis rate-limits with 500, not 429.

        Their help centre states 500s mean "too many requests from the same
        network". That throttle is network-wide, so grinding through it locks
        the operator out of nellisauction.com in their own browser — which is
        exactly what happened in practice. A few 500s must end the run.
        """
        client = await self._client(lambda req: httpx.Response(500, text="boom"))
        client.settings.server_error_stop_threshold = 3

        for _ in range(2):
            with pytest.raises(IngestError) as first:
                await client.get("/search", use_cache=False)
            assert not isinstance(first.value, BlockedError)

        with pytest.raises(BlockedError, match="rate limiting"):
            await client.get("/search", use_cache=False)

    @pytest.mark.asyncio
    async def test_a_success_resets_the_500_counter(self):
        """Isolated blips must not accumulate into a false stop."""
        responses = [500, 200, 500, 500]

        def handler(request):
            return httpx.Response(responses.pop(0), text="x")

        client = await self._client(handler)
        client.settings.server_error_stop_threshold = 3

        with pytest.raises(IngestError):
            await client.get("/a", use_cache=False)
        await client.get("/b", use_cache=False)  # success clears the streak

        for path in ("/c", "/d"):
            with pytest.raises(IngestError) as exc:
                await client.get(path, use_cache=False)
            assert not isinstance(exc.value, BlockedError), "counter did not reset"

    @pytest.mark.asyncio
    async def test_request_budget_is_enforced(self, monkeypatch):
        client = await self._client(lambda req: httpx.Response(200, text="ok"))
        client.settings.max_requests_per_run = 2
        await client.get("/a", use_cache=False)
        await client.get("/b", use_cache=False)
        with pytest.raises(BudgetExhausted):
            await client.get("/c", use_cache=False)

    @pytest.mark.asyncio
    async def test_cache_prevents_duplicate_requests(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, text="payload")

        client = await self._client(handler)
        first = await client.get("/search")
        second = await client.get("/search")
        assert calls["n"] == 1
        assert first.from_cache is False
        assert second.from_cache is True

    @pytest.mark.asyncio
    async def test_adapter_search_end_to_end(self):
        html = fixture_text("search_page.html")
        client = await self._client(lambda req: httpx.Response(200, text=html))
        from nellis.ingest.adapter import SearchFilters

        records = await EmbeddedJsonAdapter().search(client, SearchFilters(query="drill"))
        assert len(records) == 4
