"""Database models.

Design notes:
  * `Lot` is the current state; `LotSnapshot` is the time series. Keeping both
    lets us chart bid velocity and, critically, harvest FINAL prices into our
    own comps database — the single most valuable asset this system builds.
  * `Commitment` tracks what you're actually on the hook for. Exposure control
    reads from here.
  * Nothing here ever places a bid. `BidQueueEntry` is a recommendation with a
    human decision attached.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Confidence(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class CommitmentStatus(str, enum.Enum):
    LEADING = "leading"
    OUTBID = "outbid"
    WON = "won"
    LOST = "lost"
    CANCELLED = "cancelled"


class QueueStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"  # human confirmed they placed it
    SKIPPED = "skipped"
    EXPIRED = "expired"
    BLOCKED = "blocked"  # exposure cap would be breached


# --------------------------------------------------------------------------
# What a purchase was FOR — bookkeeping, not valuation
# --------------------------------------------------------------------------


class Purpose(str, enum.Enum):
    """Why an item was bought. Drives the books, not the bidding."""

    MSS_EXPENSE = "mss_expense"   # Mia's Sharing Shelf — the toy library
    RESELL = "resell"
    PERSONAL = "personal"
    UNDECIDED = "undecided"


class MssCategory(str, enum.Enum):
    """Sub-category for MSS company expenses."""

    INVENTORY_TOYS = "inventory_toys"
    SUPPLIES = "supplies"
    FIXTURES = "fixtures"
    OFFICE = "office"
    OTHER = "other"


class ValueBasis(str, enum.Enum):
    """Which reference a savings figure was measured against.

    Recorded explicitly because "you saved $340" means very different things
    depending on the denominator. Liquidation listings routinely inflate the
    stated retail price, and savings claimed against a made-up number is not a
    figure to put in a company expense record.
    """

    COMPS = "comps"                  # observed second-hand sale prices — strongest
    RETAIL_VERIFIED = "retail_ok"    # stated retail, corroborated by comps
    RETAIL_STATED = "retail_stated"  # stated retail, uncorroborated — treat with care
    NONE = "none"


# --------------------------------------------------------------------------
# Listings
# --------------------------------------------------------------------------


class Lot(Base):
    __tablename__ = "lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nellis_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    url: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(String(512), index=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)

    category: Mapped[str | None] = mapped_column(String(128), index=True, default=None)
    brand: Mapped[str | None] = mapped_column(String(128), index=True, default=None)
    model: Mapped[str | None] = mapped_column(String(128), default=None)
    upc: Mapped[str | None] = mapped_column(String(64), index=True, default=None)

    condition_name: Mapped[str | None] = mapped_column(String(128), default=None)
    condition_notes: Mapped[str | None] = mapped_column(Text, default=None)

    retail_price: Mapped[float | None] = mapped_column(Float, default=None)
    current_bid: Mapped[float] = mapped_column(Float, default=0.0)
    bid_count: Mapped[int] = mapped_column(Integer, default=0)
    buyers_premium_rate: Mapped[float | None] = mapped_column(Float, default=None)

    location: Mapped[str | None] = mapped_column(String(128), index=True, default=None)
    location_zip: Mapped[str | None] = mapped_column(String(16), default=None)

    open_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    close_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, default=None
    )

    images: Mapped[list | None] = mapped_column(JSON, default=None)
    raw: Mapped[dict | None] = mapped_column(JSON, default=None)

    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    final_price: Mapped[float | None] = mapped_column(Float, default=None)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    snapshots: Mapped[list[LotSnapshot]] = relationship(
        back_populates="lot", cascade="all, delete-orphan"
    )
    valuations: Mapped[list[Valuation]] = relationship(
        back_populates="lot", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_lots_open_close", "is_closed", "close_at"),
        Index("ix_lots_cat_closed", "category", "is_closed"),
    )

    @property
    def discount_pct(self) -> float | None:
        if not self.retail_price:
            return None
        return max(0.0, 1.0 - (self.current_bid / self.retail_price))


class LotSnapshot(Base):
    """Bid-over-time series. Feeds velocity analysis and close-price harvesting."""

    __tablename__ = "lot_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    current_bid: Mapped[float] = mapped_column(Float)
    bid_count: Mapped[int] = mapped_column(Integer, default=0)
    close_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    lot: Mapped[Lot] = relationship(back_populates="snapshots")

    __table_args__ = (Index("ix_snap_lot_time", "lot_id", "observed_at"),)


# --------------------------------------------------------------------------
# Searching
# --------------------------------------------------------------------------


class Watch(Base):
    """A saved search. Runs on a schedule; matches feed valuation and alerts."""

    __tablename__ = "watches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Term matching
    keywords: Mapped[str | None] = mapped_column(Text, default=None)  # comma-separated, OR
    require_all: Mapped[str | None] = mapped_column(Text, default=None)  # must-have, AND
    exclude_terms: Mapped[str | None] = mapped_column(Text, default=None)

    # Structured filters
    categories: Mapped[str | None] = mapped_column(Text, default=None)
    conditions: Mapped[str | None] = mapped_column(Text, default=None)
    locations: Mapped[str | None] = mapped_column(Text, default=None)
    brands: Mapped[str | None] = mapped_column(Text, default=None)

    min_retail: Mapped[float | None] = mapped_column(Float, default=None)
    max_retail: Mapped[float | None] = mapped_column(Float, default=None)
    min_current_bid: Mapped[float | None] = mapped_column(Float, default=None)
    max_current_bid: Mapped[float | None] = mapped_column(Float, default=None)
    min_discount_pct: Mapped[float | None] = mapped_column(Float, default=None)
    closes_within_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    closes_after_minutes: Mapped[int | None] = mapped_column(Integer, default=None)

    # Per-watch valuation overrides
    target_margin: Mapped[float | None] = mapped_column(Float, default=None)
    resale_channel: Mapped[str | None] = mapped_column(String(32), default=None)
    include_damaged: Mapped[bool] = mapped_column(Boolean, default=True)
    max_repair_cost: Mapped[float | None] = mapped_column(Float, default=None)

    notify: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class WatchMatch(Base):
    __tablename__ = "watch_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), index=True)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("watch_id", "lot_id", name="uq_watch_lot"),)


# --------------------------------------------------------------------------
# Comps & parts
# --------------------------------------------------------------------------


class Comp(Base):
    """A single observed price point from any source."""

    __tablename__ = "comps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(48), index=True)  # nellis|ebay|csv|feed
    query_key: Mapped[str] = mapped_column(String(256), index=True)  # normalized item key
    title: Mapped[str | None] = mapped_column(String(512), default=None)
    price: Mapped[float] = mapped_column(Float)
    shipping: Mapped[float] = mapped_column(Float, default=0.0)
    condition: Mapped[str | None] = mapped_column(String(64), default=None)
    is_sold: Mapped[bool] = mapped_column(Boolean, default=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    url: Mapped[str | None] = mapped_column(String(1024), default=None)
    raw: Mapped[dict | None] = mapped_column(JSON, default=None)

    __table_args__ = (Index("ix_comps_key_source", "query_key", "source", "observed_at"),)

    @property
    def total_price(self) -> float:
        return self.price + (self.shipping or 0.0)


class PartPrice(Base):
    """Cached replacement-part cost, keyed by a normalized part query."""

    __tablename__ = "part_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    part_key: Mapped[str] = mapped_column(String(256), index=True)
    description: Mapped[str | None] = mapped_column(String(512), default=None)
    source: Mapped[str] = mapped_column(String(48), default="manual")
    price: Mapped[float] = mapped_column(Float)
    availability: Mapped[str | None] = mapped_column(String(64), default=None)
    url: Mapped[str | None] = mapped_column(String(1024), default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("part_key", "source", name="uq_part_source"),)


# --------------------------------------------------------------------------
# Valuation output
# --------------------------------------------------------------------------


class Valuation(Base):
    """The engine's verdict on a lot at a point in time."""

    __tablename__ = "valuations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), index=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Comps
    comp_value: Mapped[float | None] = mapped_column(Float, default=None)
    comp_count: Mapped[int] = mapped_column(Integer, default=0)
    comp_sources: Mapped[list | None] = mapped_column(JSON, default=None)
    comp_spread: Mapped[float | None] = mapped_column(Float, default=None)
    confidence: Mapped[Confidence] = mapped_column(
        Enum(Confidence, native_enum=False), default=Confidence.NONE
    )

    # Condition / repair
    damage_signals: Mapped[list | None] = mapped_column(JSON, default=None)
    missing_parts: Mapped[list | None] = mapped_column(JSON, default=None)
    parts_cost: Mapped[float] = mapped_column(Float, default=0.0)
    labor_hours: Mapped[float] = mapped_column(Float, default=0.0)
    easy_fix_score: Mapped[float | None] = mapped_column(Float, default=None)
    repair_notes: Mapped[str | None] = mapped_column(Text, default=None)

    # Money
    resale_channel: Mapped[str] = mapped_column(String(32), default="ebay")
    net_resale: Mapped[float | None] = mapped_column(Float, default=None)
    walk_away_max_bid: Mapped[float] = mapped_column(Float, default=0.0)
    landed_at_max: Mapped[float | None] = mapped_column(Float, default=None)
    projected_profit: Mapped[float | None] = mapped_column(Float, default=None)
    projected_margin: Mapped[float | None] = mapped_column(Float, default=None)
    profit_at_current_bid: Mapped[float | None] = mapped_column(Float, default=None)

    # ---- outside market --------------------------------------------------
    # What it costs to just buy one elsewhere. A great discount off retail is
    # still a bad buy if an equally good product sells new for less.
    market_verification: Mapped[str | None] = mapped_column(String(24), default=None)
    verified_retail: Mapped[float | None] = mapped_column(Float, default=None)
    best_alternative_price: Mapped[float | None] = mapped_column(Float, default=None)
    best_alternative_note: Mapped[str | None] = mapped_column(Text, default=None)
    opportunity_ceiling: Mapped[float | None] = mapped_column(Float, default=None)

    recommended: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text, default=None)

    lot: Mapped[Lot] = relationship(back_populates="valuations")

    __table_args__ = (Index("ix_val_lot_time", "lot_id", "computed_at"),)


