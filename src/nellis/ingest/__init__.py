"""Ingestion package: read public Nellis listings, politely."""

from __future__ import annotations

from .adapter import AdapterChain, LotRecord, NellisAdapter, SearchFilters
from .browser import BrowserAdapter
from .client import BlockedError, BudgetExhausted, IngestError, PoliteClient, RobotsDisallowed
from .html_parse import HtmlDomAdapter
from .remix_json import EmbeddedJsonAdapter, RemixDataAdapter

__all__ = [
    "AdapterChain",
    "BlockedError",
    "BrowserAdapter",
    "BudgetExhausted",
    "EmbeddedJsonAdapter",
    "HtmlDomAdapter",
    "IngestError",
    "LotRecord",
    "NellisAdapter",
    "PoliteClient",
    "RemixDataAdapter",
    "RobotsDisallowed",
    "SearchFilters",
    "default_chain",
]


def default_chain() -> AdapterChain:
    """Standard strategy order: structured JSON, then DOM, then browser."""
    return AdapterChain(
        [
            EmbeddedJsonAdapter(),
            RemixDataAdapter(),
            HtmlDomAdapter(),
            BrowserAdapter(),
        ]
    )
