"""Market-verification tests.

The headline case is Logan's widget: retail $500, bid at $350, but an equally
good widget sells new elsewhere for $300. That must be refused outright, and it's
asserted first because it's the whole reason this module exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nellis.market.alternatives import (
    MIN_SUBSTITUTE_RATING,
    MIN_SUBSTITUTE_REVIEWS,
    NEW_ALTERNATIVE_MARGIN,
    apply_ceiling,
    build_market_view,
    verify_retail,
)
from nellis.models import MarketPrice, PriceKind, VerificationStatus

NOW = datetime.now(UTC)


def price(
    amount, kind=PriceKind.EXACT_NEW, *, source="amazon", rating=4.6,
    reviews=1200, days_ago=1, in_stock=True, title="Widget A",
) -> MarketPrice:
    return MarketPrice(
        query_key="k", kind=kind, source=source, title=title, price=amount,
        rating=rating, review_count=reviews, in_stock=in_stock,
        observed_at=NOW - timedelta(days=days_ago),
    )


class TestTheWidgetRule:
    """Logan's example, asserted directly."""

    def test_do_not_bid_when_an_equal_widget_is_cheaper_new(self):
        view = build_market_view(
            stated_retail=500.0,
            prices=[
                price(500.0, PriceKind.EXACT_NEW),
                price(300.0, PriceKind.SUBSTITUTE_NEW, title="Widget B", rating=4.5, reviews=800),
            ],
            now=NOW,
        )
        # Bid $350 -> ~$429 landed once premium and tax are added.
        decision = apply_ceiling(view, landed_at_bid=429.17)
        assert not decision.allowed
        assert "Don't bid" in decision.reason
        assert "$300" in decision.reason

    def test_stated_retail_being_accurate_does_not_save_it(self):
        """Retail can be perfectly honest and the bid still be foolish."""
        view = build_market_view(
            stated_retail=500.0,
            prices=[
                price(495.0, PriceKind.EXACT_NEW),
                price(300.0, PriceKind.SUBSTITUTE_NEW, title="Widget B"),
            ],
            now=NOW,
        )
        assert view.verification == VerificationStatus.VERIFIED
        assert not apply_ceiling(view, landed_at_bid=380.0).allowed

    def test_a_bid_well_under_the_alternative_is_allowed(self):
        view = build_market_view(
            stated_retail=500.0,
            prices=[price(300.0, PriceKind.SUBSTITUTE_NEW, title="Widget B")],
            now=NOW,
        )
        decision = apply_ceiling(view, landed_at_bid=150.0)
        assert decision.allowed
        assert decision.ceiling == pytest.approx(300.0 * (1 - NEW_ALTERNATIVE_MARGIN))

    def test_marginal_saving_is_refused(self):
        """$10 under a new one isn't worth a pickup trip and no warranty."""
        view = build_market_view(
            stated_retail=500.0,
            prices=[price(300.0, PriceKind.SUBSTITUTE_NEW, title="Widget B")],
            now=NOW,
        )
        decision = apply_ceiling(view, landed_at_bid=290.0)
        assert not decision.allowed
        assert "not worth the pickup" in decision.reason


class TestQualityGate:
    """Without this, the cheapest junk on the internet vetoes every good lot."""

    def test_low_rated_substitute_is_ignored(self):
        view = build_market_view(
            stated_retail=500.0,
            prices=[
                price(495.0, PriceKind.EXACT_NEW),
                price(19.0, PriceKind.SUBSTITUTE_NEW, title="No-name", rating=2.7, reviews=40),
            ],
            now=NOW,
        )
        assert view.rejected_for_quality == 1
        assert view.best_alternative.price == 495.0

    def test_thinly_reviewed_substitute_is_ignored(self):
        view = build_market_view(
            stated_retail=500.0,
            prices=[
                price(495.0, PriceKind.EXACT_NEW),
                price(60.0, PriceKind.SUBSTITUTE_NEW, title="Unknown", rating=4.9, reviews=3),
            ],
            now=NOW,
        )
        assert view.rejected_for_quality == 1
        assert view.best_alternative.price == 495.0

    def test_unrated_substitute_cannot_veto(self):
        """An unrated listing can't be quality-checked, so it must not decide."""
        view = build_market_view(
            stated_retail=500.0,
            prices=[
                price(495.0, PriceKind.EXACT_NEW),
                price(30.0, PriceKind.SUBSTITUTE_NEW, rating=None, reviews=None),
            ],
            now=NOW,
        )
        assert view.best_alternative.price == 495.0

    def test_a_genuinely_good_substitute_does_count(self):
        view = build_market_view(
            stated_retail=500.0,
            prices=[
                price(495.0, PriceKind.EXACT_NEW),
                price(280.0, PriceKind.SUBSTITUTE_NEW, rating=4.6, reviews=900),
            ],
            now=NOW,
        )
        assert view.rejected_for_quality == 0
        assert view.best_alternative.price == 280.0

    def test_the_exact_item_is_never_quality_gated(self):
        """It's the thing being bid on; its own rating can't disqualify it."""
        view = build_market_view(
            stated_retail=500.0,
            prices=[price(310.0, PriceKind.EXACT_NEW, rating=3.1, reviews=5)],
            now=NOW,
        )
        assert view.rejected_for_quality == 0
        assert view.best_alternative.price == 310.0


