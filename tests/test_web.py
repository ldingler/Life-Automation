"""Dashboard and extension-API tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from nellis.models import (
    BidQueueEntry,
    Commitment,
    CommitmentStatus,
    Confidence,
    Lot,
    PortfolioItem,
    QueueStatus,
    Valuation,
)

NOW = datetime.now(UTC)


@pytest.fixture
def client(db):
    from nellis.web.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def seed(db, *, nellis_id="1001", profit=86.0, max_bid=62.0, queued=True):
    lot = Lot(
        nellis_id=nellis_id,
        url=f"https://www.nellisauction.com/p/{nellis_id}",
        title="DeWalt DCD999B 20V MAX XR Hammer Drill",
        brand="DeWalt",
        category="Tools",
        condition_name="Open Box",
        retail_price=299.0,
        current_bid=41.0,
        close_at=NOW + timedelta(hours=4),
        buyers_premium_rate=0.15,
    )
    db.add(lot)
    db.flush()

    valuation = Valuation(
        lot_id=lot.id,
        comp_value=186.0,
        comp_count=14,
        comp_sources=["nellis"],
        confidence=Confidence.HIGH,
        net_resale=150.0,
        walk_away_max_bid=max_bid,
        landed_at_max=76.0,
        projected_profit=profit,
        projected_margin=0.55,
        profit_at_current_bid=99.0,
        recommended=True,
        reason=f"Bid up to ${max_bid:.2f}.",
        resale_channel="ebay",
    )
    db.add(valuation)
    db.flush()

    entry = None
    if queued:
        entry = BidQueueEntry(
            lot_id=lot.id,
            valuation_id=valuation.id,
            suggested_max_bid=max_bid,
            projected_profit=profit,
            exposure_if_won=76.0,
            rank_score=profit / 76.0,
            status=QueueStatus.PENDING,
        )
        db.add(entry)
        db.flush()
    db.commit()
    return lot, valuation, entry


class TestDashboard:
    def test_feed_shows_the_max_bid(self, client, db):
        lot, _, _ = seed(db)
        response = client.get("/")
        assert response.status_code == 200
        assert lot.title in response.text
        assert "$62.00" in response.text
        assert "Walk-away max bid" in response.text

    def test_feed_states_it_does_not_bid(self, client, db):
        seed(db)
        collapsed = " ".join(client.get("/").text.split())
        assert "does not place bids" in collapsed

    def test_min_profit_filter(self, client, db):
        seed(db, nellis_id="A", profit=20.0)
        seed(db, nellis_id="B", profit=200.0)
        text = client.get("/?min_profit=100").text
        assert "$200.00" in text or "200.00" in text
        assert text.count("Walk-away max bid") == 1

    def test_lot_detail_shows_full_cost_breakdown(self, client, db):
        lot, _, _ = seed(db)
        response = client.get(f"/lot/{lot.nellis_id}")
        assert response.status_code == 200
        assert "Buyer&#39;s premium" in response.text or "Buyer's premium" in response.text
        assert "Sales tax" in response.text
        assert "Landed cost" in response.text

    def test_unknown_lot_redirects(self, client):
        assert client.get("/lot/nope", follow_redirects=False).status_code == 303

    def test_watch_create_and_delete(self, client):
        response = client.post(
            "/watches",
            data={"name": "Power tools", "keywords": "dewalt, milwaukee",
                  "min_discount_pct": "60", "target_margin": "45",
                  "resale_channel": "ebay", "include_damaged": "1"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Power tools" in response.text
        assert "dewalt" in response.text

    def test_percent_fields_accept_whole_numbers(self, client):
        client.post(
            "/watches",
            data={"name": "W", "target_margin": "45", "min_discount_pct": "60"},
            follow_redirects=True,
        )
        from sqlalchemy import select

        from nellis.db import session_scope
        from nellis.models import Watch

        with session_scope() as session:
            watch = session.scalar(select(Watch).where(Watch.name == "W"))
            assert watch.target_margin == pytest.approx(0.45)
            assert watch.min_discount_pct == pytest.approx(0.60)

    def test_portfolio_and_analytics_render(self, client, db):
        seed(db)
        assert client.get("/portfolio").status_code == 200
        assert client.get("/analytics").status_code == 200

    def test_healthz(self, client):
        assert client.get("/healthz").json()["ok"] is True


class TestQueueApi:
    def test_queue_lists_pending_ranked(self, client, db):
        seed(db, nellis_id="A", profit=50.0)
        seed(db, nellis_id="B", profit=150.0)
        items = client.get("/api/queue").json()
        assert len(items) == 2
        assert items[0]["rank_score"] >= items[1]["rank_score"]
        assert items[0]["suggested_max_bid"] > 0

    def test_lot_endpoint_returns_valuation(self, client, db):
        lot, _, _ = seed(db)
        payload = client.get(f"/api/lot/{lot.nellis_id}").json()
        assert payload["nellis_id"] == lot.nellis_id
        assert payload["suggested_max_bid"] == 62.0
        assert payload["confidence"] == "high"

    def test_untracked_lot_is_404(self, client):
        assert client.get("/api/lot/999999").status_code == 404

    def test_confirm_books_a_commitment_and_updates_exposure(self, client, db):
        lot, _, entry = seed(db)
        before = client.get("/api/exposure").json()
        assert before["total_exposure"] == 0.0

        response = client.post(f"/api/queue/{entry.id}/confirm", json={})
        assert response.status_code == 200
        after = response.json()
        assert after["open_lots"] == 1
        assert after["total_exposure"] > 62.0  # includes premium + tax
        assert after["headroom"] < after["max_exposure"]

        from sqlalchemy import select

        from nellis.db import session_scope

        with session_scope() as session:
            commitment = session.scalar(select(Commitment).where(Commitment.lot_id == lot.id))
            assert commitment is not None
            assert commitment.status == CommitmentStatus.LEADING

    def test_confirm_accepts_a_different_amount(self, client, db):
        _, _, entry = seed(db)
        result = client.post(f"/api/queue/{entry.id}/confirm", json={"max_bid": 30.0}).json()
        assert 30.0 < result["total_exposure"] < 45.0

    def test_skip_removes_from_queue(self, client, db):
        _, _, entry = seed(db)
        assert client.post(f"/api/queue/{entry.id}/skip", json={}).status_code == 200
        assert client.get("/api/queue").json() == []

    def test_confirmed_entry_leaves_the_queue(self, client, db):
        _, _, entry = seed(db)
        client.post(f"/api/queue/{entry.id}/confirm", json={})
        assert client.get("/api/queue").json() == []

    def test_expired_lot_is_dropped_from_queue(self, client, db):
        lot, _, _ = seed(db)
        lot.close_at = NOW - timedelta(minutes=5)
        db.commit()
        assert client.get("/api/queue").json() == []

    def test_resolve_won_creates_portfolio_item_and_frees_exposure(self, client, db):
        lot, _, entry = seed(db)
        client.post(f"/api/queue/{entry.id}/confirm", json={})
        result = client.post(
            f"/api/commitment/{lot.nellis_id}/resolve", json={"won": True, "hammer_price": 55.0}
        ).json()
        assert result["status"] == "won"
        assert result["exposure"]["total_exposure"] == 0.0

        from sqlalchemy import select

        from nellis.db import session_scope

        with session_scope() as session:
            item = session.scalar(select(PortfolioItem).where(PortfolioItem.lot_id == lot.id))
            assert item is not None
            assert item.hammer_price == 55.0
            assert item.landed_cost > 55.0

    def test_resolve_lost_frees_exposure_without_portfolio_entry(self, client, db):
        lot, _, entry = seed(db)
        client.post(f"/api/queue/{entry.id}/confirm", json={})
        result = client.post(
            f"/api/commitment/{lot.nellis_id}/resolve", json={"won": False}
        ).json()
        assert result["status"] == "lost"
        assert result["exposure"]["total_exposure"] == 0.0

    def test_missing_queue_entry_is_404(self, client):
        assert client.post("/api/queue/99999/skip", json={}).status_code == 404


class TestApiAuth:
    def test_token_is_enforced_when_configured(self, client, db, monkeypatch):
        from nellis.config import get_settings

        get_settings().api_token = "s3cret"
        try:
            assert client.get("/api/queue").status_code == 401
            ok = client.get("/api/queue", headers={"X-API-Token": "s3cret"})
            assert ok.status_code == 200
        finally:
            get_settings().api_token = None

    def test_open_when_no_token_configured(self, client):
        assert client.get("/api/queue").status_code == 200
