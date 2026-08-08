"""Resale channel models: gross comp value -> net proceeds in your pocket.

A $180 eBay comp is not $180. After final-value fees, shipping you eat, and
packaging, it is closer to $135. Comparing a Nellis landed cost against a raw
comp price is the single most common way to talk yourself into a bad deal.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResaleChannel:
    key: str
    label: str
    fee_rate: float           # final value fee on (item + shipping)
    fixed_fee: float          # per-order fixed fee
    seller_pays_shipping: bool
    packaging_cost: float
    typical_days_to_sell: int
    notes: str = ""


CHANNELS: dict[str, ResaleChannel] = {
    "ebay": ResaleChannel(
        key="ebay",
        label="eBay",
        fee_rate=0.1325,
        fixed_fee=0.40,
        seller_pays_shipping=True,
        packaging_cost=2.50,
        typical_days_to_sell=21,
        notes="Widest reach, highest friction. Fees ~13% + shipping you usually absorb.",
    ),
    "local": ResaleChannel(
        key="local",
        label="Facebook Marketplace / Craigslist (local pickup)",
        fee_rate=0.0,
        fixed_fee=0.0,
        seller_pays_shipping=False,
        packaging_cost=0.0,
        typical_days_to_sell=14,
        notes="No fees, no shipping. Best for bulky/heavy items. Slower and manual.",
    ),
    "fb_shipped": ResaleChannel(
        key="fb_shipped",
        label="Facebook Marketplace (shipped)",
        fee_rate=0.10,
        fixed_fee=0.40,
        seller_pays_shipping=True,
        packaging_cost=2.00,
        typical_days_to_sell=18,
    ),
    "offerup": ResaleChannel(
        key="offerup",
        label="OfferUp (shipped)",
        fee_rate=0.129,
        fixed_fee=0.0,
        seller_pays_shipping=True,
        packaging_cost=2.00,
        typical_days_to_sell=20,
    ),
    "mercari": ResaleChannel(
        key="mercari",
        label="Mercari",
        fee_rate=0.10,
        fixed_fee=0.50,
        seller_pays_shipping=True,
        packaging_cost=2.00,
        typical_days_to_sell=20,
    ),
}

DEFAULT_CHANNEL = "ebay"


def get_channel(key: str | None) -> ResaleChannel:
    return CHANNELS.get((key or DEFAULT_CHANNEL).lower(), CHANNELS[DEFAULT_CHANNEL])


def estimate_shipping_cost(weight_lb: float | None, category: str | None = None) -> float:
    """Rough USPS/UPS ground estimate. Weight beats category when known."""
    if weight_lb is None:
        heavy = {"appliances", "furniture", "outdoor", "exercise", "tools"}
        if category and category.strip().lower() in heavy:
            return 18.00
        return 9.50
    if weight_lb <= 1:
        return 5.00
    if weight_lb <= 5:
        return 9.50
    if weight_lb <= 10:
        return 14.00
    if weight_lb <= 20:
        return 22.00
    if weight_lb <= 50:
        return 38.00
    return 65.00


@dataclass(frozen=True)
class NetProceeds:
    gross: float
    fees: float
    shipping: float
    packaging: float
    parts_cost: float
    labor_cost: float

    @property
    def net(self) -> float:
        return max(
            0.0,
            self.gross
            - self.fees
            - self.shipping
            - self.packaging
            - self.parts_cost
            - self.labor_cost,
        )

    def breakdown(self) -> dict[str, float]:
        return {
            "gross_comp_value": round(self.gross, 2),
            "marketplace_fees": round(self.fees, 2),
            "shipping": round(self.shipping, 2),
            "packaging": round(self.packaging, 2),
            "replacement_parts": round(self.parts_cost, 2),
            "labor": round(self.labor_cost, 2),
            "net_proceeds": round(self.net, 2),
        }


def net_proceeds(
    gross: float,
    *,
    channel: ResaleChannel,
    weight_lb: float | None = None,
    category: str | None = None,
    parts_cost: float = 0.0,
    labor_hours: float = 0.0,
    labor_rate: float = 25.0,
    shipping_override: float | None = None,
) -> NetProceeds:
    """What you actually keep from a sale at `gross`."""
    if gross <= 0:
        return NetProceeds(0.0, 0.0, 0.0, 0.0, parts_cost, labor_hours * labor_rate)

    shipping = 0.0
    if channel.seller_pays_shipping:
        shipping = (
            shipping_override
            if shipping_override is not None
            else estimate_shipping_cost(weight_lb, category)
        )

    # Marketplaces charge their fee on the buyer's total, shipping included.
    fees = (gross + shipping) * channel.fee_rate + channel.fixed_fee

    return NetProceeds(
        gross=gross,
        fees=fees,
        shipping=shipping,
        packaging=channel.packaging_cost,
        parts_cost=parts_cost,
        labor_cost=labor_hours * labor_rate,
    )
