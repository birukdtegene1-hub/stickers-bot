"""Validate the untrusted Mini App order before using any of its fields."""
import json
import re
from dataclasses import dataclass
from uuid import UUID

from services.pricing import BASE_PRICES, COLORS, MAX_ADDRESS, MAX_QUANTITY


class PayloadValidationError(ValueError):
    pass


@dataclass(frozen=True)
class StickerItem:
    size: str
    quantity: int
    keyboard_color: str | None


@dataclass(frozen=True)
class OrderPayload:
    items: list[StickerItem]
    delivery_method: str
    customer_phone: str
    upload_id: str
    delivery_address: str | None


def normalize_phone(value):
    number = re.sub(r"[ ()-]", "", value.strip()) if isinstance(value, str) else ""
    if number.startswith("+251"):
        number = "0" + number[4:]
    if not re.fullmatch(r"0[79][0-9]{8}", number):
        raise PayloadValidationError("Enter a ten-digit 09 or 07 phone number, or use +251.")
    return number


def parse_webapp_data(raw_json):
    if not isinstance(raw_json, str) or len(raw_json.encode("utf-8")) > 4096:
        raise PayloadValidationError("Shorten the order details and try again.")
    try:
        data = json.loads(raw_json)
        upload_id = str(UUID(data["upload_id"]))
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise PayloadValidationError("Open the order form from your uploaded file.") from exc
    rows = data.get("items")
    if not isinstance(rows, list) or not 1 <= len(rows) <= len(BASE_PRICES):
        raise PayloadValidationError("Select at least one sticker size.")
    items = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PayloadValidationError("Choose a size for each item.")
        size, qty, color = row.get("size"), row.get("quantity"), row.get("keyboard_color")
        if not isinstance(size, str) or size not in BASE_PRICES or size in seen:
            raise PayloadValidationError("Choose each sticker size once.")
        seen.add(size)
        if type(qty) is not int or not 1 <= qty <= MAX_QUANTITY:
            raise PayloadValidationError(f"Enter a quantity between 1 and {MAX_QUANTITY}.")
        if size == "spacebar_key" and color not in COLORS:
            raise PayloadValidationError("Choose a spacebar color.")
        if size != "spacebar_key" and color is not None:
            raise PayloadValidationError("Only spacebar stickers have a keyboard color.")
        items.append(StickerItem(size, qty, color))
    method = data.get("delivery_method")
    if method not in ("pickup", "delivery"):
        raise PayloadValidationError("Choose pickup or delivery.")
    address = data.get("delivery_address")
    if method == "delivery" and (not isinstance(address, str) or not 1 <= len(address.strip()) <= MAX_ADDRESS):
        raise PayloadValidationError(f"Enter a delivery address of up to {MAX_ADDRESS} characters.")
    return OrderPayload(items, method, normalize_phone(data.get("customer_phone")), upload_id,
                        address.strip() if method == "delivery" else None)
