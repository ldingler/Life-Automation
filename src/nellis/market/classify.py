"""Turning a search-result card into a priced comparable — or throwing it out.

The extension sends back whatever the page showed: a title, a price, maybe a
rating, maybe a condition badge. It makes no judgements. Everything that decides
*what a card means* lives here, where it can be tested without a browser.

Three outcomes for any card:

  * **Exact** — this is the item on the Nellis lot. Verifies stated retail.
  * **Substitute** — a different product that does the same job. Can set the
    opportunity ceiling, but only if it's actually comparable (see the quality
    gate in `alternatives`).
  * **Rejected** — an accessory, a case, a single replacement blade, a
    multi-pack, or something that just isn't the same category. These are the
    ones that quietly wreck a valuation if you let them through, because a $12
    "compatible battery for DeWalt" is not a stand-in for a $200 DeWalt drill.
"""

from __future__ import annotations

import re

from ..models import PriceKind
from ..normalize import extract_model_numbers, significant_tokens, title_similarity

# Above this, the card is the same product. Model-number agreement drives
# `title_similarity` to 1.0 on its own, so this mostly catches titles that
# repeat the brand and full product name.
EXACT_THRESHOLD = 0.72
# Below this it isn't a stand-in for anything — it's a different product class.
SUBSTITUTE_FLOOR = 0.22

USED_WORDS = re.compile(
    r"\b(used|pre[- ]?owned|preowned|refurb\w*|renewed|open[- ]box|"
    r"scratch\s*(?:and|&)\s*dent|for parts|as[- ]is|second[- ]hand)\b",
    re.I,
)
NEW_WORDS = re.compile(r"\b(brand[- ]new|new in box|nib|sealed|new)\b", re.I)

# Words that mean "this listing is a part of, or for, the thing — not the
# thing". A hit here is disqualifying unless the lot itself is that accessory.
ACCESSORY_WORDS = re.compile(
    r"\b(case|cover|sleeve|skin|bag|strap|mount|bracket|adapter|charger|"
    r"cable|cord|battery|batteries|filter|blade|bit set|screen protector|"
    r"replacement|refill|compatible with|fits|for use with|manual|"
    r"sticker|decal|keychain)\b",
    re.I,
)

# Retail listings love "(3-Pack)". A three-pack price is not a unit price, and
# treating it as one makes everything look expensive.
MULTIPACK_RE = re.compile(r"\b(\d{1,2})\s*[- ]?\s*(?:pack|pk|count|ct|pcs|pieces)\b", re.I)

JUNK_TITLE_RE = re.compile(r"^\s*(sponsored|results|see more|shop )", re.I)


class Rejected(Exception):
    """Not usable as a comparable. Carries the reason for the audit trail."""


def _condition_from(title: str, badge: str | None) -> str:
    haystack = f"{badge or ''} {title}"
    if USED_WORDS.search(haystack):
        return "used"
    return "new"


def _is_accessory(title: str, lot_title: str) -> bool:
    """True when the card is an add-on for the item rather than the item.

    Guarded by the lot's own title: if Logan is bidding on a lot of *batteries*,
    a battery listing is exactly right and must not be rejected.
    """
    hit = ACCESSORY_WORDS.search(title)
    if not hit:
        return False
    word = hit.group(0).lower()
    return word.split()[0] not in lot_title.lower()


def multipack_count(title: str) -> int:
    match = MULTIPACK_RE.search(title)
    if not match:
        return 1
    count = int(match.group(1))
    return count if 2 <= count <= 24 else 1


def classify_candidate(
    candidate: dict,
    *,
    lot_title: str,
    brand: str | None = None,
    model: str | None = None,
) -> dict:
    """Normalize one scraped card into a `MarketPrice`-shaped record.

    Raises `Rejected` when the card should not be stored at all.
    """
    title = (candidate.get("title") or "").strip()
    if not title or JUNK_TITLE_RE.match(title):
        raise Rejected("no usable title")

    try:
        price = float(candidate.get("price"))
    except (TypeError, ValueError):
        raise Rejected("no parseable price") from None
    if price <= 0:
        raise Rejected("non-positive price")

    if _is_accessory(title, lot_title):
        raise Rejected("accessory or replacement part, not the item")

    # Compare against the fullest description of the lot we have. A bare lot
    # title often omits the brand that the retailer's title leads with.
    reference = " ".join(p for p in (brand, model, lot_title) if p)
    similarity = title_similarity(reference, title)

    # A model number in the lot that the card doesn't share is close to
    # conclusive, but only when the lot actually names one.
    lot_models = set(extract_model_numbers(reference))
    card_models = set(extract_model_numbers(title))

    if similarity >= EXACT_THRESHOLD or (lot_models and lot_models & card_models):
        exact = True
    elif similarity < SUBSTITUTE_FLOOR:
        raise Rejected(f"different product class (similarity {similarity:.2f})")
    else:
        exact = False

    # Brand disagreement demotes an otherwise-exact match to a substitute. Two
    # drills with the same spec sheet and different badges are comparable, but
    # one does not verify the other's retail price.
    if exact and brand:
        brand_token = (significant_tokens(brand, 1) or [""])[0]
        if brand_token and brand_token not in title.lower():
            exact = False

    pack = multipack_count(title)
    unit_price = price / pack

    condition = _condition_from(title, candidate.get("condition"))
    if exact:
        kind = PriceKind.EXACT_NEW if condition == "new" else PriceKind.EXACT_USED
    else:
        kind = PriceKind.SUBSTITUTE_NEW if condition == "new" else PriceKind.SUBSTITUTE_USED

    note = None
    if pack > 1:
        note = f"{pack}-pack at ${price:.2f}, unit price used"

    return {
        "kind": kind.value,
        "title": title,
        "price": round(unit_price, 2),
        "shipping": _num(candidate.get("shipping")) or 0.0,
        "url": candidate.get("url"),
        "in_stock": bool(candidate.get("in_stock", True)),
        "rating": _num(candidate.get("rating")),
        "review_count": _int(candidate.get("review_count")),
        "brand": candidate.get("brand") or brand,
        "similarity": round(similarity, 3),
        "source": candidate.get("source") or "browser",
        "note": note,
    }


def classify_all(
    candidates: list[dict],
    *,
    lot_title: str,
    brand: str | None = None,
    model: str | None = None,
    limit: int | None = None,
) -> tuple[list[dict], list[str]]:
    """Classify a page of results. Returns (kept, rejection reasons)."""
    kept: list[dict] = []
    rejects: list[str] = []
    seen_urls: set[str] = set()

    for candidate in candidates:
        try:
            record = classify_candidate(
                candidate, lot_title=lot_title, brand=brand, model=model
            )
        except Rejected as exc:
            title = (candidate.get("title") or "?")[:60]
            rejects.append(f"{title}: {exc}")
            continue
        url = record.get("url")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        kept.append(record)
        if limit and len(kept) >= limit:
            break

    return kept, rejects


def _num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
