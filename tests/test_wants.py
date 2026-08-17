"""Want-mode tests.

The satiation cases come straight from how Logan described the problem: buying
screws must not stop screws being shown; buying a shed should quiet sheds for a
while but not forever; buying a microwave should quiet microwaves for a long
time. Those three are the spec, so they're asserted directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nellis.models import DemandSignal, Lot, ReplenishmentClass, SignalSource, WantItem
from nellis.valuation.condition import analyze_condition
from nellis.wants.replenishment import (
    PROFILES,
    days_until_resurface,
    infer_class,
    satiation_for,
)
from nellis.wants.scoring import SUPPRESS_BELOW, score_lot

NOW = datetime.now(UTC)


def purchase(days_ago: float, *, qty: int = 1, price: float = 50.0) -> DemandSignal:
    return DemandSignal(
        source=SignalSource.NELLIS_PURCHASE.value,
        query_key="k", title="thing", quantity=qty, price_paid=price,
        occurred_at=NOW - timedelta(days=days_ago),
    )


def returned(days_ago: float) -> DemandSignal:
    return DemandSignal(
        source=SignalSource.NELLIS_RETURN.value,
        query_key="k", title="thing", quantity=1,
        occurred_at=NOW - timedelta(days=days_ago),
    )


class TestClassInference:
    @pytest.mark.parametrize("title,expected", [
        ("Box of 500 Drywall Screws", ReplenishmentClass.CONSUMABLE),
        ("AA Batteries 48-Pack", ReplenishmentClass.CONSUMABLE),
        ("HEPA Filter Replacement 3-Pack", ReplenishmentClass.CONSUMABLE),
        ("Sterilite 20qt Storage Bin", ReplenishmentClass.STOCKABLE),
        ("25ft Extension Cord", ReplenishmentClass.STOCKABLE),
        ("Suncast 8x10 Resin Storage Shed", ReplenishmentClass.DURABLE_MULTI),
        ("DeWalt Cordless Drill", ReplenishmentClass.DURABLE_MULTI),
        ("Samsung French Door Refrigerator", ReplenishmentClass.DURABLE_SINGLE),
        ("LG Front Load Washer", ReplenishmentClass.DURABLE_SINGLE),
        ("Whirlpool Over-Range Microwave", ReplenishmentClass.DURABLE_SINGLE),
    ])
    def test_words_classify_the_obvious_cases(self, title, expected):
        assert infer_class(title) == expected

    def test_price_decides_when_words_do_not(self):
        assert infer_class("Assorted mystery lot", price=6.0) == ReplenishmentClass.CONSUMABLE
        assert infer_class("Assorted mystery lot", price=40.0) == ReplenishmentClass.STOCKABLE
        assert infer_class("Assorted mystery lot", price=900.0) == ReplenishmentClass.DURABLE_SINGLE

    def test_words_beat_price(self):
        """A cheap pack of screws is still a consumable; an expensive shed isn't single-use."""
        assert infer_class("Drywall Screws 1000ct", price=4.0) == ReplenishmentClass.CONSUMABLE
        assert infer_class("Large Storage Shed", price=800.0) == ReplenishmentClass.DURABLE_MULTI


class TestSatiationSpec:
    """Logan's three examples, asserted as written."""

    def test_screws_are_never_suppressed(self):
        """'Just because I bought screws doesn't mean I won't need more screws.'"""
        heavy_history = [purchase(d) for d in (1, 5, 12, 30)]
        result = satiation_for(heavy_history, ReplenishmentClass.CONSUMABLE, now=NOW)
        assert result.multiplier == 1.0
        assert not result.is_suppressed

    def test_shed_is_quieted_then_returns(self):
        """'Just because I bought a nice shed doesn't mean I don't need another one.'"""
        fresh = satiation_for([purchase(3)], ReplenishmentClass.DURABLE_MULTI, now=NOW)
        assert fresh.is_suppressed, "a shed bought 3 days ago should be quiet"

        later = satiation_for([purchase(400)], ReplenishmentClass.DURABLE_MULTI, now=NOW)
        assert later.multiplier > fresh.multiplier
        assert not later.is_suppressed, "over a year later, a second shed is plausible again"

    def test_microwave_stays_suppressed_far_longer_than_a_shed(self):
        shed = satiation_for([purchase(200)], ReplenishmentClass.DURABLE_MULTI, now=NOW)
        microwave = satiation_for([purchase(200)], ReplenishmentClass.DURABLE_SINGLE, now=NOW)
        assert microwave.multiplier < shed.multiplier

    def test_nothing_is_suppressed_forever(self):
        """A permanent filter would silently hide things you genuinely need again."""
        for cls in ReplenishmentClass:
            ancient = satiation_for([purchase(4000)], cls, now=NOW)
            assert ancient.multiplier > 0.9, f"{cls} never recovered"


