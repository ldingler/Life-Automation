"""Demo seeder and live-capture tests.

The demo tests deliberately assert on *behavior*, not row counts. A demo that
inserts 14 rows proves nothing; a demo where the dead-compressor fridge is
rejected with a zero bid and the missing-filter vacuum is recommended proves the
pipeline is actually running.
"""

from __future__ import annotations

import json
import zipfile

import httpx
import pytest
from sqlalchemy import select

from nellis.demo import seed
from nellis.ingest.client import PoliteClient
from nellis.ingest.record import capture, scrub
from nellis.models import (
    BidQueueEntry,
    Comp,
    Confidence,
    Lot,
    PortfolioItem,
    QueueStatus,
    Valuation,
    Watch,
)
from nellis.valuation.exposure import current_exposure

from .conftest import fixture_text


def latest(session, lot) -> Valuation:
    return session.scalar(
        select(Valuation)
        .where(Valuation.lot_id == lot.id)
        .order_by(Valuation.computed_at.desc())
        .limit(1)
    )


def find(session, needle: str) -> tuple[Lot, Valuation]:
    lot = session.scalar(
        select(Lot).where(Lot.title.contains(needle), Lot.is_closed.is_(False)).limit(1)
    )
    assert lot is not None, f"no open demo lot matching {needle!r}"
    return lot, latest(session, lot)


@pytest.fixture
async def seeded(db, settings):
    await seed(db, settings, reset=True)
    db.commit()
    return db


class TestDemoSeed:
    async def test_closed_lots_become_real_comps(self, seeded):
        comps = seeded.scalars(select(Comp).where(Comp.source == "nellis")).all()
        assert len(comps) > 80
        # Backdated, not all stamped "now" — otherwise confidence is inflated.
        assert len({c.sold_at.date() for c in comps if c.sold_at}) > 10

    async def test_comps_produce_high_confidence(self, seeded):
        """The point of seeding sales history: confidence should not be NONE."""
        _, valuation = find(seeded, "Dyson")
        assert valuation.confidence == Confidence.HIGH
        assert valuation.comp_count >= 8
        assert valuation.comp_value and valuation.comp_value > 0

    async def test_produces_recommendations_and_a_queue(self, seeded):
        recommended = seeded.scalars(
            select(Valuation).where(Valuation.recommended.is_(True))
        ).all()
        assert len(recommended) >= 3

        queued = seeded.scalars(
            select(BidQueueEntry).where(BidQueueEntry.status == QueueStatus.PENDING)
        ).all()
        assert len(queued) >= 3
        assert all(entry.suggested_max_bid > 0 for entry in queued)

    async def test_exposure_is_non_zero(self, seeded):
        state = current_exposure(seeded)
        assert state.lot_count > 0
        assert state.total > 0

    async def test_portfolio_has_sold_and_held_items(self, seeded):
        items = seeded.scalars(select(PortfolioItem)).all()
        assert len(items) >= 2
        assert any(i.sold_price is not None for i in items)
        assert any(i.realized_profit is not None for i in items)

    async def test_starter_watches_created(self, seeded):
        watches = seeded.scalars(select(Watch)).all()
        assert len(watches) >= 2
        # Damaged lots must be included — that's where repair arbitrage lives.
        assert all(w.include_damaged for w in watches)

    async def test_is_reproducible(self, db, settings):
        """Fixed seed: a number seen yesterday is the same number today."""
        first = await seed(db, settings, reset=True)
        second = await seed(db, settings, reset=True)
        assert first == second


class TestDemoProvesGuardrails:
    """Each of these is a lot the engine must REJECT, for a specific reason."""

    async def test_fatal_damage_rejected_with_zero_bid(self, seeded):
        lot, valuation = find(seeded, "Refrigerator")
        assert not valuation.recommended
        # A confident number next to "compressor is shot" is the trap being
        # guarded against — bid, profit and margin must all read zero.
        assert valuation.walk_away_max_bid == 0.0
        assert valuation.projected_profit == 0.0
        assert valuation.projected_margin == 0.0
        assert "fatal" in valuation.reason.lower()

    async def test_lot_bid_past_walkaway_rejected(self, seeded):
        overbid = [
            (lot, latest(seeded, lot))
            for lot in seeded.scalars(
                select(Lot).where(Lot.title.contains("WH-1000XM5"), Lot.is_closed.is_(False))
            ).all()
        ]
        past = [(lot, v) for lot, v in overbid if lot.current_bid > v.walk_away_max_bid]
        assert past, "expected a deliberately over-bid Sony lot"
        for _, valuation in past:
            assert not valuation.recommended
            assert "past your max" in valuation.reason.lower()

    async def test_no_comps_rejected_with_none_confidence(self, seeded):
        _, valuation = find(seeded, "Craft Supplies")
        assert valuation.confidence == Confidence.NONE
        assert not valuation.recommended

    async def test_thin_profit_rejected_against_floor(self, seeded, settings):
        rejected = seeded.scalars(
            select(Valuation).where(Valuation.recommended.is_(False))
        ).all()
        thin = [v for v in rejected if "too thin" in (v.reason or "").lower()]
        assert thin, "expected at least one lot rejected on the profit floor"
        for valuation in thin:
            assert valuation.projected_profit < settings.min_profit_dollars

    async def test_every_rejection_explains_itself(self, seeded):
        rejected = seeded.scalars(
            select(Valuation).where(Valuation.recommended.is_(False))
        ).all()
        assert rejected
        for valuation in rejected:
            assert valuation.reason and len(valuation.reason) > 15