# --------------------------------------------------------------------------
# Exposure / decisions / portfolio
# --------------------------------------------------------------------------


class Commitment(Base):
    """A max bid you actually placed. Source of truth for exposure control."""

    __tablename__ = "commitments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), index=True)
    max_bid: Mapped[float] = mapped_column(Float)
    landed_at_max: Mapped[float] = mapped_column(Float)
    category: Mapped[str | None] = mapped_column(String(128), default=None)
    status: Mapped[CommitmentStatus] = mapped_column(
        Enum(CommitmentStatus, native_enum=False), default=CommitmentStatus.LEADING, index=True
    )
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (UniqueConstraint("lot_id", name="uq_commitment_lot"),)


class BidQueueEntry(Base):
    """A recommendation awaiting your decision. Never auto-executes."""

    __tablename__ = "bid_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), index=True)
    valuation_id: Mapped[int | None] = mapped_column(
        ForeignKey("valuations.id", ondelete="SET NULL"), default=None
    )
    suggested_max_bid: Mapped[float] = mapped_column(Float)
    projected_profit: Mapped[float | None] = mapped_column(Float, default=None)
    exposure_if_won: Mapped[float | None] = mapped_column(Float, default=None)
    rank_score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[QueueStatus] = mapped_column(
        Enum(QueueStatus, native_enum=False), default=QueueStatus.PENDING, index=True
    )
    block_reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (UniqueConstraint("lot_id", name="uq_queue_lot"),)


