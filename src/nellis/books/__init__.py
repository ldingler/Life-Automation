"""Bookkeeping: what a purchase was for, and what it actually saved.

Separate from valuation on purpose. Valuation decides what to bid; this records
what happened afterwards — which category it lands in, whether it sold, and
whether the saving is a number that would survive being questioned.
"""

from .classify import Categorisation, categorise, mss_category_label, purpose_label
from .savings import (
    SavingsResult,
    SavingsTotals,
    apply_to_item,
    choose_reference,
    compute_savings,
    total_savings,
)

__all__ = [
    "Categorisation",
    "SavingsResult",
    "SavingsTotals",
    "apply_to_item",
    "categorise",
    "choose_reference",
    "compute_savings",
    "mss_category_label",
    "purpose_label",
    "total_savings",
]
