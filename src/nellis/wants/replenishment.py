"""How wanting something changes after you buy one.

The problem, in Logan's words: *"Once I purchase a more expensive item, I likely
won't need that item again — however that is not always true. Just because I
bought a nice shed doesn't mean I don't need another one. Just because I bought
screws doesn't mean I won't need more screws."*

So "already bought it" is not a filter, it's a **decay curve**, and which curve
applies depends on what kind of thing it is:

    screws          buying more is the normal case         no suppression, ever
    storage bins    you can always use another             shallow dip, fast recovery
    shed            plausible you'd want a second          deep dip, slow recovery
    microwave       one is genuinely enough                near-total, very slow

Two things make this workable in practice rather than a classification chore:

  * **Price modulates the guess.** A $4 item is almost never a
    once-in-a-lifetime purchase; a $600 one usually is. Price is the strongest
    single hint when the words are ambiguous.
  * **Suppression is never permanent.** Everything recovers eventually, because
    the alternative — a hard "you own one" filter — silently hides things you
    genuinely need again, and you'd never know what you missed.

Returns are handled separately and hit harder than purchases. Buying something
means you wanted it; returning it means you tried it and didn't.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from ..models import DemandSignal, ReplenishmentClass, SignalSource


@dataclass(frozen=True)
class Profile:
    """Decay parameters for one replenishment class.

    `floor` is how far interest drops immediately after buying one — 1.0 means
    untouched. `half_life_days` is how fast it climbs back toward full.
    """

    floor: float
    half_life_days: float
    per_extra_unit: float  # multiplier applied to the floor per additional unit owned
    label: str


PROFILES: dict[ReplenishmentClass, Profile] = {
    # Never suppressed. Buying screws is evidence you use screws.
    ReplenishmentClass.CONSUMABLE: Profile(
        floor=1.00, half_life_days=1.0, per_extra_unit=1.00,
        label="consumable — buying more is normal",
    ),
    # Mild, brief dip: you just stocked up, but another is rarely unwelcome.
    ReplenishmentClass.STOCKABLE: Profile(
        floor=0.70, half_life_days=30.0, per_extra_unit=0.85,
        label="stockable — more is usually fine",
    ),
    # The shed case: a second is plausible, but not next week.
    ReplenishmentClass.DURABLE_MULTI: Profile(
        floor=0.30, half_life_days=180.0, per_extra_unit=0.55,
        label="durable — you might want another eventually",
    ),
    # One is enough. Recovers over years, for replacement rather than addition.
    ReplenishmentClass.DURABLE_SINGLE: Profile(
        floor=0.05, half_life_days=900.0, per_extra_unit=0.40,
        label="one is enough",
    ),
}

# A return is the strongest negative evidence available: you had it and rejected
# it. Suppress harder and longer than a purchase of the same thing.
RETURN_FLOOR = 0.08
RETURN_HALF_LIFE_DAYS = 540.0

CONSUMABLE_TERMS = {
    "screw", "screws", "nail", "nails", "bolt", "bolts", "nut", "nuts",
    # "washer" is deliberately absent: a hardware washer and a laundry washer
    # share the word, and misreading a $600 appliance as a consumable would stop
    # it ever being suppressed. Hardware lots almost always name a companion
    # fastener ("nuts, bolts and washers"), which catches them here anyway.
    "anchor", "anchors", "fastener", "fasteners", "zip tie", "cable tie", "staple",
    "battery", "batteries", "filter", "filters", "bulb", "bulbs", "fuse",
    "tape", "glue", "adhesive", "caulk", "grout", "sealant", "epoxy", "solder",
    "sandpaper", "sanding", "blade", "blades", "bit", "bits", "drill bit",
    "detergent", "soap", "cleaner", "wipes", "sponge", "paper towel", "toilet paper",
    "trash bag", "bags", "liner", "propane", "fuel", "oil", "lubricant", "grease",
    "ink", "toner", "diaper", "diapers", "formula", "snack", "coffee", "pods",
    "vitamin", "supplement", "shampoo", "toothpaste", "razor", "floss",
}

STOCKABLE_TERMS = {
    "bin", "bins", "tote", "totes", "container", "containers", "basket",
    "shelf", "shelves", "hook", "hooks", "hanger", "hangers", "organizer",
    "cord", "extension cord", "power strip", "cable", "rope", "twine", "strap",
    "tarp", "bucket", "bucket set", "clamp", "clamps", "towel", "towels",
    "blanket", "pillow", "sheet", "hose", "sprinkler", "planter", "pot",
    "storage", "drawer", "caddy", "tray", "mat", "rug",
}

DURABLE_MULTI_TERMS = {
    "shed", "chair", "chairs", "stool", "table", "desk", "bench", "cart",
    "ladder", "fan", "heater", "lamp", "light", "monitor", "speaker", "headphone",
    "cabinet", "bookcase", "drill", "saw", "sander", "grinder", "wrench",
    "tool", "toolbox", "vacuum", "tire", "wheel", "bike", "cooler", "canopy",
    "gazebo", "firepit", "fire pit", "umbrella", "mirror", "clock", "keyboard",
}

DURABLE_SINGLE_TERMS = {
    "refrigerator", "fridge", "freezer", "washer", "dryer", "dishwasher",
    "microwave", "oven", "range", "stove", "cooktop", "water heater", "furnace",
    "air conditioner", "hvac", "generator", "snowblower", "snow blower",
    "lawn mower", "mower", "tractor", "pressure washer", "treadmill", "elliptical",
    "mattress", "sofa", "couch", "sectional", "television", " tv ", "projector",
    "laptop", "desktop computer", "dryer vent", "grill", "smoker", "hot tub",
    "dehumidifier", "humidifier", "air purifier", "septic", "sump pump",
}

# Price bands that override weak word evidence.
CHEAP_CEILING = 15.0     # below this, treat as consumable-ish
STOCK_CEILING = 60.0
EXPENSIVE_FLOOR = 250.0  # above this, one is usually enough


def infer_class(
    title: str,
    *,
    category: str | None = None,
    price: float | None = None,
) -> ReplenishmentClass:
    """Best guess at how replenishable something is.

    Word evidence first, since "screws" is unambiguous at any price. Price only
    decides when the words don't, which keeps a cheap pack of screws from being
    misread and an expensive shed from being called disposable.
    """
    haystack = f"{(title or '').lower()} {(category or '').lower()}"

    # Word boundaries, not substrings. "cordless drill" contains "cord", and a
    # naive `in` check files a drill as an extension cord.
    def hit(terms: set[str]) -> bool:
        return any(re.search(rf"\b{re.escape(term.strip())}\b", haystack) for term in terms)

    # Order matters where a title carries two signals. "Storage shed" contains
    # both "storage" and "shed"; the more specific object wins, so the broad
    # STOCKABLE bucket is checked last rather than first.
    if hit(CONSUMABLE_TERMS):
        return ReplenishmentClass.CONSUMABLE
    if hit(DURABLE_SINGLE_TERMS):
        return ReplenishmentClass.DURABLE_SINGLE
    if hit(DURABLE_MULTI_TERMS):
        return ReplenishmentClass.DURABLE_MULTI
    if hit(STOCKABLE_TERMS):
        return ReplenishmentClass.STOCKABLE

    # No word evidence — fall back to price, which is a decent proxy for
    # "how often would anyone buy this?"
    if price is not None:
        if price <= CHEAP_CEILING:
            return ReplenishmentClass.CONSUMABLE
        if price <= STOCK_CEILING:
            return ReplenishmentClass.STOCKABLE
        if price >= EXPENSIVE_FLOOR:
            return ReplenishmentClass.DURABLE_SINGLE

    return ReplenishmentClass.DURABLE_MULTI


def _recovery(floor: float, half_life_days: float, days_since: float) -> float:
    """Interest recovering from `floor` back toward 1.0 over time."""
    if floor >= 1.0:
        return 1.0
    days_since = max(0.0, days_since)
    remaining = 0.5 ** (days_since / half_life_days) if half_life_days > 0 else 0.0
    return floor + (1.0 - floor) * (1.0 - remaining)


@dataclass
class Satiation:
    multiplier: float
    reason: str
    owned_units: int = 0
    returned: bool = False

    # Meaningfully damped, not merely touched. A shed bought over a year ago
    # scores ~0.85 and should read as available again, not as still-hidden.
    SUPPRESSED_BELOW = 0.75

    @property
    def is_suppressed(self) -> bool:
        return self.multiplier < self.SUPPRESSED_BELOW


def satiation_for(
    signals: list[DemandSignal],
    replenishment: ReplenishmentClass,
    *,
    now: datetime | None = None,
) -> Satiation:
    """How much to damp interest, given what's already been bought or returned.

    `signals` should be the purchase/return history for one item family.
    """
    now = now or datetime.now(UTC)
    profile = PROFILES[replenishment]

    purchases = [s for s in signals if s.source == SignalSource.NELLIS_PURCHASE.value
                 or s.source == SignalSource.AMAZON_ORDER.value]
    returns = [s for s in signals if s.source == SignalSource.NELLIS_RETURN.value]

    if not purchases and not returns:
        return Satiation(multiplier=1.0, reason="never bought this")

    # A return dominates: you had it and gave it back.
    if returns:
        newest = max(returns, key=lambda s: _aware(s.occurred_at))
        days = (now - _aware(newest.occurred_at)).total_seconds() / 86400.0
        multiplier = _recovery(RETURN_FLOOR, RETURN_HALF_LIFE_DAYS, days)
        return Satiation(
            multiplier=round(multiplier, 4),
            reason=f"you returned one {int(days)}d ago — strong signal you didn't want it",
            owned_units=sum(s.quantity for s in purchases),
            returned=True,
        )

    if profile.floor >= 1.0:
        units = sum(s.quantity for s in purchases)
        return Satiation(
            multiplier=1.0,
            reason=f"{profile.label}; owning {units} changes nothing",
            owned_units=units,
        )

    units = sum(max(1, s.quantity) for s in purchases)
    newest = max(purchases, key=lambda s: _aware(s.occurred_at))
    days = (now - _aware(newest.occurred_at)).total_seconds() / 86400.0

    # Each extra unit deepens the floor: three sheds suppress harder than one.
    floor = profile.floor * (profile.per_extra_unit ** max(0, units - 1))
    floor = max(0.02, floor)
    multiplier = _recovery(floor, profile.half_life_days, days)

    if multiplier >= Satiation.SUPPRESSED_BELOW:
        reason = f"bought one {int(days)}d ago, but that's long enough ago to want another"
    elif units > 1:
        reason = f"you already own {units}; last bought {int(days)}d ago ({profile.label})"
    else:
        reason = f"bought one {int(days)}d ago ({profile.label})"

    return Satiation(
        multiplier=round(multiplier, 4), reason=reason, owned_units=units
    )


def days_until_resurface(
    replenishment: ReplenishmentClass, *, threshold: float = 0.75
) -> float | None:
    """Roughly when a suppressed item becomes interesting again.

    Shown in the UI so suppression is visible and finite rather than a silent
    disappearance — "hidden until ~Mar 2027" instead of the item just vanishing.
    """
    profile = PROFILES[replenishment]
    if profile.floor >= threshold:
        return 0.0
    if profile.half_life_days <= 0:
        return None
    import math

    remaining = (1.0 - threshold) / (1.0 - profile.floor)
    if remaining <= 0:
        return None
    return profile.half_life_days * math.log2(1.0 / remaining)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
