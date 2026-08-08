"""Repair and missing-parts arbitrage.

The thesis: most bidders filter out anything labeled damaged or incomplete, so
those lots clear at a steep discount that is often far larger than the cost of
making the item whole. A blender missing a $12 lid sells for 20% of a complete
one. That gap is the trade.

This module answers three questions per lot:
  1. What is missing or broken?          (valuation.condition)
  2. What does fixing it cost?           (PartsProvider)
  3. Is the value recovered worth it?    (easy_fix_score)
"""

from __future__ import annotations

from .base import PartQuote, PartsProvider, RepairAssessment, assess_repair
from .providers import CatalogPartsProvider, CompositePartsProvider, EbayPartsProvider

__all__ = [
    "CatalogPartsProvider",
    "CompositePartsProvider",
    "EbayPartsProvider",
    "PartQuote",
    "PartsProvider",
    "RepairAssessment",
    "assess_repair",
]