class PortfolioItem(Base):
    """A lot you won. Closes the loop: projected vs realized."""

    __tablename__ = "portfolio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), index=True)
    hammer_price: Mapped[float] = mapped_column(Float)
    landed_cost: Mapped[float] = mapped_column(Float)
    projected_profit: Mapped[float | None] = mapped_column(Float, default=None)

    repair_spend: Mapped[float] = mapped_column(Float, default=0.0)
    sold_price: Mapped[float | None] = mapped_column(Float, default=None)
    sold_fees: Mapped[float] = mapped_column(Float, default=0.0)
    sold_channel: Mapped[str | None] = mapped_column(String(32), default=None)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # ---- bookkeeping -----------------------------------------------------
    purpose: Mapped[Purpose] = mapped_column(
        Enum(Purpose, native_enum=False), default=Purpose.UNDECIDED, index=True
    )
    mss_category: Mapped[MssCategory | None] = mapped_column(
        Enum(MssCategory, native_enum=False), default=None
    )
    # Set when a human categorises by hand; blocks the classifier overwriting it.
    purpose_locked: Mapped[bool] = mapped_column(Boolean, default=False)

    # ---- savings ---------------------------------------------------------
    reference_value: Mapped[float | None] = mapped_column(Float, default=None)
    value_basis: Mapped[ValueBasis] = mapped_column(
        Enum(ValueBasis, native_enum=False), default=ValueBasis.NONE
    )
    savings: Mapped[float | None] = mapped_column(Float, default=None)

    @property
    def realized_profit(self) -> float | None:
        if self.sold_price is None:
            return None
        return self.sold_price - self.sold_fees - self.landed_cost - self.repair_spend


