"""Telegram reply and inline keyboards for button-driven ordering."""
from telebot import types

BROWSE_TEMPLATES = "🎨 Browse Templates"
UPLOAD_ARTWORK = "📤 Upload Stickers"
MY_CART = "🛒 My Cart"
MY_ORDERS = "📦 My Orders"
HELP = "❓ Help"
MANAGE_ORDERS = "🛠 Manage Orders"
ADMIN_PANEL = "⚙️ Admin Panel"


def main_menu_keyboard(*, staff: bool = False):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    keyboard.row(BROWSE_TEMPLATES, UPLOAD_ARTWORK)
    keyboard.row(MY_CART, MY_ORDERS)
    if staff:
        keyboard.row(MANAGE_ORDERS, ADMIN_PANEL)
        keyboard.row(HELP)
    else:
        keyboard.row(HELP)
    return keyboard


def phone_share_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add(types.KeyboardButton("Share my phone number", request_contact=True))
    return keyboard


def location_share_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add(types.KeyboardButton("📍 Share my delivery location", request_location=True))
    return keyboard


def actions(rows, cols: int = 1):
    if not rows:
        return None
    keyboard = types.InlineKeyboardMarkup(row_width=cols)
    buttons = [types.InlineKeyboardButton(text, **{key: value}) for text, key, value in rows]
    keyboard.add(*buttons)
    return keyboard