class TestSatiationMechanics:
    def test_never_bought_means_full_interest(self):
        result = satiation_for([], ReplenishmentClass.DURABLE_SINGLE, now=NOW)
        assert result.multiplier == 1.0
        assert "never bought" in result.reason

    def test_more_units_suppress_harder(self):
        one = satiation_for([purchase(10)], ReplenishmentClass.DURABLE_MULTI, now=NOW)
        three = satiation_for([purchase(10, qty=3)], ReplenishmentClass.DURABLE_MULTI, now=NOW)
        assert three.multiplier < one.multiplier
        assert three.owned_units == 3

    def test_recovery_is_monotonic(self):
        previous = -1.0
        for days in (0, 30, 90, 180, 365, 730):
            current = satiation_for(
                [purchase(days)], ReplenishmentClass.DURABLE_MULTI, now=NOW
            ).multiplier
            assert current >= previous, "interest must not dip as time passes"
            previous = current

    def test_a_return_suppresses_harder_than_a_purchase(self):
        """Returning something is stronger evidence than buying it."""
        bought = satiation_for([purchase(30)], ReplenishmentClass.DURABLE_MULTI, now=NOW)
        sent_back = satiation_for([returned(30)], ReplenishmentClass.DURABLE_MULTI, now=NOW)
        assert sent_back.multiplier < bought.multiplier
        assert sent_back.returned
        assert "returned" in sent_back.reason

    def test_a_return_outweighs_a_purchase_of_the_same_thing(self):
        mixed = satiation_for(
            [purchase(60), returned(30)], ReplenishmentClass.DURABLE_MULTI, now=NOW
        )
        assert mixed.returned
        assert mixed.multiplier < 0.5

    def test_returns_do_not_suppress_consumables_forever_either(self):
        assert satiation_for(
            [returned(3000)], ReplenishmentClass.CONSUMABLE, now=NOW
        ).multiplier > 0.9

    def test_resurface_estimate_orders_correctly(self):
        consumable = days_until_resurface(ReplenishmentClass.CONSUMABLE)
        stockable = days_until_resurface(ReplenishmentClass.STOCKABLE)
        multi = days_until_resurface(ReplenishmentClass.DURABLE_MULTI)
        single = days_until_resurface(ReplenishmentClass.DURABLE_SINGLE)
        assert consumable == 0.0
        assert stockable < multi < single

    def test_every_class_has_a_profile(self):
        for cls in ReplenishmentClass:
            assert cls in PROFILES
            assert 0.0 < PROFILES[cls].floor <= 1.0


