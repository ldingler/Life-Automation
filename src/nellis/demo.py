"""Offline demo dataset.

Purpose: let you see the whole system working — dashboard, queue, repair flags,
exposure meter, digest email — with no credentials, no network, and no scraping.

This does NOT insert pre-baked answers. It seeds raw lots and closed sales, then
runs them through the real `ValuationEngine`, so every number on screen is
genuinely computed by the same code that runs in production. If the valuation
logic is wrong, the demo shows it being wrong.

The dataset is built around item *families*: a run of closed sales for each
product, which become real comps, plus open lots of the same products. That's
what lets confidence scoring reach HIGH instead of reporting NONE on everything.

Deliberate edge cases are included so the demo proves the guardrails, not just
the happy path:

    * a clean high-margin lot           -> recommended, HIGH confidence
    * a blender missing its lid         -> repair path, easy-fix flag
    * a fridge with a dead compressor   -> fatal, correctly rejected
    * headphones already bid too high   -> past walk-away, correctly rejected
    * an obscure item with no comps     -> NONE confidence, correctly warned
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .ingest.adapter import LotRecord
from .ingest.harvest import record_snapshot, upsert_lot
from .models import (
    Alert,
    BidQueueEntry,
    Comp,
    Commitment,
    CommitmentStatus,
    Lot,
    LotSnapshot,
    PortfolioItem,
    Valuation,
    Watch,
    WatchMatch,
)
from .search.watch import _enqueue
from .valuation.cost import landed_cost
from .valuation.engine import ValuationEngine

log = logging.getLogger(__name__)

# Fixed seed: the demo must look the same every time it is run, so a number you
# saw yesterday is the same number today.
SEED = 20260809

STORES = ["Philadelphia, PA", "Bensalem, PA", "Cherry Hill, NJ"]


@dataclass
class Family:
    """A product with a sales history, plus the open lots we'll value."""

    title: str
    brand: str
    model: str
    category: str
    retail: float
    close_low: float
    close_high: float
    closes: int = 14
    channel: str = "ebay"
    open_lots: list[dict] = field(default_factory=list)


# Close ranges are what these actually tend to go for at liquidation auction —
# well under retail, which is exactly why "retail $299" is a misleading anchor.
FAMILIES: list[Family] = [
    Family(
        title="DeWalt DCD999B 20V MAX XR Hammer Drill (Tool Only)",
        brand="DeWalt", model="DCD999B", category="Tools",
        retail=299.00, close_low=84.0, close_high=108.0, closes=16,
        open_lots=[
            {"bid": 12.00, "condition": "Open Box",
             "notes": "Box opened, tool appears unused. All accessories present."},
            {"bid": 46.00, "condition": "Used",
             "notes": "Light wear on chuck. Functional."},
        ],
    ),
    Family(
        title="Milwaukee 2767-20 M18 FUEL High Torque Impact Wrench",
        brand="Milwaukee", model="2767-20", category="Tools",
        retail=349.00, close_low=118.0, close_high=146.0, closes=15,
        open_lots=[
            {"bid": 31.00, "condition": "Like New", "notes": "Complete in box."},
            {"bid": 22.00, "condition": "Damaged",
             "notes": "Missing battery and charger. Tool itself untested."},
        ],
    ),
    Family(
        title="Dyson V8 Animal Cordless Stick Vacuum",
        brand="Dyson", model="V8", category="Home",
        retail=329.99, close_low=118.0, close_high=152.0, closes=13,
        open_lots=[
            {"bid": 19.00, "condition": "Like New", "notes": "Complete with attachments."},
            {"bid": 14.00, "condition": "Damaged",
             "notes": "Missing filter and one attachment. Motor runs."},
        ],
    ),
    Family(
        title="Sony WH-1000XM5 Wireless Noise Cancelling Headphones",
        brand="Sony", model="WH-1000XM5", category="Electronics",
        retail=399.99, close_low=148.0, close_high=182.0, closes=18,
        open_lots=[
            {"bid": 38.00, "condition": "Like New", "notes": "Complete in box with case."},
            # Deliberately bid past its walk-away — the engine must reject this.
            {"bid": 214.00, "condition": "Open Box", "notes": "Complete."},
        ],
    ),
    Family(
        title="Ninja BL660 Professional Blender 1100W",
        brand="Ninja", model="BL660", category="Kitchen",
        retail=129.99, close_low=28.0, close_high=44.0, closes=12,
        # Nobody ships a blender. On eBay, fixed shipping and fees eat the entire
        # margin on a ~$35 item — which is exactly why channel choice belongs in
        # the model rather than being assumed.
        channel="local",
        open_lots=[
            {"bid": 4.00, "condition": "Damaged",
             "notes": "Missing lid. Pitcher, base and blades all included. Untested."},
        ],
    ),
    Family(
        title="Instant Pot Duo 7-in-1 Electric Pressure Cooker 6qt",
        brand="Instant Pot", model="Duo60", category="Kitchen",
        retail=99.95, close_low=26.0, close_high=41.0, closes=12,
        open_lots=[
            {"bid": 3.00, "condition": "Damaged",
             "notes": "Missing sealing ring and steam rack. Otherwise complete."},
        ],
    ),
    Family(
        title="Weber Spirit II E-310 3-Burner Propane Grill",
        brand="Weber", model="E-310", category="Outdoor",
        retail=549.00, close_low=182.0, close_high=238.0, closes=11,
        channel="local",
        open_lots=[
            {"bid": 55.00, "condition": "Open Box",
             "notes": "Assembled, never used. Local pickup only."},
        ],
    ),
    Family(
        title="Samsung RF28R7351SG French Door Refrigerator",
        brand="Samsung", model="RF28R7351SG", category="Appliances",
        retail=2399.00, close_low=520.0, close_high=780.0, closes=9,
        channel="local",
        open_lots=[
            # Fatal damage — must be rejected no matter how cheap it is.
            {"bid": 25.00, "condition": "Damaged",
             "notes": "Compressor is shot. Does not power on. Cosmetic dents on door."},
        ],
    ),
]

