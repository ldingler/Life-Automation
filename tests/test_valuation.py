"""Valuation math tests. Pure functions, deterministic, no network."""

from __future__ import annotations

from datetime import UTC

import pytest

from nellis.models import Confidence
from nellis.valuation.comps.base import CompPoint, CompSet, aggregate
from nellis.valuation.condition import analyze_condition, extract_missing_parts
from nellis.valuation.confidence import score_confidence
from nellis.valuation.cost import (
    bid_increment,
    floor_to_increment,
    landed_cost,
    margin_at,
    profit_at,
    walk_away_max_bid,
)
from nellis.valuation.repair.base import PartQuote, assess_repair
from nellis.valuation.resale import get_channel, net_proceeds

BP = 0.15
TAX = 0.06625


class TestLandedCost:
    def test_tax_applies_to_premium_too(self):
        """The commonly-missed step: tax is on (hammer + BP), not hammer alone."""
        cost = landed_cost(100.0, bp_rate=BP, tax_rate=TAX)
        assert cost.buyers_premium == pytest.approx(15.0)
        assert cost.tax == pytest.approx(115.0 * TAX)
        assert cost.total == pytest.approx(115.0 * 1.06625)

    def test_every_dollar_bid_costs_about_1_23(self):
        cost = landed_cost(100.0, bp_rate=BP, tax_rate=TAX)
        assert cost.total / 100.0 == pytest.approx(1.2262, abs=1e-4)

    def test_pickup_is_additive_not_taxed(self):
        cost = landed_cost(100.0, bp_rate=BP, tax_rate=TAX, pickup=10.0)
        assert cost.total == pytest.approx(115.0 * 1.06625 + 10.0)

    def test_breakdown_is_self_consistent(self):
        b = landed_cost(62.0, bp_rate=BP, tax_rate=TAX, pickup=3.0).breakdown()
        assert b["hammer"] + b["buyers_premium"] + b["tax"] + b["pickup"] == pytest.approx(
            b["total"], abs=0.01
        )


class TestBidIncrements:
    def test_increments_scale_with_price(self):
        assert bid_increment(50) == 1.0
        assert bid_increment(250) == 5.0
        assert bid_increment(750) == 10.0
        assert bid_increment(5000) == 25.0

    def test_rounds_down_never_up(self):
        """Rounding up would silently breach the margin the user asked for."""
        assert floor_to_increment(62.9) == 62.0
        assert floor_to_increment(253.7) == 250.0
        assert floor_to_increment(0.4) == 0.0
        assert floor_to_increment(-5) == 0.0


class TestWalkAwaySolver:
    @pytest.mark.parametrize("net", [50, 137.5, 400, 1200])
    @pytest.mark.parametrize("margin", [0.2, 0.4, 0.6])
    def test_bidding_the_max_always_clears_the_target_margin(self, net, margin):
        """The core invariant of the entire system."""
        max_bid = walk_away_max_bid(net, target_margin=margin, bp_rate=BP, tax_rate=TAX)
        achieved = margin_at(max_bid, net, bp_rate=BP, tax_rate=TAX)
        assert achieved >= margin - 1e-9

    def test_one_increment_above_the_max_breaches_the_margin(self):
        """Proves the answer is actually maximal, not just safe."""
        net = 200.0
        margin = 0.4
        max_bid = walk_away_max_bid(net, target_margin=margin, bp_rate=BP, tax_rate=TAX)
        over = max_bid + bid_increment(max_bid)
        assert margin_at(over, net, bp_rate=BP, tax_rate=TAX) < margin

    def test_worked_example(self):
        """$186 comp, eBay, 40% margin -> the number you'd type into Nellis."""
        channel = get_channel("ebay")
        proceeds = net_proceeds(186.0, channel=channel, category="Tools")
        max_bid = walk_away_max_bid(
            proceeds.net, target_margin=0.40, bp_rate=BP, tax_rate=TAX
        )
        assert 50.0 <= max_bid <= 80.0
        assert profit_at(max_bid, proceeds.net, bp_rate=BP, tax_rate=TAX) > 0

    def test_pickup_cost_lowers_the_max_bid(self):
        base = walk_away_max_bid(200, target_margin=0.4, bp_rate=BP, tax_rate=TAX)
        with_pickup = walk_away_max_bid(
            200, target_margin=0.4, bp_rate=BP, tax_rate=TAX, pickup=20.0
        )
        assert with_pickup < base

    def test_worthless_item_yields_no_bid(self):
        assert walk_away_max_bid(0, target_margin=0.4, bp_rate=BP, tax_rate=TAX) == 0.0
        assert walk_away_max_bid(-10, target_margin=0.4, bp_rate=BP, tax_rate=TAX) == 0.0

    def test_impossible_margin_yields_no_bid(self):
        assert walk_away_max_bid(100, target_margin=0.99, bp_rate=BP, tax_rate=TAX) == 0.0