class TestWantScoring:
    def _lot(self, **overrides) -> Lot:
        defaults = dict(
            nellis_id="900001",
            url="https://www.nellisauction.com/p/900001",
            title="Suncast 8x10 Resin Storage Shed",
            category="Outdoor",
            condition_name="Open Box",
            retail_price=1200.0,
            current_bid=90.0,
            buyers_premium_rate=0.15,
        )
        defaults.update(overrides)
        return Lot(**defaults)

    def _want(self, **overrides) -> WantItem:
        defaults = dict(
            label="Storage shed", query_key="t:shed", keywords="shed",
            max_worth_to_me=500.0, target_discount_vs_retail=0.5,
            replenishment=ReplenishmentClass.DURABLE_MULTI, priority=1.0,
            accept_damaged=True, active=True,
        )
        defaults.update(overrides)
        return WantItem(**defaults)

    def _score(self, lot, want, satiation_signals=(), comp_value=None, **kw):
        from nellis.config import get_settings

        cls = want.replenishment if want else ReplenishmentClass.DURABLE_MULTI
        return score_lot(
            lot,
            want=want,
            satiation=satiation_for(list(satiation_signals), cls, now=NOW),
            condition=analyze_condition(
                lot.title, lot.condition_name, lot.condition_notes, lot.description
            ),
            intent_weight=kw.pop("intent_weight", 1.0),
            comp_value=comp_value,
            settings=get_settings(),
            matched_via=kw.pop("matched_via", "want list"),
        )

    def test_wanted_item_produces_a_usable_bid(self):
        verdict = self._score(self._lot(), self._want())
        assert verdict.suggested_max_bid > 0
        assert not verdict.suppressed
        assert verdict.interest_score > SUPPRESS_BELOW

    def test_bid_never_exceeds_what_you_said_it_is_worth(self):
        """The ceiling is what it's worth to YOU, landed — premium and tax included."""
        want = self._want(max_worth_to_me=200.0)
        verdict = self._score(self._lot(), want)
        assert verdict.landed_at_max <= 200.0 + 1e-6

    def test_inflated_retail_is_caught_by_comps(self):
        """Liquidation retail is often fiction; comps must be able to override it."""
        lot = self._lot(retail_price=4000.0)
        verdict = self._score(lot, self._want(max_worth_to_me=None), comp_value=300.0)
        assert any("inflated" in w for w in verdict.warnings)
        assert verdict.suggested_max_bid < 400.0

    def test_recent_purchase_suppresses_the_shed(self):
        verdict = self._score(self._lot(), self._want(), satiation_signals=[purchase(2)])
        assert verdict.suppressed
        assert "shed" in verdict.explanation.lower() or "Hidden" in verdict.explanation

    def test_consumable_is_not_suppressed_by_a_recent_purchase(self):
        lot = self._lot(title="Box of 500 Drywall Screws", retail_price=40.0, current_bid=2.0)
        want = self._want(
            label="Screws", replenishment=ReplenishmentClass.CONSUMABLE, max_worth_to_me=25.0
        )
        verdict = self._score(lot, want, satiation_signals=[purchase(1), purchase(20)])
        assert not verdict.suppressed
        assert verdict.satiation.multiplier == 1.0

    def test_fatal_damage_kills_personal_interest(self):
        lot = self._lot(condition_name="Damaged", condition_notes="Cracked frame, does not power on")
        verdict = self._score(lot, self._want())
        assert verdict.suggested_max_bid == 0.0
        assert verdict.suppressed

    def test_complete_only_want_rejects_missing_parts(self):
        lot = self._lot(condition_name="Damaged", condition_notes="Missing hardware and door")
        verdict = self._score(lot, self._want(accept_damaged=False))
        assert verdict.suggested_max_bid == 0.0

    def test_already_bid_past_your_ceiling_drops_interest(self):
        cheap = self._score(self._lot(current_bid=20.0), self._want(max_worth_to_me=500.0))
        pricey = self._score(self._lot(current_bid=900.0), self._want(max_worth_to_me=500.0))
        assert pricey.interest_score < cheap.interest_score
        assert pricey.suppressed

    def test_unpriceable_lot_is_refused_rather_than_guessed(self):
        lot = self._lot(retail_price=None)
        verdict = self._score(lot, self._want(max_worth_to_me=None), comp_value=None)
        assert verdict.suggested_max_bid == 0.0
        assert "can't price" in verdict.explanation.lower()

    def test_weak_intent_scores_below_strong_intent(self):
        """A saved-for-later item should rank under something on your list."""
        strong = self._score(self._lot(), self._want(), intent_weight=0.95)
        weak = self._score(self._lot(), self._want(), intent_weight=0.30)
        assert weak.interest_score < strong.interest_score

    def test_explanation_names_the_reason(self):
        verdict = self._score(self._lot(), self._want())
        assert "Bid up to" in verdict.explanation
        assert "want list" in verdict.explanation


