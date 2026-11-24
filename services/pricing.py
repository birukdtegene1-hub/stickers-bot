"""Pricing engine for sticker orders.

Encapsulates base prices per size and tiered volume discounts.
All monetary values are in Ethiopian Birr (ETB).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

# Base price per unit, keyed by canonical size identifier.
BASE_PRICES: dict[str, Decimal] = {
    "20x20": Decimal("40"),
    "40x40": Decimal("60"),
    "60x60": Decimal("80"),
    "spacebar_key": Decimal("80"),
    "phone_back": Decimal("80"),
}

# Discount tiers ordered from highest threshold to lowest.
# Each tuple is (minimum_quantity, discount_percentage).
_DISCOUNT_TIERS: list[tuple[int, int]] = [
    (20, 15),
    (10, 10),
    (5, 5),
    (1, 0),
]

DEPOSIT_FRACTION = Decimal("0.50")


@dataclass(frozen=True)
class LineItem:
    """Computed pricing for a single line in the order."""

    size: str
    unit_price: Decimal
    quantity: int
    line_total: Decimal


@dataclass(frozen=True)
class OrderCalculation:
    """Full pricing breakdown for an order."""

    line_items: list[LineItem]
    subtotal: Decimal
    total_quantity: int
    discount_percent: int
    discount_amount: Decimal
    total: Decimal
    deposit: Decimal


class PricingError(Exception):
    """Raised when pricing cannot be computed due to invalid input."""


def _resolve_discount(total_quantity: int) -> int:
    """Return the discount percentage for a given total item count."""
    for threshold, percent in _DISCOUNT_TIERS:
        if total_quantity >= threshold:
            return percent
    return 0


def calculate_order(
    items: Sequence[tuple[str, int]],
) -> OrderCalculation:
    """Compute full pricing for a list of (size, quantity) pairs.

    Parameters
    ----------
    items:
        Each element is a (size_key, quantity) pair. The size_key must exist
        in BASE_PRICES. Quantity must be a positive integer.

    Returns
    -------
    OrderCalculation with all monetary fields rounded to 2 decimal places.

    Raises
    ------
    PricingError
        If a size key is unrecognized or a quantity is not positive.
    """
    if not items:
        raise PricingError("Order must contain at least one item.")

    line_items: list[LineItem] = []
    subtotal = Decimal("0")
    total_quantity = 0

    for size, quantity in items:
        if size not in BASE_PRICES:
            raise PricingError(
                f"Unknown size '{size}'. "
                f"Valid sizes: {', '.join(sorted(BASE_PRICES.keys()))}."
            )
        if quantity < 1:
            raise PricingError(f"Quantity must be at least 1, got {quantity} for size '{size}'.")

        unit_price = BASE_PRICES[size]
        line_total = unit_price * quantity
        line_items.append(
            LineItem(size=size, unit_price=unit_price, quantity=quantity, line_total=line_total)
        )
        subtotal += line_total
        total_quantity += quantity

    discount_percent = _resolve_discount(total_quantity)
    discount_amount = (subtotal * discount_percent / 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    total = (subtotal - discount_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    deposit = (total * DEPOSIT_FRACTION).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return OrderCalculation(
        line_items=line_items,
        subtotal=subtotal,
        total_quantity=total_quantity,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        total=total,
        deposit=deposit,
    )


LABELS = {
    "20x20": "20 × 20 mm", "40x40": "40 × 40 mm", "60x60": "60 × 60 mm",
    "spacebar_key": "Spacebar key", "phone_back": "Phone back",
}
MAX_QUANTITY = 999
COLORS = ("black", "white", "silver", "pink", "blue")
MAX_ADDRESS = 300


def catalog():
    return {
        "products": [{"key": key, "name": LABELS[key], "cents": int(price * 100)}
                     for key, price in BASE_PRICES.items()],
        "discounts": _DISCOUNT_TIERS,
        "depositPercent": int(DEPOSIT_FRACTION * 100),
        "maxQuantity": MAX_QUANTITY, "colors": COLORS, "maxAddress": MAX_ADDRESS,
    }
