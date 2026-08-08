"""End-to-end engine, exposure, matching and email-rendering tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nellis.models import (
    Commitment,
    CommitmentStatus,
    Confidence,
    Lot,
    Watch,
)
from nellis.search.matcher import match_reasons, matches
from nellis.valuation.comps.base import CompPoint, CompsProvider, ItemQuery
from nellis.valuation.engine import ValuationEngine
from nellis.valuation.exposure import check_exposure, current_exposure, rank_by_capital_efficiency

NOW = datetime.now(UTC)


class FakeComps(CompsProvider):
    """Deterministic comps so valuation assertions are exact."""

    name = "nellis"

    def __init__(self, prices: list[float], title: str = "DeWalt DCD999B Hammer Drill"):
        self.prices = prices
        self.title = title

    async def fetch(self, query: ItemQuery, *, limit: int = 40) -> list[CompPoint]:
        return [
            CompPoint(
                price=p, source="nellis", title=self.title,
                is_sold=True, sold_at=NOW - timedelta(days=7), similarity=1.0,
            )
            for p in self.prices
        ]


def make_engine(session, prices, **kwargs):
    from nellis.config import get_settings
    from nellis.valuation.comps import CompsService

    settings = get_settings()
    for key, value in kwargs.items():
        setattr(settings, key, value)
    service = CompsService(session, settings, providers=[FakeComps(prices)])
    return ValuationEngine(session, settings, comps_service=service, offline=True)


def add_lot(session, **overrides) -> Lot:
    defaults = dict(
        nellis_id="1001",
        url="https://www.nellisauction.com/p/1001",
        title="DeWalt DCD999B 20V MAX XR Hammer Drill (Tool Only)",
        brand="DeWalt",
        model="DCD999B",
        category="Tools",
        condition_name="Open Box",
        retail_price=299.0,
        current_bid=41.0,
        close_at=NOW + timedelta(hours=3),
        buyers_premium_rate=0.15,
    )
    defaults.update(overrides)
    lot = Lot(**defaults)
    session.add(lot)
    session.flush()
    return lot


class TestEngineEndToEnd:
    @pytest.mark.asyncio
    async def test_good_deal_is_recommended_with_a_usable_bid(self, db):
        lot = add_lot(db)
        engine = make_engine(db, [180, 185, 190, 175, 188, 182, 179, 186])
        result = await engine.value(lot)

        assert result.recommended
        assert result.walk_away_max_bid > lot.current_bid
        assert result.projected_profit > 0
        assert result.confidence.level in (Confidence.HIGH, Confidence.MEDIUM)
        # Sanity: the bid must be well under the comp value after all costs.
        assert result.walk_away_max_bid < result.comp_set.value

    @pytest.mark.asyncio
    async def test_bidding_the_recommended_max_actually_hits_the_margin(self, db):
        """The invariant that matters, verified through the whole pipeline."""
        lot = add_lot(db)
        engine = make_engine(db, [180] * 10, target_margin=0.40)
        result = await engine.value(lot)

        from nellis.valuation.cost import margin_at

        achieved = margin_at(
            result.walk_away_max_bid, result.net_resale, bp_rate=0.15, tax_rate=0.06625
        )
        assert achieved >= result.effective_margin - 1e-9

    @pytest.mark.asyncio
    async def test_overpriced_lot_is_rejected(self, db):
        lot = add_lot(db, current_bid=250.0)
        engine = make_engine(db, [180] * 8)
        result = await engine.value(lot)
        assert not result.recommended
        assert "past your max" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_no_comps_falls_back_and_warns(self, db):
        lot = add_lot(db)
        engine = make_engine(db, [])
        result = await engine.value(lot)
        assert result.confidence.level == Confidence.NONE
        assert not result.recommended
        assert any("no comps" in w.lower() for w in result.warnings)

    @pytest.mark.asyncio
    async def test_valuation_is_persisted(self, db):
        from sqlalchemy import select

        from nellis.models import Valuation

        lot = add_lot(db)
        engine = make_engine(db, [180] * 8)
        await engine.value(lot)
        stored = db.scalars(select(Valuation).where(Valuation.lot_id == lot.id)).all()
        assert len(stored) == 1
        assert stored[0].walk_away_max_bid > 0

    @pytest.mark.asyncio
    async def test_low_confidence_produces_a_lower_bid(self, db):
        lot_a = add_lot(db, nellis_id="A")
        lot_b = add_lot(db, nellis_id="B")
        rich = await make_engine(db, [180] * 12).value(lot_a)
        thin = await make_engine(db, [120, 240]).value(lot_b)
        assert thin.walk_away_max_bid < rich.walk_away_max_bid


class TestRepairIntegration:
    @pytest.mark.asyncio
    async def test_missing_part_is_detected_priced_and_valued(self, db):
        lot = add_lot(
            db,
            nellis_id="2002",
            title="Ninja BL660 Professional Blender",
            brand="Ninja",
            model="BL660",
            category="Kitchen",
            condition_name="Damaged",
            condition_notes="Missing lid. Pitcher and base included.",
            retail_price=129.99,
            current_bid=12.0,
        )
        engine = make_engine(db, [95, 100, 92, 98, 105, 97])
        result = await engine.value(lot)

        assert "lid" in result.condition.missing_parts
        assert result.repair is not None
        assert result.repair.parts_cost > 0
        assert result.repair.is_worth_repairing
        assert result.repair.easy_fix_score is not None

    @pytest.mark.asyncio
    async def test_fatal_damage_is_never_recommended(self, db):
        lot = add_lot(
            db,
            nellis_id="3003",
            title="Samsung Refrigerator",
            condition_name="Damaged",
            condition_notes="Compressor is shot. Does not power on.",
            current_bid=5.0,
        )
        engine = make_engine(db, [800] * 8)
        result = await engine.value(lot)
        assert result.condition.is_fatal
        assert not result.recommended


class TestExposure:
    def _commit(self, db, lot, amount, category="tools"):
        db.add(
            Commitment(
                lot_id=lot.id, max_bid=amount * 0.8, landed_at_max=amount,
                category=category, status=CommitmentStatus.LEADING,
            )
        )
        db.flush()

    def test_exposure_sums_open_commitments(self, db):
        a = add_lot(db, nellis_id="A")
        b = add_lot(db, nellis_id="B")
        self._commit(db, a, 200.0)
        self._commit(db, b, 150.0)
        state = current_exposure(db)
        assert state.total == pytest.approx(350.0)
        assert state.lot_count == 2

    def test_outbid_still_counts_as_exposure(self, db):
        """You can be re-outbid back into the lead at any moment."""
        lot = add_lot(db, nellis_id="A")
        db.add(
            Commitment(lot_id=lot.id, max_bid=100, landed_at_max=123.0,
                       status=CommitmentStatus.OUTBID)
        )
        db.flush()
        assert current_exposure(db).total == pytest.approx(123.0)

    def test_total_cap_blocks_the_bid(self, db):
        a = add_lot(db, nellis_id="A")
        target = add_lot(db, nellis_id="B")
        self._commit(db, a, 900.0)
        decision = check_exposure(
            db, target, 200.0, max_total=1000.0, max_lots=25, max_per_category=5000.0
        )
        assert not decision.allowed
        assert "exposure" in decision.reason.lower()

    def test_per_category_cap_blocks_the_bid(self, db):
        a = add_lot(db, nellis_id="A")
        target = add_lot(db, nellis_id="B", category="Tools")
        self._commit(db, a, 400.0, category="tools")
        decision = check_exposure(
            db, target, 200.0, max_total=99999.0, max_lots=25, max_per_category=500.0
        )
        assert not decision.allowed
        assert "category" in decision.reason.lower()

    def test_rebidding_same_lot_replaces_rather_than_stacks(self, db):
        lot = add_lot(db, nellis_id="A")
        self._commit(db, lot, 900.0)
        decision = check_exposure(
            db, lot, 950.0, max_total=1000.0, max_lots=25, max_per_category=5000.0
        )
        assert decision.allowed
        assert decision.worst_case_total == pytest.approx(950.0)

    def test_overlapping_closes_are_flagged(self, db):
        a = add_lot(db, nellis_id="A", close_at=NOW + timedelta(hours=3))
        target = add_lot(db, nellis_id="B", close_at=NOW + timedelta(hours=3, minutes=10))
        self._commit(db, a, 100.0)
        decision = check_exposure(
            db, target, 100.0, max_total=99999.0, max_lots=25,
            max_per_category=99999.0, overlap_window_minutes=30,
        )
        assert decision.allowed
        assert "A" in decision.overlapping_lots

    def test_capital_efficiency_ranking_prefers_profit_per_dollar(self):
        # (profit, exposure): the $50-on-$100 deal beats $80-on-$400.
        order = rank_by_capital_efficiency([(80.0, 400.0), (50.0, 100.0)])
        assert order[0] == 1

    @pytest.mark.asyncio
    async def test_engine_blocks_when_cap_would_break(self, db):
        held = add_lot(db, nellis_id="HELD")
        db.add(
            Commitment(lot_id=held.id, max_bid=1000, landed_at_max=1490.0,
                       status=CommitmentStatus.LEADING, category="tools")
        )
        db.flush()
        target = add_lot(db, nellis_id="TARGET")
        engine = make_engine(db, [180] * 10, max_open_exposure=1500.0)
        result = await engine.value(target)
        assert not result.recommended
        assert result.exposure is not None and not result.exposure.allowed


class TestWatchMatching:
    def _watch(self, **kwargs) -> Watch:
        defaults = dict(name="w", enabled=True)
        defaults.update(kwargs)
        return Watch(**defaults)

    def test_keyword_matches(self, db):
        lot = add_lot(db)
        assert matches(lot, self._watch(keywords="dewalt, milwaukee"))
        assert not matches(lot, self._watch(keywords="ryobi"))

    def test_exclude_term_wins(self, db):
        lot = add_lot(db)
        reasons = match_reasons(lot, self._watch(keywords="dewalt", exclude_terms="tool only"))
        assert any("excluded" in r for r in reasons)

    def test_require_all_terms(self, db):
        lot = add_lot(db)
        assert matches(lot, self._watch(require_all="dewalt, hammer"))
        assert not matches(lot, self._watch(require_all="dewalt, sawzall"))

    def test_price_and_discount_filters(self, db):
        lot = add_lot(db)  # retail 299, bid 41 -> ~86% discount
        assert matches(lot, self._watch(min_retail=100, max_current_bid=100))
        assert not matches(lot, self._watch(min_retail=500))
        assert matches(lot, self._watch(min_discount_pct=0.5))
        assert not matches(lot, self._watch(min_discount_pct=0.95))

    def test_closes_within_filter(self, db):
        lot = add_lot(db, close_at=NOW + timedelta(minutes=45))
        assert matches(lot, self._watch(closes_within_minutes=60))
        assert not matches(lot, self._watch(closes_within_minutes=15))

    def test_exclude_damaged(self, db):
        lot = add_lot(db, condition_name="Damaged", condition_notes="Missing lid")
        assert not matches(lot, self._watch(include_damaged=False))
        assert matches(lot, self._watch(include_damaged=True))

    def test_failure_reasons_are_explanatory(self, db):
        lot = add_lot(db)
        reasons = match_reasons(lot, self._watch(keywords="ryobi", min_retail=9999))
        assert len(reasons) == 2


class TestEmailRendering:
    @pytest.mark.asyncio
    async def test_digest_renders_with_the_max_bid_visible(self, db):
        from nellis.notify.email import EmailNotifier

        lot = add_lot(db)
        result = await make_engine(db, [180] * 10).value(lot)
        email = EmailNotifier().render_digest([result])

        assert "walk-away max bid" in email.html.lower()
        assert f"{result.walk_away_max_bid:.2f}" in email.html
        assert lot.url in email.html
        assert "never places bids" in email.html.lower()
        assert lot.title in email.text

    @pytest.mark.asyncio
    async def test_repair_play_is_surfaced(self, db):
        from nellis.notify.email import EmailNotifier

        lot = add_lot(
            db, nellis_id="2002", title="Ninja BL660 Blender", brand="Ninja",
            condition_name="Damaged", condition_notes="Missing lid.",
            retail_price=129.99, current_bid=8.0,
        )
        result = await make_engine(db, [95, 100, 92, 98, 105, 97]).value(lot)
        html = EmailNotifier().render_digest([result]).html
        assert "Repair play" in html

    def test_empty_digest_still_renders(self, db):
        from nellis.notify.email import EmailNotifier

        email = EmailNotifier().render_digest([])
        assert "no qualifying deals" in email.subject.lower()

    @pytest.mark.asyncio
    async def test_dedupe_suppresses_a_repeat_digest(self, db):
        from nellis.notify import dedupe

        lot = add_lot(db)
        result = await make_engine(db, [180] * 10).value(lot)
        key = dedupe.deal_key(lot.nellis_id, result.walk_away_max_bid)
        assert not dedupe.already_sent(db, key)
        dedupe.record_sent(db, "deal", key, lot_id=lot.id)
        assert dedupe.already_sent(db, key)
