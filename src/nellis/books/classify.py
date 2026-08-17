"""What was this purchase for?

Three destinations, and they mean different things downstream:

    MSS_EXPENSE   stock and running costs for Mia's Sharing Shelf, the toy
                  library. Sub-categorised, because that's how it has to appear
                  in the books.
    RESELL        bought to flip. The resale engine already decided it was
                  worth buying; this records the intent.
    PERSONAL      bought because Logan wanted it.

The classifier guesses so that a hundred purchases don't have to be sorted by
hand, but a guess is only ever a starting point: `purpose_locked` means a human
decided, and nothing re-categorises it afterwards. Expense records that silently
re-sort themselves are worse than no records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import MssCategory, Purpose

# ---- Mia's Sharing Shelf: toy-library stock -------------------------------
TOY_TERMS = {
    "toy", "toys", "puzzle", "puzzles", "lego", "duplo", "playset", "play set",
    "board game", "boardgame", "game", "games", "doll", "dollhouse", "figure",
    "action figure", "plush", "stuffed animal", "blocks", "building blocks",
    "magnatiles", "magna-tiles", "train set", "race track", "ride on", "ride-on",
    "tricycle", "scooter", "playhouse", "play kitchen", "sandbox", "slide",
    "swing set", "trampoline", "bouncer", "activity", "montessori", "stem kit",
    "science kit", "craft kit", "art set", "easel", "play mat", "ball pit",
    "rocker", "walker", "push toy", "pull toy", "shape sorter", "stacking",
    "peg board", "busy board", "sensory", "fidget", "kinetic sand", "playdoh",
    "play-doh", "nerf", "hot wheels", "barbie", "fisher-price", "fisher price",
    "little tikes", "step2", "melissa & doug", "melissa and doug",
}

# Consumables and running costs for operating the library.
SUPPLY_TERMS = {
    "sanitizer", "disinfectant", "wipes", "cleaner", "cleaning", "soap",
    "label", "labels", "labeling", "sticker", "stickers", "tag", "tags",
    "ziploc", "zip lock", "storage bag", "bag", "bags", "glove", "gloves",
    "paper towel", "trash bag", "battery", "batteries", "tape", "velcro",
    "laminating", "laminator sheet", "barcode", "rubber band", "twist tie",
}

# Things that make the basement a lending library.
FIXTURE_TERMS = {
    "shelf", "shelves", "shelving", "bookcase", "cabinet", "rack", "racking",
    "bin", "bins", "tote", "totes", "container", "containers", "cart",
    "table", "workbench", "bench", "stool", "chair", "mat", "rug", "flooring",
    "pegboard", "hook", "hooks", "divider", "locker", "cubby", "cubbies",
    "lighting", "light fixture", "shop light", "dehumidifier", "fan",
    "step ladder", "ladder", "hand truck", "dolly", "wagon",
}

OFFICE_TERMS = {
    "printer", "ink", "toner", "paper", "cardstock", "laminator", "binder",
    "folder", "clipboard", "pen", "pens", "marker", "sharpie", "notebook",
    "label maker", "labelmaker", "scanner", "barcode scanner", "laptop",
    "tablet", "ipad", "monitor", "keyboard", "mouse", "router", "receipt",
    "cash box", "card reader", "square reader", "shredder", "desk",
}

# Strong signals that something is stock rather than equipment: age ranges and
# child-directed wording that rarely appear on anything else.
CHILD_HINT_RE = re.compile(
    r"\b(?:ages?\s*\d|\d+\s*[-–]\s*\d+\s*(?:years?|yrs?|months?|mos?)|toddler|preschool|"
    r"kids?|children'?s?|baby|infant|nursery)\b",
    re.IGNORECASE,
)

RESELL_HINT_TERMS = {
    "lot of", "case of", "pallet", "wholesale", "bulk", "assorted",
}


@dataclass
class Categorisation:
    purpose: Purpose
    mss_category: MssCategory | None
    confidence: float
    reason: str

    def as_dict(self) -> dict:
        return {
            "purpose": self.purpose.value,
            "mss_category": self.mss_category.value if self.mss_category else None,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
        }


def _hits(terms: set[str], haystack: str) -> list[str]:
    # Word boundaries, not substrings — "bag" must not match "baggage", and
    # this class of bug already bit the replenishment lexicons once.
    return [t for t in terms if re.search(rf"\b{re.escape(t)}\b", haystack)]


def categorise(
    title: str,
    *,
    category: str | None = None,
    description: str | None = None,
    price: float | None = None,
    projected_profit: float | None = None,
    matched_want: bool = False,
) -> Categorisation:
    """Guess what a purchase was for.

    Order of evidence:
      1. toy-library stock, which is the whole point of MSS and the most
         distinctive vocabulary
      2. other MSS operating categories — fixtures, supplies, office
      3. an explicit want match means it was bought for personal use
      4. a strong resale projection means it was bought to flip
    """
    haystack = " ".join(
        filter(None, [(title or "").lower(), (category or "").lower(), (description or "").lower()])
    )

    toy_hits = _hits(TOY_TERMS, haystack)
    child_signal = bool(CHILD_HINT_RE.search(haystack))
    bulk_signal = bool(_hits(RESELL_HINT_TERMS, haystack))

    # Toy-library stock. Bulk wording alongside toys still reads as inventory —
    # a case of puzzles is exactly what a lending library buys.
    if toy_hits or (child_signal and not _hits(OFFICE_TERMS, haystack)):
        detail = toy_hits[0] if toy_hits else "child-directed wording"
        return Categorisation(
            purpose=Purpose.MSS_EXPENSE,
            mss_category=MssCategory.INVENTORY_TOYS,
            confidence=0.9 if toy_hits else 0.65,
            reason=f"reads as toy-library stock ({detail})",
        )

    for terms, mss_cat, label in (
        (FIXTURE_TERMS, MssCategory.FIXTURES, "storage/room fit-out"),
        (OFFICE_TERMS, MssCategory.OFFICE, "office or admin equipment"),
        (SUPPLY_TERMS, MssCategory.SUPPLIES, "operating supplies"),
    ):
        hits = _hits(terms, haystack)
        if hits:
            return Categorisation(
                purpose=Purpose.MSS_EXPENSE,
                mss_category=mss_cat,
                confidence=0.7,
                reason=f"{label} ({hits[0]})",
            )

    if matched_want:
        return Categorisation(
            purpose=Purpose.PERSONAL, mss_category=None, confidence=0.8,
            reason="matched something on your want list",
        )

    if projected_profit is not None and projected_profit > 0:
        return Categorisation(
            purpose=Purpose.RESELL, mss_category=None, confidence=0.75,
            reason=f"resale engine projected ${projected_profit:,.2f} profit",
        )

    if bulk_signal:
        return Categorisation(
            purpose=Purpose.RESELL, mss_category=None, confidence=0.5,
            reason="bulk/assorted wording suggests a flip",
        )

    return Categorisation(
        purpose=Purpose.UNDECIDED, mss_category=None, confidence=0.0,
        reason="no clear signal — needs categorising by hand",
    )


def mss_category_label(category: MssCategory | None) -> str:
    return {
        MssCategory.INVENTORY_TOYS: "Inventory (toys)",
        MssCategory.SUPPLIES: "Supplies",
        MssCategory.FIXTURES: "Fixtures",
        MssCategory.OFFICE: "Office",
        MssCategory.OTHER: "Other",
    }.get(category, "—")


def purpose_label(purpose: Purpose) -> str:
    return {
        Purpose.MSS_EXPENSE: "MSS Company Expense",
        Purpose.RESELL: "Resell",
        Purpose.PERSONAL: "Personal",
        Purpose.UNDECIDED: "Uncategorised",
    }.get(purpose, purpose.value)