class Alert(Base):
    """Sent-notification ledger. `dedupe_key` is what stops inbox spam."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("lots.id", ondelete="CASCADE"), index=True, default=None
    )
    dedupe_key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    subject: Mapped[str | None] = mapped_column(String(512), default=None)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------
# Personal wants — "do I want this?" rather than "can I flip this?"
#
# The resale side of this system asks whether a lot can be sold on at a margin.
# These models answer a different question: whether Logan personally wants the
# thing. A shed he needs is worth buying at a price that would be a terrible
# flip, and the two verdicts must not be conflated.
# --------------------------------------------------------------------------


class ReplenishmentClass(str, enum.Enum):
    """How wanting something changes after you buy one.

    The distinction Logan drew: buying screws doesn't mean you stop needing
    screws, and buying a shed doesn't strictly mean you'll never want another —
    but buying a microwave probably does.
    """

    CONSUMABLE = "consumable"          # screws, batteries, filters — never satiated
    STOCKABLE = "stockable"            # bins, cords, tarps — more is fine
    DURABLE_MULTI = "durable_multi"    # sheds, chairs, tools — maybe another, later
    DURABLE_SINGLE = "durable_single"  # microwave, mower — one is enough


class SignalSource(str, enum.Enum):
    NELLIS_PURCHASE = "nellis_purchase"
    NELLIS_RETURN = "nellis_return"
    NELLIS_WATCHLIST = "nellis_watchlist"
    AMAZON_CART = "amazon_cart"
    AMAZON_SAVED = "amazon_saved"
    AMAZON_ORDER = "amazon_order"
    ALEXA_LIST = "alexa_list"
    MANUAL = "manual"


# How strongly each source implies "I want this", and whether it means
# "already satisfied". A return is the strongest possible negative signal.
SOURCE_INTENT: dict[str, float] = {
    SignalSource.MANUAL.value: 1.00,
    SignalSource.ALEXA_LIST.value: 0.95,      # you literally said you need it
    SignalSource.AMAZON_CART.value: 0.90,     # about to buy it
    SignalSource.NELLIS_WATCHLIST.value: 0.85,
    SignalSource.AMAZON_SAVED.value: 0.60,    # wanted it, not urgently
    SignalSource.NELLIS_PURCHASE.value: 0.50, # proves taste, but you have one
    SignalSource.AMAZON_ORDER.value: 0.50,
    SignalSource.NELLIS_RETURN.value: -1.00,  # you tried it and sent it back
}


class WantItem(Base):
    """Something Logan is actively looking for.

    Distinct from `Watch`, which is a resale hunting rule. A WantItem is a
    personal need: "I want a chest freezer", with what it's worth to him rather
    than what it might resell for.
    """

    __tablename__ = "want_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(200))
    query_key: Mapped[str] = mapped_column(String(256), index=True)

    keywords: Mapped[str | None] = mapped_column(Text, default=None)
    exclude_terms: Mapped[str | None] = mapped_column(Text, default=None)
    category: Mapped[str | None] = mapped_column(String(128), default=None)

    # What it's worth to YOU. Not a resale estimate — the most you'd rationally
    # pay landed rather than buy it new elsewhere.
    max_worth_to_me: Mapped[float | None] = mapped_column(Float, default=None)
    target_discount_vs_retail: Mapped[float] = mapped_column(Float, default=0.5)

    replenishment: Mapped[ReplenishmentClass] = mapped_column(
        Enum(ReplenishmentClass, native_enum=False), default=ReplenishmentClass.DURABLE_MULTI
    )
    # Set by hand when the operator corrects a guess; blocks re-inference.
    replenishment_locked: Mapped[bool] = mapped_column(Boolean, default=False)

    priority: Mapped[float] = mapped_column(Float, default=1.0)
    min_condition: Mapped[str | None] = mapped_column(String(64), default=None)
    accept_damaged: Mapped[bool] = mapped_column(Boolean, default=True)

    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    source: Mapped[str] = mapped_column(String(32), default=SignalSource.MANUAL.value)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_want_active_key", "active", "query_key"),)


class DemandSignal(Base):
    """One observed piece of evidence about what Logan wants or already has.

    Everything imported — Nellis purchases and returns, watchlist entries,
    Amazon cart and saved-for-later, Alexa list lines — lands here in one shape,
    so the scoring never has to care where a signal came from.
    """

    __tablename__ = "demand_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    query_key: Mapped[str] = mapped_column(String(256), index=True)
    title: Mapped[str] = mapped_column(String(512))

    quantity: Mapped[int] = mapped_column(Integer, default=1)
    price_paid: Mapped[float | None] = mapped_column(Float, default=None)
    condition: Mapped[str | None] = mapped_column(String(64), default=None)
    category: Mapped[str | None] = mapped_column(String(128), default=None)
    brand: Mapped[str | None] = mapped_column(String(128), default=None)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    external_id: Mapped[str | None] = mapped_column(String(128), default=None)
    raw: Mapped[dict | None] = mapped_column(JSON, default=None)

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_signal_source_external"),
        Index("ix_signal_key_time", "query_key", "occurred_at"),
    )

    @property
    def intent_weight(self) -> float:
        return SOURCE_INTENT.get(self.source, 0.5)


class WantMatch(Base):
    """A lot the want engine thinks Logan personally wants."""

    __tablename__ = "want_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), index=True)
    want_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("want_items.id", ondelete="SET NULL"), default=None
    )

    interest_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    satiation_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    discount_vs_retail: Mapped[float | None] = mapped_column(Float, default=None)
    landed_at_max: Mapped[float | None] = mapped_column(Float, default=None)
    suggested_max_bid: Mapped[float] = mapped_column(Float, default=0.0)

    matched_via: Mapped[str | None] = mapped_column(String(64), default=None)
    explanation: Mapped[str | None] = mapped_column(Text, default=None)
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("lot_id", "want_item_id", name="uq_want_match"),)


# --------------------------------------------------------------------------
# What this thing actually costs to buy elsewhere, right now
# --------------------------------------------------------------------------


class PriceKind(str, enum.Enum):
    """Whether an observed price is for THIS item or a stand-in for it."""

    EXACT_NEW = "exact_new"        # same item, new — verifies stated retail
    EXACT_USED = "exact_used"      # same item, used/refurb
    SUBSTITUTE_NEW = "sub_new"     # different item, same job, new
    SUBSTITUTE_USED = "sub_used"


class VerificationStatus(str, enum.Enum):
    """How stated retail held up when checked against real listings."""

    VERIFIED = "verified"        # real sellers list it near the stated price
    OVERSTATED = "overstated"    # real price is materially lower
    UNDERSTATED = "understated"  # stated retail is below the real street price
    UNVERIFIED = "unverified"    # nothing found — no claim either way


class MarketPrice(Base):
    """One price observed at a real retailer for a real, buyable item.

    Distinct from `Comp`, which records what something *sold for* second-hand.
    This is what you would pay to get one today, which is the number that decides
    whether bidding makes any sense at all.
    """

    __tablename__ = "market_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query_key: Mapped[str] = mapped_column(String(256), index=True)
    kind: Mapped[PriceKind] = mapped_column(Enum(PriceKind, native_enum=False), index=True)

    source: Mapped[str] = mapped_column(String(48), index=True)  # amazon|walmart|ebay|…
    title: Mapped[str] = mapped_column(String(512))
    price: Mapped[float] = mapped_column(Float)
    shipping: Mapped[float] = mapped_column(Float, default=0.0)
    url: Mapped[str | None] = mapped_column(String(1024), default=None)
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)

    # Quality signals. Without these a $19 knock-off would veto every genuine
    # $500 lot, so a substitute only counts if it's actually comparable.
    rating: Mapped[float | None] = mapped_column(Float, default=None)          # 0..5
    review_count: Mapped[int | None] = mapped_column(Integer, default=None)
    brand: Mapped[str | None] = mapped_column(String(128), default=None)

    similarity: Mapped[float] = mapped_column(Float, default=1.0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw: Mapped[dict | None] = mapped_column(JSON, default=None)

    __table_args__ = (
        Index("ix_market_key_kind", "query_key", "kind", "observed_at"),
    )

    @property
    def total_price(self) -> float:
        return self.price + (self.shipping or 0.0)


# --------------------------------------------------------------------------
# Getting those prices, without an API
# --------------------------------------------------------------------------


class LookupSite(str, enum.Enum):
    """Where to go looking for a price.

    No API keys anywhere in this list. Every one of these is read out of a
    normal, already-logged-in browser session, from a page the operator could
    have opened by hand.
    """

    AMAZON = "amazon"
    WALMART = "walmart"
    FACEBOOK = "facebook"


class LookupStatus(str, enum.Enum):
    PENDING = "pending"
    LEASED = "leased"      # handed to the browser, not yet reported back
    DONE = "done"
    EMPTY = "empty"        # searched fine, found nothing usable
    BLOCKED = "blocked"    # the site asked us to stop; we stopped
    FAILED = "failed"


class LookupJob(Base):
    """One "go look this up" instruction for the browser.

    The queue exists because the alternative — this app fetching retailer pages
    itself — needs credentials nobody will grant and produces a request pattern
    that looks nothing like a person. This way the only thing that ever touches
    Amazon or Facebook is the operator's own browser, at the operator's own
    pace, in the operator's own session, and it stops the moment a site signals
    it would rather we didn't.
    """

    __tablename__ = "lookup_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("lots.id", ondelete="CASCADE"), default=None, index=True
    )
    query_key: Mapped[str] = mapped_column(String(256), index=True)
    query: Mapped[str] = mapped_column(String(256))
    site: Mapped[LookupSite] = mapped_column(Enum(LookupSite, native_enum=False), index=True)
    status: Mapped[LookupStatus] = mapped_column(
        Enum(LookupStatus, native_enum=False), default=LookupStatus.PENDING, index=True
    )

    # Priority is "how much does the answer change what we'd do" — a lot closing
    # in an hour with $400 on the table outranks idle curiosity.
    priority: Mapped[float] = mapped_column(Float, default=0.0, index=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    recorded: Mapped[int] = mapped_column(Integer, default=0)  # prices actually stored
    note: Mapped[str | None] = mapped_column(String(512), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    lot: Mapped[Lot | None] = relationship("Lot")

    __table_args__ = (
        Index("ix_lookup_status_priority", "status", "priority"),
        Index("ix_lookup_site_finished", "site", "finished_at"),
    )