class TestImporters:
    def test_plain_list_handles_how_lists_are_actually_written(self, db):
        from nellis.wants.importers import import_plain_list

        text = """Shopping List
- 3 boxes of drywall screws
• Storage shed
  chest freezer qty: 1
* AA batteries $12.99
"""
        result = import_plain_list(db, "alexa_list", text)
        assert result.added == 4

        from sqlalchemy import select

        from nellis.models import DemandSignal

        titles = [s.title for s in db.scalars(select(DemandSignal)).all()]
        assert "boxes of drywall screws" in titles
        assert "Storage shed" in titles
        assert any("batteries" in t for t in titles)

        screws = next(s for s in db.scalars(select(DemandSignal)).all() if "screws" in s.title)
        assert screws.quantity == 3, "leading count should become quantity"

    def test_plain_list_creates_wants_with_inferred_classes(self, db):
        from sqlalchemy import select

        from nellis.models import ReplenishmentClass, WantItem
        from nellis.wants.importers import import_plain_list

        import_plain_list(db, "alexa_list", "drywall screws\nchest freezer\n")
        wants = {w.label: w.replenishment for w in db.scalars(select(WantItem)).all()}
        assert wants["drywall screws"] == ReplenishmentClass.CONSUMABLE
        assert wants["chest freezer"] == ReplenishmentClass.DURABLE_SINGLE

    def test_reimporting_does_not_double_count(self, db):
        """A re-imported purchase would wrongly deepen suppression."""
        from nellis.wants.importers import import_records

        rows = [{"title": "Suncast Storage Shed", "price": "220.00", "date": "2026-06-01"}]
        first = import_records(db, "nellis_purchase", rows)
        second = import_records(db, "nellis_purchase", rows)
        assert first.added == 1
        assert second.added == 0 and second.skipped == 1

    def test_purchases_do_not_create_wants(self, db):
        """Buying something is not evidence you're still looking for it."""
        from sqlalchemy import select

        from nellis.models import WantItem
        from nellis.wants.importers import import_records

        import_records(
            db, "nellis_purchase",
            [{"title": "Chest Freezer", "price": "300"}],
            create_wants=True,
        )
        assert db.scalars(select(WantItem)).all() == []

    def test_csv_import_reads_a_history_export(self, db):
        from nellis.wants.importers import import_csv

        csv_text = (
            "title,price,quantity,date,condition\n"
            "DeWalt Drill,88.00,1,2026-05-02,Open Box\n"
            "Drywall Screws 1000ct,9.50,2,2026-06-11,New\n"
        )
        result = import_csv(db, "nellis_purchase", csv_text)
        assert result.added == 2

    def test_history_lookup_feeds_satiation(self, db):
        from nellis.models import ReplenishmentClass
        from nellis.wants.importers import history_for, import_records
        from nellis.wants.replenishment import satiation_for

        import_records(
            db, "nellis_purchase",
            [{"title": "Suncast Storage Shed", "price": "220", "date": "2026-08-01"}],
        )
        key = "t:shed-storage-suncast"
        from nellis.normalize import normalize_item_key

        history = history_for(db, normalize_item_key("Suncast Storage Shed"))
        assert history, f"expected history for shed, key was {key}"
        assert satiation_for(history, ReplenishmentClass.DURABLE_MULTI).is_suppressed

    def test_manual_correction_survives_reinference(self, db):
        """Once corrected by hand, inference must not quietly overwrite it."""
        from sqlalchemy import select

        from nellis.models import ReplenishmentClass, WantItem
        from nellis.wants.importers import (
            import_plain_list,
            infer_replenishment_for_wants,
            set_replenishment,
        )

        import_plain_list(db, "alexa_list", "chest freezer\n")
        want = db.scalar(select(WantItem))
        assert want.replenishment == ReplenishmentClass.DURABLE_SINGLE

        set_replenishment(db, want.id, ReplenishmentClass.DURABLE_MULTI)
        infer_replenishment_for_wants(db)
        db.refresh(want)
        assert want.replenishment == ReplenishmentClass.DURABLE_MULTI
        assert want.replenishment_locked

    def test_a_bad_row_does_not_sink_the_import(self, db):
        from nellis.wants.importers import import_records

        result = import_records(
            db, "amazon_cart",
            [{"title": "Good Item", "price": "10"}, {"title": ""}, {"title": "Another", "price": "x"}],
        )
        assert result.added == 2
        assert result.skipped == 1
