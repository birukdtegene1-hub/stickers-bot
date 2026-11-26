"""Dynamic pricing engine for sticker orders (all monetary values in ETB)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

from services import database as db

BASE_PRICES: dict[str, Decimal] = {
    "20x20": Decimal("40"),
    "40x40": Decimal("60"),
    "60x60": Decimal("80"),
    "spacebar_key": Decimal("80"),
    "phone_back": Decimal("80"),
}

LABELS = {
    "20x20": "20 × 20 mm",
    "40x40": "40 × 40 mm",
    "60x60": "60 × 60 mm",
    "spacebar_key": "Spacebar key",
    "phone_back": "Phone back",
}

MAX_QUANTITY = 999
COLORS = ("black", "white", "silver", "pink", "blue")
MAX_ADDRESS = 300


@dataclass(frozen=True)
class LineItem:
    size: str
    unit_price: Decimal
    quantity: int
    line_total: Decimal


@dataclass(frozen=True)
class OrderCalculation:
    line_items: list[LineItem]
    subtotal: Decimal
    total_quantity: int
    discount_percent: int
    discount_amount: Decimal
    total: Decimal
    deposit: Decimal


class PricingError(Exception):
    pass


def size_prices_map(*, include_inactive: bool = False) -> dict[str, Decimal]:
    rows = db.list_sizes(include_inactive=include_inactive)
    if not rows:
        return dict(BASE_PRICES)
    return {r["key"]: Decimal(str(r["price_etb"])) for r in rows}


def size_labels_map(*, include_inactive: bool = True) -> dict[str, str]:
    rows = db.list_sizes(include_inactive=include_inactive)
    merged = dict(LABELS)
    for r in rows:
        merged[r["key"]] = r["label"]
    return merged


def get_deposit_fraction() -> Decimal:
    raw = db.get_setting("deposit_percent", "50")
    try:
        pct = Decimal(raw)
        if 0 <= pct <= 100:
            return pct / Decimal("100")
    except Exception:
        pass
    return Decimal("0.50")


def _resolve_discount(total_quantity: int) -> int:
    tiers = db.list_discount_tiers()
    for threshold, percent in tiers:
        if total_quantity >= threshold:
            return percent
    return 0


def calculate_order(
    items: Sequence[tuple[str, int] | tuple[str, int, Decimal | str | None]],
) -> OrderCalculation:
    """Compute pricing for (size, quantity) or (size, quantity, custom_unit_price) tuples."""
    if not items:
        raise PricingError("Order must contain at least one item.")

    prices = size_prices_map(include_inactive=True)
    line_items: list[LineItem] = []
    subtotal = Decimal("0")
    total_quantity = 0

    for entry in items:
        if len(entry) == 2:
            size, quantity = entry
            custom_price = None
        else:
            size, quantity, custom_price = entry

        if size not in prices and custom_price is None:
            raise PricingError(f"Unknown size '{size}'.")
        if quantity < 1:
            raise PricingError(f"Quantity must be at least 1, got {quantity}.")

        unit_price = Decimal(str(custom_price)) if custom_price is not None else prices[size]
        line_total = unit_price * quantity
        line_items.append(LineItem(size=size, unit_price=unit_price, quantity=quantity, line_total=line_total))
        subtotal += line_total
        total_quantity += quantity

    discount_percent = _resolve_discount(total_quantity)
    discount_amount = (subtotal * discount_percent / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total = (subtotal - discount_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    deposit = (total * get_deposit_fraction()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return OrderCalculation(
        line_items=line_items,
        subtotal=subtotal,
        total_quantity=total_quantity,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        total=total,
        deposit=deposit,
    )
