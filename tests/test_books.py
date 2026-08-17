"""Bookkeeping tests: categorisation and savings.

The savings tests carry most of the weight. These figures go into MSS company
expense records, so the important property isn't that a number is produced —
it's that an indefensible number is never presented as a defensible one.
"""

from __future__ import annotations

import pytest

from nellis.books.classify import categorise, mss_category_label, purpose_label
from nellis.books.savings import (
    UNVERIFIED_RETAIL_HAIRCUT,
    apply_to_item,
    choose_reference,
    compute_savings,
    total_savings,
)
from nellis.models import MssCategory, PortfolioItem, Purpose, ValueBasis

BP = 0.15
TAX = 0.06625


class TestCategorisation:
    @pytest.mark.parametrize("title,expected", [
        ("Melissa & Doug Wooden Puzzle Set", MssCategory.INVENTORY_TOYS),
        ("LEGO Classic Creative Bricks 500pc", MssCategory.INVENTORY_TOYS),
        ("Fisher-Price Laugh & Learn Activity Table", MssCategory.INVENTORY_TOYS),
        ("Magna-Tiles 100 Piece Set", MssCategory.INVENTORY_TOYS),
        ("5-Tier Metal Storage Shelving Unit", MssCategory.FIXTURES),
        ("Sterilite 20qt Storage Bins 6-Pack", MssCategory.FIXTURES),
        ("Brother Label Maker", MssCategory.OFFICE),
        ("HP Printer Ink Cartridge", MssCategory.OFFICE),
        ("Clorox Disinfecting Wipes 6-Pack", MssCategory.SUPPLIES),
        ("Avery Labels 1000ct", MssCategory.SUPPLIES),
    ])
    def test_mss_subcategories(self, title, expected):
        result = categorise(title)
        assert result.purpose == Purpose.MSS_EXPENSE
        assert result.mss_category == expected

    def test_child_wording_alone_reads_as_inventory(self):
        result = categorise("Wooden Activity Cube, Ages 2-5")
        assert result.purpose == Purpose.MSS_EXPENSE
        assert result.mss_category == MssCategory.INVENTORY_TOYS

    def test_bulk_toys_are_stock_not_a_flip(self):
        """A case of puzzles is exactly what a lending library buys."""
        result = categorise("Case of 24 Assorted Kids Puzzles")
        assert result.purpose == Purpose.MSS_EXPENSE
        assert result.mss_category == MssCategory.INVENTORY_TOYS

    def test_want_match_reads_as_personal(self):
        result = categorise("Suncast Storage Shed", matched_want=True)
        assert result.purpose == Purpose.PERSONAL

    def test_projected_profit_reads_as_resell(self):
        result = categorise("DeWalt DCD999B Hammer Drill", projected_profit=45.0)
        assert result.purpose == Purpose.RESELL

    def test_unknown_is_left_for_a_human(self):
        """Guessing wrong in a company expense record is worse than not guessing."""
        result = categorise("Miscellaneous Item 47")
        assert result.purpose == Purpose.UNDECIDED
        assert result.confidence == 0.0
        assert "by hand" in result.reason

    def test_word_boundaries_are_respected(self):
        """'bag' must not fire on 'baggage'; this bug already bit once elsewhere."""
        result = categorise("Samsonite Baggage Set")
        assert result.mss_category != MssCategory.SUPPLIES

    def test_labels_are_human_readable(self):
        assert purpose_label(Purpose.MSS_EXPENSE) == "MSS Company Expense"
        assert mss_category_label(MssCategory.INVENTORY_TOYS) == "Inventory (toys)"


class TestReferenceSelection:
    def test_comps_are_preferred_when_available(self):
        value, basis, _ = choose_reference(retail_price=200.0, comp_value=150.0, comp_count=8)
        assert value == 150.0
        assert basis == ValueBasis.RETAIL_VERIFIED

    def test_inflated_retail_is_rejected_in_favour_of_comps(self):
        """The core protection: 'retail $4000' on a $150 item is not a baseline."""
        value, basis, note = choose_reference(
            retail_price=4000.0, comp_value=150.0, comp_count=9
        )
        assert value == 150.0
        assert basis == ValueBasis.COMPS
        assert "inflated" in note

    def test_thin_comps_do_not_count_as_comps(self):
        _, basis, _ = choose_reference(retail_price=200.0, comp_value=150.0, comp_count=1)
        assert basis == ValueBasis.RETAIL_STATED

    def test_unverified_retail_is_haircut(self):
        value, basis, note = choose_reference(retail_price=100.0, comp_value=None)
        assert basis == ValueBasis.RETAIL_STATED
        assert value == pytest.approx(100.0 * UNVERIFIED_RETAIL_HAIRCUT)
        assert "unverified" in note

    def test_no_evidence_means_no_claim(self):
        value, basis, note = choose_reference(retail_price=None, comp_value=None)
        assert value is None
        assert basis == ValueBasis.NONE
        assert "can't be claimed" in note