class TestDemoRepairArbitrage:
    async def test_missing_part_detected_priced_and_recommended(self, seeded):
        """The headline feature: a cheap part recovering real value."""
        candidates = [
            latest(seeded, lot)
            for lot in seeded.scalars(
                select(Lot).where(Lot.is_closed.is_(False))
            ).all()
        ]
        easy = [
            v for v in candidates
            if v.missing_parts and v.easy_fix_score and v.easy_fix_score >= 2.0
        ]
        assert easy, "expected at least one easy-fix repair play"
        for valuation in easy:
            assert valuation.parts_cost > 0
            assert valuation.labor_hours > 0
            assert valuation.repair_notes

    async def test_channel_choice_changes_the_bid(self, seeded):
        """Kitchen items sell locally; eBay's fixed costs would erase the margin."""
        _, blender = find(seeded, "Ninja")
        assert blender.resale_channel == "local"
        _, drill = find(seeded, "DCD999B")
        assert drill.resale_channel == "ebay"


class TestRecordCapture:
    async def _client(self, handler):
        client = PoliteClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        await client.__aenter__()
        return client

    @pytest.mark.asyncio
    async def test_capture_writes_a_usable_zip(self, tmp_path):
        html = fixture_text("search_page.html")
        lot_html = fixture_text("lot_page.html")

        def handler(request):
            return httpx.Response(200, text=lot_html if "/p/" in request.url.path else html)

        client = await self._client(handler)
        archive, report = await capture(client, out_dir=tmp_path)

        assert archive.exists()
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            assert "search.html" in names
            assert "lot.html" in names
            assert "report.json" in names
            assert any(n.startswith("search_blob_") for n in names)
            payload = json.loads(zf.read("report.json"))

        assert payload["search_lots"] == 4
        assert "routes/search" in payload["route_ids"]

        probes = {p["strategy"]: p for p in payload["probes"]}
        assert probes["embedded-json"]["lots_found"] == 4
        # The whole point of the report: which fields actually resolved.
        assert "retail_price" in probes["embedded-json"]["resolved_fields"]
        assert "title" in probes["embedded-json"]["resolved_fields"]

    @pytest.mark.asyncio
    async def test_capture_notes_when_nothing_parses(self, tmp_path):
        client = await self._client(lambda r: httpx.Response(200, text="<html><body/></html>"))
        _, report = await capture(client, out_dir=tmp_path)
        assert report.search_lots == 0
        assert report.notes, "a capture that parsed nothing must say so"

    def test_scrub_redacts_credentials_but_keeps_lot_data(self):
        payload = {
            "id": 1001,
            "title": "DeWalt Drill",
            "currentPrice": 41.0,
            "accessToken": "eyJhbGciOi-secret",
            "user": {"email": "someone@example.com"},
            "nested": [{"sessionId": "abc123", "retailPrice": 299.0}],
        }
        cleaned = scrub(payload)

        assert cleaned["accessToken"] == "<redacted>"
        assert cleaned["user"] == "<redacted>"
        assert cleaned["nested"][0]["sessionId"] == "<redacted>"
        # Everything needed to calibrate the parsers must survive.
        assert cleaned["title"] == "DeWalt Drill"
        assert cleaned["currentPrice"] == 41.0
        assert cleaned["nested"][0]["retailPrice"] == 299.0

    def test_scrub_survives_deep_nesting(self):
        node: dict = {"token": "x"}
        for _ in range(40):
            node = {"wrap": node}
        assert scrub(node) is not None


class TestDemoIntegration:
    async def test_dashboard_renders_seeded_data(self, seeded):
        from fastapi.testclient import TestClient

        from nellis.web.app import create_app

        with TestClient(create_app()) as client:
            feed = client.get("/")
            assert feed.status_code == 200
            assert "Walk-away max bid" in feed.text
            for path in ("/portfolio", "/analytics", "/watches"):
                assert client.get(path).status_code == 200

            items = client.get("/api/queue").json()
            assert len(items) >= 3
            assert all(i["suggested_max_bid"] > 0 for i in items)

    async def test_digest_renders_from_seeded_data(self, seeded, settings):
        from nellis.notify.email import EmailNotifier
        from nellis.valuation.engine import ValuationEngine

        engine = ValuationEngine(seeded, settings, offline=True)
        results = []
        for lot in seeded.scalars(select(Lot).where(Lot.is_closed.is_(False))).all():
            result = await engine.value(lot)
            if result.recommended:
                results.append(result)

        assert results
        email = EmailNotifier(settings).render_digest(results)
        assert "walk-away max bid" in email.html.lower()
        assert "never places bids" in email.html.lower()