class TestRetailVerification:
    def test_accurate_retail_is_confirmed_not_doubted(self):
        """Retail is right more often than not; the job is to check, not assume."""
        value, status, note = verify_retail(500.0, [price(480.0), price(520.0), price(499.0)])
        assert status == VerificationStatus.VERIFIED
        assert value == pytest.approx(499.0)
        assert "confirmed" in note

    def test_inflated_retail_is_caught(self):
        _, status, note = verify_retail(4000.0, [price(300.0), price(320.0), price(310.0)])
        assert status == VerificationStatus.OVERSTATED
        assert "overstated" in note

    def test_understated_retail_is_flagged_too(self):
        """Under-stated retail matters: the lot may be a better deal than it looks."""
        _, status, _ = verify_retail(100.0, [price(400.0), price(420.0), price(410.0)])
        assert status == VerificationStatus.UNDERSTATED

    def test_nothing_found_means_no_claim(self):
        value, status, note = verify_retail(500.0, [])
        assert value is None
        assert status == VerificationStatus.UNVERIFIED
        assert "unconfirmed" in note

    def test_median_resists_one_clearance_listing(self):
        value, status, _ = verify_retail(
            500.0, [price(480.0), price(500.0), price(510.0), price(9.99)]
        )
        assert value > 400.0
        assert status == VerificationStatus.VERIFIED


class TestFreshnessAndStock:
    def test_stale_prices_are_ignored(self):
        view = build_market_view(
            stated_retail=500.0,
            prices=[price(200.0, PriceKind.SUBSTITUTE_NEW, days_ago=90)],
            now=NOW,
        )
        assert view.best_alternative is None

    def test_out_of_stock_is_not_an_alternative(self):
        """You can't actually buy it, so it can't be the reason not to bid."""
        view = build_market_view(
            stated_retail=500.0,
            prices=[price(200.0, PriceKind.SUBSTITUTE_NEW, in_stock=False)],
            now=NOW,
        )
        assert view.best_alternative is None

    def test_no_data_permits_bidding_with_a_caveat(self):
        view = build_market_view(stated_retail=500.0, prices=[], now=NOW)
        decision = apply_ceiling(view, landed_at_bid=100.0)
        assert decision.allowed
        assert "no verified alternative" in decision.reason


class TestUsedAlternatives:
    def test_used_alternative_needs_no_extra_margin(self):
        """Both options are used, so there's no warranty gap to compensate for."""
        view = build_market_view(
            stated_retail=500.0,
            prices=[price(200.0, PriceKind.SUBSTITUTE_USED, rating=4.4, reviews=300)],
            now=NOW,
        )
        assert view.opportunity_ceiling == pytest.approx(200.0)

    def test_new_alternative_demands_a_margin(self):
        view = build_market_view(
            stated_retail=500.0,
            prices=[price(200.0, PriceKind.SUBSTITUTE_NEW)],
            now=NOW,
        )
        assert view.opportunity_ceiling == pytest.approx(200.0 * (1 - NEW_ALTERNATIVE_MARGIN))

    def test_cheapest_option_wins_regardless_of_kind(self):
        view = build_market_view(
            stated_retail=500.0,
            prices=[
                price(400.0, PriceKind.EXACT_NEW),
                price(180.0, PriceKind.EXACT_USED, rating=4.3, reviews=90),
                price(250.0, PriceKind.SUBSTITUTE_NEW),
            ],
            now=NOW,
        )
        assert view.best_alternative.price == 180.0