class TestResale:
    def test_fees_are_charged_on_shipping_too(self):
        channel = get_channel("ebay")
        proceeds = net_proceeds(100.0, channel=channel, shipping_override=10.0)
        assert proceeds.fees == pytest.approx(110.0 * 0.1325 + 0.40)

    def test_local_channel_has_no_fees_or_shipping(self):
        proceeds = net_proceeds(100.0, channel=get_channel("local"))
        assert proceeds.fees == 0.0
        assert proceeds.shipping == 0.0
        assert proceeds.net == 100.0

    def test_gross_is_not_net(self):
        """The whole point: a $180 comp is not $180 in your pocket."""
        proceeds = net_proceeds(180.0, channel=get_channel("ebay"), category="Tools")
        assert proceeds.net < 150.0

    def test_parts_and_labor_reduce_net(self):
        base = net_proceeds(200.0, channel=get_channel("local"))
        with_repair = net_proceeds(
            200.0, channel=get_channel("local"), parts_cost=20.0, labor_hours=1.0, labor_rate=25.0
        )
        assert with_repair.net == pytest.approx(base.net - 45.0)


class TestConditionDetection:
    def test_extracts_named_missing_part(self):
        assert "lid" in extract_missing_parts("missing lid. pitcher and base included.")

    def test_extracts_multiword_part(self):
        assert "power cord" in extract_missing_parts("no power cord included")

    def test_fatal_damage_is_flagged(self):
        report = analyze_condition("Fridge", "Damaged", "Compressor is shot, does not power on")
        assert report.is_fatal
        assert report.condition_multiplier <= 0.25

    def test_missing_part_is_not_fatal(self):
        report = analyze_condition(
            "Ninja BL660 Blender", "Damaged", "Missing lid. Base and pitcher included."
        )
        assert not report.is_fatal
        assert report.is_incomplete
        assert "lid" in report.missing_parts

    def test_sealed_new_retains_full_value(self):
        report = analyze_condition("Sony Headphones", "Brand New", "Factory sealed")
        assert report.condition_multiplier == 1.0
        assert not report.signals

    def test_untested_discounts_value(self):
        tested = analyze_condition("Drill", "Open Box", "Complete")
        untested = analyze_condition("Drill", "Open Box", "Untested, sold as-is")
        assert untested.condition_multiplier < tested.condition_multiplier

    def test_summary_is_human_readable(self):
        report = analyze_condition("Blender", "Damaged", "Missing lid, untested")
        assert "lid" in report.summary


