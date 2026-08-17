"""Personal want-matching: "do I want this?" alongside "can I flip this?"

The resale engine and this one share ingestion, comps and landed-cost maths but
answer different questions and produce different numbers. Keeping them separate
is deliberate — running a shed you need through resale-margin logic would reject
it for having a thin flip margin, which is not why you were looking for a shed.
"""

from .importers import (
    ImportResult,
    history_for,
    import_csv,
    import_plain_list,
    import_records,
    infer_replenishment_for_wants,
    record_signal,
    set_replenishment,
    wanting_signals,
)
from .replenishment import (
    PROFILES,
    Satiation,
    days_until_resurface,
    infer_class,
    satiation_for,
)
from .scoring import WantVerdict, resurfacing_note, score_lot

__all__ = [
    "PROFILES",
    "ImportResult",
    "history_for",
    "import_csv",
    "import_plain_list",
    "import_records",
    "infer_replenishment_for_wants",
    "record_signal",
    "set_replenishment",
    "wanting_signals",
    "Satiation",
    "WantVerdict",
    "days_until_resurface",
    "infer_class",
    "resurfacing_note",
    "satiation_for",
    "score_lot",
]