# No sales history at all — exercises the NONE-confidence path.
ORPHAN_LOTS = [
    {
        "title": "Assorted Craft Supplies Mixed Lot",
        "category": "Home", "retail": 64.99, "bid": 7.00,
        "condition": "Used", "notes": "Contents vary. Untested.",
    },
    {
        "title": "Unbranded Bluetooth Shower Speaker",
        "category": "Electronics", "retail": 24.99, "bid": 2.00,
        "condition": "New", "notes": "",
    },
]


def _record(
    *, lot_id: int, title: str, category: str, retail: float, bid: float,
    condition: str, notes: str, close_at: datetime | None,
    brand: str | None = None, model: str | None = None,
    rng: random.Random, closed: bool = False, final: float | None = None,
) -> LotRecord:
    return LotRecord(
        nellis_id=str(lot_id),
        url=f"https://www.nellisauction.com/p/{lot_id}",
        title=title,
        description=notes or None,
        category=category,
        brand=brand,
        model=model,
        condition_name=condition,
        condition_notes=notes or None,
        retail_price=retail,
        current_bid=final if closed else bid,
        bid_count=rng.randint(3, 28),
        buyers_premium_rate=0.15,
        location=rng.choice(STORES),
        location_zip="19124",
        close_at=close_at,
        images=[],
        is_closed=closed,
        final_price=final,
        raw={"_source": "demo"},
    )


def clear(session: Session) -> None:
    """Wipe demo/all data. Used by --reset."""
    for model in (
        Alert, BidQueueEntry, Commitment, PortfolioItem, Valuation,
        WatchMatch, LotSnapshot, Comp, Lot, Watch,
    ):
        session.execute(delete(model))
    session.flush()