class TestRepairEconomics:
    def test_cheap_part_big_uplift_is_flagged_easy(self):
        assessment = assess_repair(
            functional_value=120.0,
            condition_multiplier=0.40,
            quotes=[PartQuote(part="lid", price=12.0, source="catalog")],
            unpriced_parts=[],
            is_fatal=False,
            labor_rate=25.0,
        )
        assert assessment.is_worth_repairing
        assert assessment.value_uplift > 0
        assert assessment.easy_fix_score is not None and assessment.easy_fix_score > 2.0

    def test_expensive_part_low_uplift_is_rejected(self):
        assessment = assess_repair(
            functional_value=100.0,
            condition_multiplier=0.85,
            quotes=[PartQuote(part="motor", price=80.0, source="ebay")],
            unpriced_parts=[],
            is_fatal=False,
            labor_rate=25.0,
        )
        assert not assessment.is_worth_repairing

    def test_fatal_damage_skips_repair_path(self):
        assessment = assess_repair(
            functional_value=200.0,
            condition_multiplier=0.25,
            quotes=[],
            unpriced_parts=[],
            is_fatal=True,
        )
        assert not assessment.is_worth_repairing
        assert assessment.value_uplift == 0.0
        assert "scrap" in assessment.notes.lower()

    def test_max_repair_cost_cap_blocks_expensive_fix(self):
        assessment = assess_repair(
            functional_value=300.0,
            condition_multiplier=0.3,
            quotes=[PartQuote(part="pump", price=60.0, source="ebay")],
            unpriced_parts=[],
            is_fatal=False,
            max_repair_cost=25.0,
        )
        assert not assessment.is_worth_repairing

    def test_labor_is_actually_charged(self):
        cheap_labor = assess_repair(
            functional_value=120.0, condition_multiplier=0.4,
            quotes=[PartQuote(part="wheels", price=10.0, source="c")],
            unpriced_parts=[], is_fatal=False, labor_rate=1.0,
        )
        pricey_labor = assess_repair(
            functional_value=120.0, condition_multiplier=0.4,
            quotes=[PartQuote(part="wheels", price=10.0, source="c")],
            unpriced_parts=[], is_fatal=False, labor_rate=200.0,
        )
        assert pricey_labor.value_uplift < cheap_labor.value_uplift


class TestCompsAggregation:
    def _points(self, prices, source="nellis", sold=True):
        from datetime import datetime, timedelta

        now = datetime.now(UTC)
        return [
            CompPoint(
                price=p, source=source, title="DeWalt DCD999B Drill",
                is_sold=sold, sold_at=now - timedelta(days=5),
            )
            for p in prices
        ]

    def test_median_resists_a_wild_outlier(self):
        tight = aggregate(self._points([100, 105, 110, 95, 102]))
        skewed = aggregate(self._points([100, 105, 110, 95, 102, 9000]))
        assert abs(tight.value - skewed.value) < 15

    def test_active_listings_are_discounted_versus_sold(self):
        sold = aggregate(self._points([100] * 6, source="nellis", sold=True))
        active = aggregate(self._points([100] * 6, source="ebay", sold=False))
        assert active.value < sold.value

    def test_empty_input_yields_no_value(self):
        result = aggregate([])
        assert result.value is None and result.count == 0

    def test_dissimilar_titles_are_excluded(self):
        points = self._points([100, 100, 100])
        for p in points:
            p.similarity = 0.1
        assert aggregate(points).count == 0


class TestConfidence:
    def test_no_comps_is_lowest_confidence(self):
        report = score_confidence(CompSet(value=None, count=0))
        assert report.level == Confidence.NONE
        assert report.margin_bump > 0

    def test_many_tight_recent_sold_comps_is_high(self):
        report = score_confidence(
            CompSet(value=100, count=15, sources=["nellis", "ebay_sold"], spread=0.15,
                    median_age_days=10)
        )
        assert report.level == Confidence.HIGH
        assert report.margin_bump == 0.0

    def test_thin_scattered_stale_comps_is_low(self):
        report = score_confidence(
            CompSet(value=100, count=2, sources=["ebay"], spread=1.2, median_age_days=200)
        )
        assert report.level == Confidence.LOW
        assert report.margin_bump > 0

    def test_low_confidence_makes_the_bid_more_conservative(self):
        """Uncertainty must cost money, not be ignored."""
        high = score_confidence(
            CompSet(value=100, count=15, sources=["nellis"], spread=0.1, median_age_days=5)
        )
        low = score_confidence(
            CompSet(value=100, count=2, sources=["ebay"], spread=1.5, median_age_days=200)
        )
        net = 200.0
        high_bid = walk_away_max_bid(
            net, target_margin=0.4 + high.margin_bump, bp_rate=BP, tax_rate=TAX
        )
        low_bid = walk_away_max_bid(
            net, target_margin=0.4 + low.margin_bump, bp_rate=BP, tax_rate=TAX
        )
        assert low_bid < high_bid
