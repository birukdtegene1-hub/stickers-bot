from urllib.parse import urlencode
from telebot import types
from config import settings


SHOP = "Open sticker shop"
UPLOAD_ARTWORK = "Upload artwork"
MY_ORDERS = "My orders"
HELP = "Help"
MANAGE_ORDERS = "Manage orders"


def shop_keyboard(*, staff=False, upload_id=None):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    if upload_id:
        url = settings.webapp_url + "?" + urlencode({"draft": upload_id})
        keyboard.add(types.KeyboardButton("Configure stickers", web_app=types.WebAppInfo(url=url)))
    keyboard.add(types.KeyboardButton(SHOP, web_app=types.WebAppInfo(url=settings.webapp_url)))
    keyboard.row(UPLOAD_ARTWORK, MY_ORDERS)
    if staff:
        keyboard.row(HELP, MANAGE_ORDERS)
    else:
        keyboard.row(HELP)
    return keyboard


def actions(rows):
    keyboard = types.InlineKeyboardMarkup()
    for text, key, value in rows:
        keyboard.add(types.InlineKeyboardButton(text, **{key: value}))
    return keyboard if rows else None
