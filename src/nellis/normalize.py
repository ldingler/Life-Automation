"""Item-identity normalization.

Comps are only as good as the matching. A "DeWalt DCD999B 20V MAX XR Hammer
Drill (Tool Only) - Open Box" and a "DEWALT DCD999 20V Hammer Drill" must land
on the same key, while a "DeWalt DCD777" must not.

Strategy, in order of trust:
  1. UPC/GTIN — exact, when present
  2. brand + model number — near-exact; model numbers are the real identity
  3. significant-token signature — fuzzy fallback
"""

from __future__ import annotations

import re
import unicodedata

# Words that carry no identity: condition, packaging, marketing.
STOPWORDS = {
    "a", "an", "and", "the", "with", "for", "of", "in", "to", "by", "or",
    "new", "used", "open", "box", "openbox", "sealed", "refurbished", "refurb",
    "damaged", "broken", "missing", "parts", "repair", "as", "is", "asis",
    "tool", "only", "toolonly", "kit", "set", "pack", "pc", "pcs", "piece",
    "lot", "bundle", "item", "items", "brand", "genuine", "oem", "original",
    "free", "shipping", "sale", "deal", "great", "excellent", "good", "nice",
    "large", "small", "medium", "size", "color", "black", "white", "blue",
    "red", "green", "gray", "grey", "silver", "gold",
}

# A model number: has a digit, is alphanumeric, 3+ chars. "DCD999B", "M18", "RT2100".
MODEL_TOKEN_RE = re.compile(r"^(?=.*\d)[a-z0-9][a-z0-9\-]{2,}$")
PUNCT_RE = re.compile(r"[^\w\s\-]")
WS_RE = re.compile(r"\s+")


def _ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def tokenize(text: str) -> list[str]:
    text = _ascii_fold(text or "").lower()
    text = PUNCT_RE.sub(" ", text)
    text = WS_RE.sub(" ", text).strip()
    return [t for t in text.split(" ") if t]


def significant_tokens(text: str, limit: int = 6) -> list[str]:
    tokens = [t for t in tokenize(text) if t not in STOPWORDS and len(t) > 1]
    return tokens[:limit]


def extract_model_numbers(text: str) -> list[str]:
    """Model-like tokens, most specific (longest) first."""
    candidates = [t for t in tokenize(text) if MODEL_TOKEN_RE.match(t)]
    # Pure years and prices are not model numbers.
    filtered = [
        c for c in candidates if not (c.isdigit() and (len(c) == 4 and c.startswith(("19", "20"))))
    ]
    return sorted(set(filtered), key=lambda c: (-len(c), c))


def normalize_item_key(
    title: str,
    *,
    brand: str | None = None,
    model: str | None = None,
    upc: str | None = None,
) -> str:
    """Stable identity key used to group comps."""
    if upc:
        digits = re.sub(r"\D", "", upc)
        if len(digits) >= 8:
            return f"upc:{digits}"

    brand_token = (significant_tokens(brand, 1) or [""])[0] if brand else ""

    model_token = ""
    if model:
        models = extract_model_numbers(model)
        model_token = models[0] if models else (significant_tokens(model, 1) or [""])[0]
    if not model_token:
        models = extract_model_numbers(title)
        model_token = models[0] if models else ""

    if brand_token and model_token:
        return f"bm:{brand_token}:{model_token}"
    if model_token:
        return f"m:{model_token}"

    tokens = significant_tokens(title, 5)
    if brand_token and brand_token not in tokens:
        tokens = [brand_token, *tokens][:5]
    return "t:" + "-".join(sorted(tokens)) if tokens else "t:unknown"


def search_query(title: str, *, brand: str | None = None, model: str | None = None) -> str:
    """A concise query string for external marketplace search."""
    parts: list[str] = []
    if brand:
        parts.extend(significant_tokens(brand, 1))
    models = extract_model_numbers(model or "") or extract_model_numbers(title)
    if models:
        parts.append(models[0])
    for token in significant_tokens(title, 5):
        if token not in parts:
            parts.append(token)
        if len(parts) >= 5:
            break
    return " ".join(parts)


TRAILING_SUFFIX_RE = re.compile(r"^([a-z]*\d[a-z0-9]*?)[a-z]{1,2}$")


def key_family(key: str) -> str:
    """Broader key for lookup, so variant SKUs group together.

    Manufacturers append letters for packaging variants: DCD999B is the bare
    tool, DCD999 the kit — same drill, and a comp for one is a valid comp for
    the other. Storage keeps the precise key; lookup widens to the family.
    """
    if key.startswith("upc:"):
        return key
    prefix, _, model = key.rpartition(":")
    if not prefix or not model:
        return key
    match = TRAILING_SUFFIX_RE.match(model)
    if match and len(match.group(1)) >= 4:
        return f"{prefix}:{match.group(1)}"
    return key


def title_similarity(a: str, b: str) -> float:
    """Jaccard over significant tokens, with a model-number override.

    Matching model numbers is near-conclusive, so it floors the score high;
    conflicting model numbers floor it low regardless of shared words.
    """
    models_a = set(extract_model_numbers(a))
    models_b = set(extract_model_numbers(b))
    if models_a and models_b:
        if models_a & models_b:
            return 1.0
        return 0.15

    tokens_a = set(significant_tokens(a, 12))
    tokens_b = set(significant_tokens(b, 12))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