async def seed(
    session: Session,
    settings: Settings | None = None,
    *,
    reset: bool = False,
) -> dict:
    """Build the demo dataset and run the real engine over it."""
    settings = settings or get_settings()
    rng = random.Random(SEED)
    now = datetime.now(UTC)

    if reset:
        clear(session)

    lot_id = 700_000
    stats = {"closed": 0, "open": 0, "comps": 0, "recommended": 0,
             "repair_flagged": 0, "rejected": 0, "queued": 0}

    # ---- 1. sales history -> real comps ---------------------------------
    sold_dates: dict[str, datetime] = {}
    for family in FAMILIES:
        for _ in range(family.closes):
            lot_id += 1
            final = round(rng.uniform(family.close_low, family.close_high), 2)
            sold_at = now - timedelta(days=rng.uniform(1, 55))
            record = _record(
                lot_id=lot_id, title=family.title, category=family.category,
                retail=family.retail, bid=final, condition="Open Box",
                notes="", close_at=sold_at,
                brand=family.brand, model=family.model, rng=rng,
                closed=True, final=final,
            )
            lot, _ = upsert_lot(session, record)
            sold_dates[lot.url] = sold_at
            stats["closed"] += 1
    session.flush()

    # `close_lot` stamps comps with "now". Backdate them to the sale dates above
    # so recency weighting and confidence scoring behave as they would live —
    # otherwise every comp looks a few seconds old and confidence is inflated.
    for lot in session.scalars(select(Lot).where(Lot.is_closed.is_(True))).all():
        sold_at = sold_dates.get(lot.url)
        if sold_at is not None:
            lot.closed_at = sold_at
    for comp in session.scalars(select(Comp).where(Comp.source == "nellis")).all():
        sold_at = sold_dates.get(comp.url or "")
        if sold_at is not None:
            comp.sold_at = sold_at
    session.flush()
    stats["comps"] = session.scalar(select(func.count(Comp.id))) or 0

    # ---- 2. open lots ----------------------------------------------------
    open_specs: list[tuple[dict, Family | None]] = []
    for family in FAMILIES:
        for spec in family.open_lots:
            open_specs.append((spec, family))
    for spec in ORPHAN_LOTS:
        open_specs.append((spec, None))

    created: list[tuple[Lot, str]] = []
    for spec, family in open_specs:
        lot_id += 1
        closes_in = timedelta(minutes=rng.randint(35, 60 * 34))
        record = _record(
            lot_id=lot_id,
            title=family.title if family else spec["title"],
            category=family.category if family else spec["category"],
            retail=family.retail if family else spec["retail"],
            bid=spec["bid"],
            condition=spec["condition"],
            notes=spec.get("notes", ""),
            close_at=now + closes_in,
            brand=family.brand if family else None,
            model=family.model if family else None,
            rng=rng,
        )
        lot, _ = upsert_lot(session, record)
        record_snapshot(session, lot)
        created.append((lot, family.channel if family else "ebay"))
        stats["open"] += 1
    session.flush()

    # ---- 3. run the REAL engine -----------------------------------------
    engine = ValuationEngine(session, settings, offline=True)
    for lot, channel in created:
        result = await engine.value(lot, channel_key=channel)
        if result.repair is not None and result.repair.is_worth_repairing:
            stats["repair_flagged"] += 1
        if result.recommended:
            stats["recommended"] += 1
            if _enqueue(session, result):
                stats["queued"] += 1
        else:
            stats["rejected"] += 1
    session.flush()

    # ---- 4. live commitments so exposure is non-zero ---------------------
    for lot, _ in created[:3]:
        entry = session.scalar(select(BidQueueEntry).where(BidQueueEntry.lot_id == lot.id))
        if entry is None:
            continue
        landed = landed_cost(
            entry.suggested_max_bid,
            bp_rate=lot.buyers_premium_rate or settings.default_buyers_premium,
            tax_rate=settings.sales_tax_rate,
            pickup=settings.pickup_cost,
        )
        session.add(
            Commitment(
                lot_id=lot.id, max_bid=entry.suggested_max_bid,
                landed_at_max=landed.total, category=lot.category,
                status=CommitmentStatus.LEADING,
            )
        )

    # ---- 5. portfolio history so realized-vs-projected has data ----------
    won = session.scalars(
        select(Lot).where(Lot.is_closed.is_(True)).limit(3)
    ).all()
    for index, lot in enumerate(won):
        hammer = lot.final_price or 50.0
        landed = landed_cost(hammer, bp_rate=0.15, tax_rate=settings.sales_tax_rate)
        item = PortfolioItem(
            lot_id=lot.id, hammer_price=hammer, landed_cost=landed.total,
            projected_profit=round(landed.total * 0.55, 2),
        )
        if index < 2:
            # Sold slightly under projection on purpose — the Portfolio page is
            # meant to reveal when the model runs optimistic.
            item.sold_price = round(landed.total * 1.42, 2)
            item.sold_fees = round(item.sold_price * 0.1325, 2)
            item.sold_channel = "ebay"
            item.sold_at = now - timedelta(days=rng.randint(3, 20))
        session.add(item)

    # ---- 6. a couple of starter watches ---------------------------------
    if (session.scalar(select(func.count(Watch.id))) or 0) == 0:
        session.add_all([
            Watch(name="Power tools", keywords="dewalt,milwaukee,makita,ryobi",
                  min_discount_pct=0.55, target_margin=0.40, resale_channel="ebay",
                  include_damaged=True, enabled=True),
            Watch(name="Repairable kitchen", categories="Kitchen",
                  keywords="ninja,instant pot,kitchenaid,vitamix",
                  max_current_bid=40, target_margin=0.45, max_repair_cost=35,
                  resale_channel="ebay", include_damaged=True, enabled=True),
        ])

    session.flush()
    log.info("demo seeded: %s", stats)
    return stats
