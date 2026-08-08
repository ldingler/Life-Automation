"""Condition and damage signal extraction.

Most bidders skip anything labeled damaged or incomplete, which is exactly why
those lots go cheap. The money is in telling apart:

    "missing lid"        -> $8 part, 5 minutes, item becomes whole
    "compressor is shot" -> scrap

This module reads the listing text and produces a structured verdict: how much
of the item's functional value survives as-is, what is missing, and whether the
gap is closeable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Parts that are routinely orderable for cheap. Finding one of these named as
# missing is the strongest "easy fix" signal there is.
REPLACEABLE_PARTS: dict[str, float] = {
    # part name -> typical replacement cost in USD (fallback when no lookup hits)
    "lid": 12.0, "cover": 14.0, "cap": 8.0, "remote": 15.0, "remote control": 15.0,
    "charger": 18.0, "power cord": 12.0, "cord": 12.0, "cable": 10.0, "adapter": 16.0,
    "power supply": 22.0, "battery": 28.0, "batteries": 12.0, "manual": 0.0,
    "screws": 6.0, "hardware": 9.0, "bolts": 7.0, "filter": 14.0, "filters": 18.0,
    "blade": 16.0, "blades": 20.0, "hose": 15.0, "handle": 14.0, "knob": 8.0,
    "shelf": 18.0, "tray": 14.0, "basket": 16.0, "rack": 18.0, "drawer": 24.0,
    "bag": 15.0, "case": 22.0, "stand": 20.0, "mount": 16.0, "bracket": 12.0,
    "wheel": 14.0, "wheels": 26.0, "gasket": 9.0, "seal": 9.0, "belt": 11.0,
    "pitcher": 24.0, "carafe": 22.0, "plate": 12.0, "grate": 16.0, "nozzle": 10.0,
    "attachment": 18.0, "attachments": 30.0, "accessories": 25.0, "stylus": 12.0,
    "earbuds": 25.0, "ear tips": 9.0, "strap": 12.0, "band": 14.0, "charger cable": 12.0,
    "screen protector": 8.0, "antenna": 11.0, "fuse": 5.0, "bulb": 9.0, "key": 10.0,
    "keys": 14.0, "pump": 26.0, "tube": 9.0, "clamp": 8.0, "foot": 7.0, "feet": 10.0,
}

# Phrases indicating the item is fundamentally broken, not merely incomplete.
FATAL_PATTERNS = (
    r"does ?n[o']?t (?:power|turn) on", r"won'?t (?:power|turn) on", r"no power",
    r"motor (?:is )?(?:bad|dead|shot|seized)", r"compressor (?:is )?(?:bad|dead|shot)",
    r"cracked (?:screen|display|lcd)", r"shattered", r"water damage", r"burn(?:ed|t) out",
    r"for parts(?: only)?", r"parts/repair", r"salvage", r"scrap", r"not (?:working|functional)",
    r"stripped", r"seized", r"leaking", r"mold", r"rusted through",
)

COSMETIC_PATTERNS = (
    r"scratch", r"scuff", r"dent", r"ding", r"blemish", r"cosmetic",
    r"discolor", r"stain", r"faded", r"chipped", r"marks?\b", r"worn",
)

UNTESTED_PATTERNS = (r"untested", r"not tested", r"unable to test", r"as[- ]is", r"as is\b")

INCOMPLETE_PATTERNS = (
    r"missing", r"incomplete", r"does ?n[o']?t include", r"not included",
    r"no (?:remote|charger|cord|lid|manual|battery|hardware)", r"without (?:the )?",
    r"partial", r"open(?:ed)? box", r"tool only", r"bare tool", r"body only",
)

# Condition grade -> fraction of functional-comp value the item retains.
CONDITION_MULTIPLIERS: tuple[tuple[str, float], ...] = (
    (r"brand new|factory sealed|sealed|new in box|nib", 1.00),
    (r"like new|open box|openbox|excellent", 0.92),
    (r"very good|refurb", 0.85),
    (r"\bgood\b|used", 0.78),
    (r"\bfair\b|acceptable", 0.65),
    (r"damaged|for parts|salvage|poor|broken", 0.40),
)

MISSING_RE = re.compile(
    r"(?:missing|no|without|does ?n[o']?t include|not included:?)\s+"
    r"(?:the\s+|a\s+|an\s+|any\s+)?([a-z][a-z\s/\-]{1,28})",
    re.IGNORECASE,
)


@dataclass
class DamageSignal:
    kind: str  # fatal | cosmetic | untested | incomplete
    phrase: str
    severity: float  # 0 = harmless, 1 = ruinous

    def as_dict(self) -> dict:
        return {"kind": self.kind, "phrase": self.phrase, "severity": round(self.severity, 2)}


@dataclass
class ConditionReport:
    condition_multiplier: float
    signals: list[DamageSignal] = field(default_factory=list)
    missing_parts: list[str] = field(default_factory=list)
    is_fatal: bool = False
    is_untested: bool = False
    is_incomplete: bool = False

    @property
    def signal_dicts(self) -> list[dict]:
        return [s.as_dict() for s in self.signals]

    @property
    def summary(self) -> str:
        if self.is_fatal:
            return "Likely non-functional — treat as parts value only"
        parts = []
        if self.missing_parts:
            parts.append("missing " + ", ".join(self.missing_parts[:4]))
        if self.is_untested:
            parts.append("untested")
        cosmetic = [s for s in self.signals if s.kind == "cosmetic"]
        if cosmetic:
            parts.append("cosmetic wear")
        return "; ".join(parts) if parts else "No damage signals detected"


def _match_any(patterns: tuple[str, ...], text: str) -> list[str]:
    hits = []
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            hits.append(match.group(0).strip())
    return hits


def extract_missing_parts(text: str) -> list[str]:
    """Pull named missing components, keeping only ones we recognize as orderable."""
    found: list[str] = []
    for match in MISSING_RE.finditer(text):
        phrase = re.sub(r"\s+", " ", match.group(1).strip().lower())
        # Trim trailing filler so "lid and the base is fine" -> "lid"
        phrase = re.split(r"\b(?:and|but|however|though|the item|item)\b", phrase)[0].strip()
        phrase = phrase.rstrip(".,;:/- ")
        if not phrase:
            continue
        # Prefer the longest known part name contained in the phrase.
        candidates = [p for p in REPLACEABLE_PARTS if p in phrase]
        if candidates:
            best = max(candidates, key=len)
            if best not in found:
                found.append(best)
        elif 2 < len(phrase) <= 24 and phrase not in found:
            words = phrase.split()
            if len(words) <= 3:
                found.append(phrase)
    return found


def analyze_condition(
    title: str,
    condition_name: str | None = None,
    condition_notes: str | None = None,
    description: str | None = None,
) -> ConditionReport:
    """Read all listing text and return a structured condition verdict."""
    blob = " ".join(filter(None, [title, condition_name, condition_notes, description]))
    lowered = blob.lower()

    signals: list[DamageSignal] = []

    fatal_hits = _match_any(FATAL_PATTERNS, lowered)
    for hit in fatal_hits:
        signals.append(DamageSignal("fatal", hit, 0.95))

    for hit in _match_any(COSMETIC_PATTERNS, lowered):
        signals.append(DamageSignal("cosmetic", hit, 0.15))

    untested_hits = _match_any(UNTESTED_PATTERNS, lowered)
    for hit in untested_hits:
        signals.append(DamageSignal("untested", hit, 0.35))

    incomplete_hits = _match_any(INCOMPLETE_PATTERNS, lowered)
    for hit in incomplete_hits:
        signals.append(DamageSignal("incomplete", hit, 0.45))

    missing_parts = extract_missing_parts(lowered) if incomplete_hits else []

    # Base multiplier from the stated condition grade.
    multiplier = 0.80
    grade_text = (condition_name or "") + " " + title
    for pattern, value in CONDITION_MULTIPLIERS:
        if re.search(pattern, grade_text, re.IGNORECASE):
            multiplier = value
            break

    is_fatal = bool(fatal_hits)
    if is_fatal:
        multiplier = min(multiplier, 0.25)
    if untested_hits and not is_fatal:
        multiplier *= 0.88
    if any(s.kind == "cosmetic" for s in signals) and not is_fatal:
        multiplier *= 0.94

    return ConditionReport(
        condition_multiplier=round(max(0.05, min(1.0, multiplier)), 4),
        signals=signals,
        missing_parts=missing_parts,
        is_fatal=is_fatal,
        is_untested=bool(untested_hits),
        is_incomplete=bool(incomplete_hits),
    )
