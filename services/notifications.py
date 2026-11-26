"""Customer and staff Telegram notifications."""
import logging
from config import settings
from services import database as db

CUSTOMER_TEXT = {
    "deposit_confirmed": "Deposit received! Your order is confirmed and queued for printing.",
    "printing": "Your stickers are now being printed.",
    "ready_for_pickup": "Your stickers are ready for pickup!",
    "out_for_delivery": "Your stickers have been dispatched for delivery.",
    "collected": "Collection and final payment recorded. Thank you for ordering!",
    "delivered": "Delivery and final payment recorded. Thank you for ordering!",
}


def notification_event(order: dict) -> str:
    status = order["status"]
    if status == "out_for_delivery" and order["delivery_method"] == "pickup":
        return "ready_for_pickup"
    if status == "completed":
        return "collected" if order["delivery_method"] == "pickup" else "delivered"
    return status


def notify_customer(bot, order: dict) -> str:
    event = notification_event(order)
    text = CUSTOMER_TEXT.get(event)
    if not text:
        return "skipped"
    if event == "ready_for_pickup":
        text += "\n" + settings.pickup_instructions
    try:
        bot.send_message(order["telegram_chat_id"], f"Order #{order['id'][:8]} · {text}")
        return "delivered"
    except Exception as exc:
        logging.warning("Customer notification failed for %s (%s)", order["id"], type(exc).__name__)
        return "failed"


def notify_staff_new_order(bot, order: dict) -> None:
    from telebot import types

    oid = order["id"]
    short_id = oid[:8]
    name = order.get("customer_name") or "Customer"
    uid = order["telegram_user_id"]
    handover = (
        f"delivery ({order.get('delivery_address') or 'no address'})"
        if order["delivery_method"] == "delivery"
        else "pickup"
    )
    sticker_lines = [
        f"• {it.get('label', it['size'])} · {it['size']} × {it['quantity']}"
        + (f" ({it['keyboard_color']})" if it.get("keyboard_color") else "")
        for it in order["items"]
    ]
    text = (
        f"New sticker order #{short_id}\n"
        f"Name · {name} (ID: {uid})\n"
        f"Phone · {order['customer_phone']}\n"
        f"Handover · {handover}\n"
        f"Total · {order['total']} ETB (Deposit: {order['deposit_amount']} ETB)\n\n"
        f"Stickers:\n" + "\n".join(sticker_lines)
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Confirm deposit received", callback_data=f"deposit:{oid}"))
    markup.add(types.InlineKeyboardButton("View Order", callback_data=f"staff:{oid}"))

    batch_items = db.list_batch_items(order["batch_id"]) if order.get("batch_id") else []
    batch_by_id = {it["id"]: it for it in batch_items}

    for admin_id in settings.admin_telegram_ids:
        try:
            bot.send_message(admin_id, text, reply_markup=markup)
            for it in order["items"]:
                if it.get("kind") == "upload":
                    up = batch_by_id.get(it["ref_id"])
                    if up:
                        bot.send_document(
                            admin_id,
                            up["file_id"],
                            caption=f"Order #{short_id} · {name} · {it['size']} × {it['quantity']}",
                        )
                elif it.get("kind") == "template":
                    tpl = db.get_template(it["ref_id"])
                    if tpl and tpl.get("sample_file_id"):
                        bot.send_photo(
                            admin_id,
                            tpl["sample_file_id"],
                            caption=f"Order #{short_id} · {name} · Template: {tpl['title']} ({it['size']} × {it['quantity']})",
                        )
        except Exception as exc:
            logging.warning("Staff notification failed for admin %s (%s)", admin_id, type(exc).__name__)


def notify_staff_payment_screenshot(bot, order: dict, file_id: str, *, is_photo: bool = True) -> None:
    from telebot import types

    oid = order["id"]
    caption = (
        f"Payment screenshot for Order #{oid[:8]}\n"
        f"Name: {order.get('customer_name') or 'Customer'} (ID: {order['telegram_user_id']})\n"
        f"Phone: {order['customer_phone']}\n"
        f"Deposit: {order['deposit_amount']} ETB / Total: {order['total']} ETB"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Confirm deposit received", callback_data=f"deposit:{oid}"))
    markup.add(types.InlineKeyboardButton("View Order", callback_data=f"staff:{oid}"))
    for admin_id in settings.admin_telegram_ids:
        try:
            if is_photo:
                bot.send_photo(admin_id, file_id, caption=caption, reply_markup=markup)
            else:
                bot.send_document(admin_id, file_id, caption=caption, reply_markup=markup)
        except Exception as exc:
            logging.warning("Staff payment screenshot failed for admin %s (%s)", admin_id, type(exc).__name__)