class TestReporting:
    def test_view_explains_itself(self):
        view = build_market_view(
            stated_retail=500.0,
            prices=[
                price(495.0, PriceKind.EXACT_NEW),
                price(300.0, PriceKind.SUBSTITUTE_NEW, title="Widget B"),
                price(15.0, PriceKind.SUBSTITUTE_NEW, rating=2.0, reviews=8),
            ],
            now=NOW,
        )
        payload = view.as_dict()
        assert payload["verification"] == "verified"
        assert payload["best_alternative_price"] == 300.0
        assert payload["rejected_for_quality"] == 1
        assert any("quality bar" in n for n in payload["notes"])

    def test_thresholds_are_configurable(self):
        strict = build_market_view(
            stated_retail=500.0,
            prices=[price(100.0, PriceKind.SUBSTITUTE_NEW, rating=4.2, reviews=50)],
            now=NOW, min_rating=4.8, min_reviews=1000,
        )
        assert strict.best_alternative is None

    def test_defaults_are_sane(self):
        assert 3.5 <= MIN_SUBSTITUTE_RATING <= 4.5
        assert MIN_SUBSTITUTE_REVIEWS >= 10
        assert 0.1 <= NEW_ALTERNATIVE_MARGIN <= 0.5


class TestEngineIntegration:
    """The ceiling has to actually cap the bid, not just report a concern."""

    async def _value(self, db, market_records, *, comps, current_bid, retail):
        from datetime import timedelta

        from nellis.config import get_settings
        from nellis.market.lookup import record_prices
        from nellis.models import Lot
        from nellis.normalize import normalize_item_key
        from nellis.valuation.comps import CompsService
        from nellis.valuation.comps.base import CompPoint, CompsProvider, ItemQuery
        from nellis.valuation.engine import ValuationEngine

        lot = Lot(
            nellis_id="55501", url="https://www.nellisauction.com/p/55501",
            title="Acme Widget A Pro", brand="Acme", category="Tools",
            condition_name="Open Box", retail_price=retail, current_bid=current_bid,
            close_at=NOW + timedelta(hours=2), buyers_premium_rate=0.15,
        )
        db.add(lot)
        db.flush()

        record_prices(db, normalize_item_key(lot.title, brand=lot.brand), market_records)
        db.flush()

        class Fake(CompsProvider):
            name = "nellis"

            async def fetch(self, query: ItemQuery, *, limit: int = 40):
                return [
                    CompPoint(price=p, source="nellis", title=lot.title, is_sold=True,
                              sold_at=NOW - timedelta(days=5), similarity=1.0)
                    for p in comps
                ]

        settings = get_settings()
        engine = ValuationEngine(
            db, settings,
            comps_service=CompsService(db, settings, providers=[Fake()]),
            offline=True,
        )
        return await engine.value(lot)

    @pytest.mark.asyncio
    async def test_cheaper_new_equivalent_caps_the_bid(self, db):
        """The widget case, end to end through the real engine."""
        result = await self._value(
            db,
            [{"price": 300.0, "kind": "sub_new", "source": "amazon",
              "title": "Widget B Pro", "rating": 4.5, "review_count": 900}],
            comps=[450.0] * 8, current_bid=350.0, retail=500.0,
        )
        assert not result.recommended
        assert result.market is not None
        assert result.market.best_alternative.price == 300.0
        assert result.walk_away_max_bid == 0.0 or result.landed_at_max <= 300.0

    @pytest.mark.asyncio
    async def test_no_alternative_leaves_the_bid_alone(self, db):
        result = await self._value(
            db, [], comps=[450.0] * 8, current_bid=50.0, retail=500.0
        )
        assert result.walk_away_max_bid > 0
        assert result.ceiling.allowed

    @pytest.mark.asyncio
    async def test_verified_retail_is_recorded(self, db):
        result = await self._value(
            db,
            [{"price": 495.0, "kind": "exact_new", "source": "walmart",
              "title": "Acme Widget A Pro", "rating": 4.4, "review_count": 300}],
            comps=[200.0] * 8, current_bid=20.0, retail=500.0,
        )
        assert result.market.verification.value == "verified"

    @pytest.mark.asyncio
    async def test_junk_substitute_does_not_veto_a_good_lot(self, db):
        result = await self._value(
            db,
            [{"price": 12.0, "kind": "sub_new", "source": "amazon",
              "title": "Generic knockoff", "rating": 2.1, "review_count": 6}],
            comps=[450.0] * 8, current_bid=40.0, retail=500.0,
        )
        assert result.walk_away_max_bid > 0, "a 2-star knockoff must not block a real lot"
