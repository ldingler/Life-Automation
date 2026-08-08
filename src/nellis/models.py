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