class TestSavingsMath:
    def test_savings_are_measured_against_full_landed_cost(self):
        """Comparing retail to the hammer price alone overstates every saving ~23%."""
        result = compute_savings(
            hammer_price=100.0, retail_price=None, comp_value=300.0, comp_count=10,
            bp_rate=BP, tax_rate=TAX,
        )
        assert result.landed_cost == pytest.approx(122.62, abs=0.02)
        assert result.savings == pytest.approx(300.0 - 122.62, abs=0.02)

    def test_repair_spend_reduces_savings(self):
        without = compute_savings(
            hammer_price=50.0, retail_price=None, comp_value=200.0, comp_count=5
        )
        with_repair = compute_savings(
            hammer_price=50.0, retail_price=None, comp_value=200.0, comp_count=5,
            repair_spend=30.0,
        )
        assert with_repair.savings == pytest.approx(without.savings - 30.0)

    def test_a_bad_buy_shows_negative_savings(self):
        """Overpaying must show as negative, not be floored at zero."""
        result = compute_savings(
            hammer_price=300.0, retail_price=None, comp_value=120.0, comp_count=6
        )
        assert result.savings < 0

    def test_unpriceable_purchase_claims_nothing(self):
        result = compute_savings(hammer_price=40.0, retail_price=None, comp_value=None)
        assert result.savings is None
        assert not result.is_defensible

    def test_defensibility_is_explicit(self):
        comps = compute_savings(
            hammer_price=40.0, retail_price=None, comp_value=200.0, comp_count=6
        )
        stated = compute_savings(hammer_price=40.0, retail_price=200.0, comp_value=None)
        assert comps.is_defensible
        assert not stated.is_defensible

    def test_apply_persists_onto_the_row(self):
        item = PortfolioItem(lot_id=1, hammer_price=50.0, landed_cost=61.31)
        result = apply_to_item(item, retail_price=None, comp_value=200.0, comp_count=7)
        assert item.savings == result.savings
        assert item.value_basis == ValueBasis.COMPS
        assert item.reference_value == 200.0


class TestSavingsTotals:
    def _item(self, savings, basis, landed=100.0):
        return PortfolioItem(
            lot_id=1, hammer_price=80.0, landed_cost=landed,
            savings=savings, value_basis=basis,
        )

    def test_verified_and_unverified_are_kept_apart(self):
        """A mixed total is only as trustworthy as its weakest input."""
        totals = total_savings([
            self._item(100.0, ValueBasis.COMPS),
            self._item(50.0, ValueBasis.RETAIL_VERIFIED),
            self._item(400.0, ValueBasis.RETAIL_STATED),
        ])
        assert totals.headline_savings == pytest.approx(150.0)
        assert totals.unverified_savings == pytest.approx(400.0)
        assert totals.total_savings_including_unverified == pytest.approx(550.0)

    def test_headline_leads_with_the_defensible_number(self):
        totals = total_savings([self._item(999.0, ValueBasis.RETAIL_STATED)])
        assert totals.headline_savings == 0.0, "unverified savings must not headline"

    def test_unpriced_items_are_counted_not_guessed(self):
        totals = total_savings([self._item(None, ValueBasis.NONE)])
        assert totals.unpriced_count == 1
        assert totals.headline_savings == 0.0

    def test_effective_discount(self):
        totals = total_savings([self._item(100.0, ValueBasis.COMPS, landed=100.0)])
        assert totals.effective_discount == pytest.approx(0.5)

    def test_empty_portfolio_is_safe(self):
        totals = total_savings([])
        assert totals.item_count == 0
        assert totals.effective_discount is None
