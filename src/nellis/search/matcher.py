"""Watch matching: does this lot satisfy this saved search?

Nellis' own search is applied server-side where possible (it saves requests),
but every filter is re-checked locally so results are consistent regardless of
what the site supports. Local matching is also what makes filters like
"min discount %" and "closes within N minutes" work at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..models import Lot, Watch


def _terms(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


def _haystack(lot: Lot) -> str:
    return " ".join(
        filter(
            None,
            [
                lot.title,
                lot.description,
                lot.brand,
                lot.model,
                lot.category,
                lot.condition_name,
                lot.condition_notes,
            ],
        )
    ).lower()


def _minutes_to_close(lot: Lot, now: datetime) -> float | None:
    if lot.close_at is None:
        return None
    close_at = lot.close_at
    if close_at.tzinfo is None:
        close_at = close_at.replace(tzinfo=UTC)
    return (close_at - now).total_seconds() / 60.0


def match_reasons(lot: Lot, watch: Watch, now: datetime | None = None) -> list[str]:
    """Return the list of failed conditions. Empty list means the lot matches.

    Returning *why* something failed (rather than a bare bool) is what makes
    watches debuggable when they unexpectedly return nothing.
    """
    now = now or datetime.now(UTC)
    failures: list[str] = []
    haystack = _haystack(lot)

    keywords = _terms(watch.keywords)
    if keywords and not any(k in haystack for k in keywords):
        failures.append("no keyword matched")

    for term in _terms(watch.require_all):
        if term not in haystack:
            failures.append(f"missing required term '{term}'")

    for term in _terms(watch.exclude_terms):
        if term in haystack:
            failures.append(f"excluded term '{term}' present")

    categories = _terms(watch.categories)
    if categories:
        category = (lot.category or "").lower()
        if not any(c in category for c in categories):
            failures.append("category mismatch")

    conditions = _terms(watch.conditions)
    if conditions:
        condition = (lot.condition_name or "").lower()
        if not any(c in condition for c in conditions):
            failures.append("condition mismatch")

    locations = _terms(watch.locations)
    if locations:
        location = (lot.location or "").lower()
        if not any(loc in location for loc in locations):
            failures.append("location mismatch")

    brands = _terms(watch.brands)
    if brands:
        brand = (lot.brand or "").lower()
        title = (lot.title or "").lower()
        if not any(b in brand or b in title for b in brands):
            failures.append("brand mismatch")

    retail = lot.retail_price
    if watch.min_retail is not None and (retail is None or retail < watch.min_retail):
        failures.append(f"retail below ${watch.min_retail:,.0f}")
    if watch.max_retail is not None and (retail is None or retail > watch.max_retail):
        failures.append(f"retail above ${watch.max_retail:,.0f}")

    bid = lot.current_bid or 0.0
    if watch.min_current_bid is not None and bid < watch.min_current_bid:
        failures.append("current bid too low")
    if watch.max_current_bid is not None and bid > watch.max_current_bid:
        failures.append(f"current bid above ${watch.max_current_bid:,.0f}")

    if watch.min_discount_pct is not None:
        discount = lot.discount_pct
        if discount is None or discount < watch.min_discount_pct:
            failures.append(f"discount under {watch.min_discount_pct:.0%}")

    minutes = _minutes_to_close(lot, now)
    if watch.closes_within_minutes is not None:
        if minutes is None or minutes > watch.closes_within_minutes or minutes < 0:
            failures.append(f"not closing within {watch.closes_within_minutes} min")
    if watch.closes_after_minutes is not None:
        if minutes is None or minutes < watch.closes_after_minutes:
            failures.append(f"closes sooner than {watch.closes_after_minutes} min")

    # Explicit `is False`: a Watch built in Python (not yet flushed) has None
    # here, and None means "use the default", which is to INCLUDE damaged lots.
    # Treating None as falsy would silently suppress the repair-arbitrage lots
    # that are the whole point of scanning damaged inventory.
    if watch.include_damaged is False:
        from ..valuation.condition import analyze_condition

        report = analyze_condition(
            lot.title, lot.condition_name, lot.condition_notes, lot.description
        )
        if report.is_fatal or report.is_incomplete:
            failures.append("damaged/incomplete excluded by watch")

    return failures


def matches(lot: Lot, watch: Watch, now: datetime | None = None) -> bool:
    return not match_reasons(lot, watch, now)
