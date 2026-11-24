"""Private-chat ordering, receipts, and staff fulfillment controls."""
from dataclasses import asdict
from decimal import Decimal
import logging
from uuid import UUID

from telebot import types

from bot.keyboards import HELP, MANAGE_ORDERS, MY_ORDERS, SHOP, UPLOAD_ARTWORK, actions, shop_keyboard
from bot.webapp_data import parse_webapp_data, normalize_phone, PayloadValidationError
from config import settings
from services import database as db, storage
from services.notifications import deliver_next
from services.pricing import calculate_order, LABELS


ARTWORK_INSTRUCTIONS = "Attach your original PNG, JPEG or plain SVG (up to 8 MiB). Tap the paperclip and choose File."
PHONE_PROMPT = "Update contact number for order "


def status_label(order):
    status = order["status"]
    if status == "deposit_confirmed" and order.get("deposit_paid") is None:
        return "Verify historical deposit"
    if status == "out_for_delivery":
        return "Ready for pickup" if order["delivery_method"] == "pickup" else "Dispatched"
    if status == "completed":
        return "Collected" if order["delivery_method"] == "pickup" else "Delivered"
    return status.replace("_", " ").capitalize()


def notification_event(order):
    if order["status"] == "out_for_delivery" and order["delivery_method"] == "pickup":
        return "ready_for_pickup"
    if order["status"] == "completed":
        return "collected" if order["delivery_method"] == "pickup" else "delivered"
    return order["status"]


def next_status(order):
    if order.get("deposit_paid") is not True:
        return None
    return {"deposit_confirmed": "printing", "printing": "out_for_delivery",
            "out_for_delivery": "completed"}.get(order["status"])


def summary(order):
    lines = [f"Order #{order['id'][:8]} · {status_label(order)}"]
    for item in order["items"]:
        name = LABELS.get(item["size"], item["size"])
        color = f" ({item['keyboard_color']})" if item.get("keyboard_color") else ""
        lines.append(f"{name}{color} · {item['quantity']} × {item['unit_price']} ETB = {item['line_total']} ETB")
    total, deposit = (Decimal(str(order[key])) for key in ("total", "deposit_amount"))
    lines.extend([f"Subtotal · {order['subtotal']} ETB",
                  f"Discount · {order['discount_percent']}% ({Decimal(str(order['subtotal'])) - total:.2f} ETB)",
                  f"Total · {total:.2f} ETB"])
    paid = order.get("deposit_paid")
    deposit_label = {True: "received", False: "due", None: "awaiting reconciliation"}[paid]
    lines.append(f"Deposit {deposit_label} · {deposit:.2f} ETB")
    if order.get("balance_received_at"):
        lines.append(f"Balance received · {total - deposit:.2f} ETB ({order.get('balance_method') or 'recorded'})")
    elif order["status"] == "completed":
        lines.append("Final payment · historical record requires reconciliation")
    else:
        lines.append(f"Balance at handover · {total - deposit:.2f} ETB")
    lines += [f"Contact · {order['customer_phone']}",
              "Pickup · " + settings.pickup_instructions if order["delivery_method"] == "pickup"
              else f"Delivery · {order.get('delivery_address') or 'Confirm address with customer'}"]
    return "\n".join(lines)


def register_handlers(bot):
    def send(chat, text, rows=()):
        bot.send_message(chat, text, reply_markup=actions(rows), disable_web_page_preview=True)

    def send_menu(message, text, upload_id=None):
        keyboard = shop_keyboard(staff=message.from_user.id in settings.admin_telegram_ids, upload_id=upload_id)
        bot.send_message(message.chat.id, text, reply_markup=keyboard)

    def private(message):
        if message.chat.type == "private":
            return True
        send(message.chat.id, "Open a private chat with the bot to manage sticker orders.")
        return False

    def staff(message):
        if not private(message):
            return False
        if message.from_user.id in settings.admin_telegram_ids:
            return True
        send(message.chat.id, "This command is for shop staff.")
        return False

    def receipt(chat, order, admin=False):
        oid = order["id"]
        rows = []
        note = ""
        if admin:
            try:
                url = storage.print_url(order)
            except Exception as exc:
                logging.warning("Print URL unavailable for order %s (%s)", oid, type(exc).__name__)
                url = None
            if url:
                rows.append(("Open print artwork (15 minutes)", "url", url))
            else:
                note = "\nOriginal artwork requires staff review."
            note += f"\nFull order ID · {oid}"
            delivery = db.notification_status(oid, notification_event(order))
            if delivery:
                note += f"\nCustomer notification · {delivery}"
            if order.get("deposit_paid") is not True:
                rows.append(("Confirm deposit received", "callback_data", f"deposit:{oid}"))
            target = next_status(order)
            if target == "completed":
                handover = "collection" if order["delivery_method"] == "pickup" else "delivery"
                note += f"\nConfirm {handover} after receiving the balance."
                for method in ("cash", "transfer", "other"):
                    rows.append((f"Confirm {handover} · paid by {method}", "callback_data", f"settle:{oid}:{method}"))
            elif target:
                caption = {"printing": "Start printing", "out_for_delivery": "Ready for pickup" if order["delivery_method"] == "pickup" else "Dispatch"}[target]
                rows.append((caption, "callback_data", f"advance:{oid}:{target}"))
        elif order["status"] == "pending_deposit":
            note = "\nThe shop will contact you at the number above to arrange your deposit."
            rows.append(("Update phone number", "callback_data", f"phone:{oid}"))
        send(chat, summary(order) + note, rows)

    help_text = (f"Tap {SHOP} to see sizes and prices. {ARTWORK_INSTRUCTIONS} "
                 "Once we receive the file, tap Configure stickers to choose sizes, quantities and pickup or delivery. "
                 f"Tap {MY_ORDERS} to see your receipts and order status.")

    @bot.message_handler(commands=["start", "help"])
    def start(message):
        if private(message):
            send_menu(message, help_text)

    @bot.message_handler(func=lambda message: message.text == HELP)
    def help_message(message):
        if private(message):
            send(message.chat.id, help_text)

    @bot.message_handler(func=lambda message: message.text == UPLOAD_ARTWORK)
    def prompt_artwork(message):
        if private(message):
            send(message.chat.id, ARTWORK_INSTRUCTIONS)

    @bot.message_handler(content_types=["photo"])
    def photo(message):
        prompt_artwork(message)

    @bot.message_handler(content_types=["document"])
    def document(message):
        if not private(message):
            return
        doc = message.document
        mime = doc.mime_type
        if mime not in storage.EXTENSIONS:
            send(message.chat.id, "Choose a PNG, JPEG or plain SVG and send it as a file.")
            return
        try:
            data = storage.validate_image(storage.download_file(bot, doc), mime)
        except storage.ArtworkError as exc:
            send(message.chat.id, str(exc))
            return
        upload = db.register_upload(message.from_user.id, message.message_id, storage.EXTENSIONS[mime])
        if upload["deleting"] or upload.get("ordered"):
            send(message.chat.id, f"Send the file again for a new order, or tap {MY_ORDERS}.")
            return
        storage.store(upload, data, mime)
        send_menu(message, "Artwork received. Tap Configure stickers to continue.", upload["id"])

    @bot.message_handler(content_types=["web_app_data"])
    def submission(message):
        if not private(message):
            return
        data = message.web_app_data.data
        if data == "upload-artwork":
            prompt_artwork(message)
            return
        if data == "view-orders":
            my_orders(message)
            return
        try:
            payload = parse_webapp_data(data)
        except PayloadValidationError as exc:
            send(message.chat.id, f"{exc} Reopen Configure stickers to edit. For an older form, resend the artwork.")
            return
        uid = message.from_user.id
        order = db.order_by("upload_id", payload.upload_id)
        if not order:
            upload = db.active_upload(payload.upload_id)
            if not upload or upload["owner_id"] != uid:
                send(message.chat.id, "This artwork is unavailable. Send your file again to open a new form.")
                return
            calculation = calculate_order([(i.size, i.quantity) for i in payload.items])
            items = [dict(asdict(item), unit_price=str(line.unit_price), line_total=str(line.line_total))
                     for item, line in zip(payload.items, calculation.line_items)]
            order = db.save_order({
                "telegram_user_id": uid, "telegram_chat_id": message.chat.id,
                "upload_id": upload["id"], "image_url": storage.object_path(upload),
                "items": items, "subtotal": str(calculation.subtotal), "discount_percent": calculation.discount_percent,
                "total": str(calculation.total), "deposit_amount": str(calculation.deposit),
                "delivery_method": payload.delivery_method, "delivery_address": payload.delivery_address,
                "customer_phone": payload.customer_phone, "status": "pending_deposit", "deposit_paid": False,
            })
        if order["telegram_user_id"] != uid:
            send(message.chat.id, "Please send your own artwork to start an order.")
            return
        send_menu(message, f"Order saved. Find it anytime under {MY_ORDERS}.")
        receipt(message.chat.id, order)

    def queue(chat, page, owner=None):
        rows = db.list_orders(page, owner)
        mode = "staff" if owner is None else "mine"
        buttons = [(f"#{o['id'][:8]} · {status_label(o)} · {o['total']} ETB",
                    "callback_data", f"{mode}:{o['id']}") for o in rows[:db.PAGE_SIZE]]
        if page:
            buttons.append(("Previous page", "callback_data", f"{mode}page:{page-1}"))
        if len(rows) > db.PAGE_SIZE:
            buttons.append(("Next page", "callback_data", f"{mode}page:{page+1}"))
        title = "Active orders" if owner is None else "My orders"
        send(chat, f"{title} · Page {page + 1}" if rows else f"{title} · No orders here.", buttons)

    def update_phone(message, order_id, raw):
        try:
            order_id = str(UUID(order_id))
            phone = normalize_phone(raw)
        except PayloadValidationError as exc:
            send(message.chat.id, f"{exc} Tap Update phone number to try again.")
            return
        except (ValueError, TypeError):
            send(message.chat.id, f"Open {MY_ORDERS} and tap Update phone number on your receipt.")
            return
        order = db.order_by("id", order_id)
        updated = db.correct_contact_phone(order, message.from_user.id, phone) if order else None
        if updated:
            receipt(message.chat.id, updated)
        else:
            send(message.chat.id, f"This order is unavailable for phone changes. Open {MY_ORDERS}.")

    def phone_reply_order(message):
        reply = message.reply_to_message
        if reply and reply.from_user and str(reply.from_user.id) == bot.token.partition(":")[0]:
            text = reply.text or ""
            if text.startswith(PHONE_PROMPT):
                return text.partition("\n")[0].removeprefix(PHONE_PROMPT)
        return None

    @bot.message_handler(func=lambda message: phone_reply_order(message) is not None)
    def phone_reply(message):
        if private(message):
            update_phone(message, phone_reply_order(message), message.text)

    @bot.message_handler(commands=["phone"])
    def correct_phone(message):
        if private(message):
            parts = message.text.split(maxsplit=2)
            if len(parts) == 3:
                update_phone(message, parts[1], parts[2])
            else:
                send(message.chat.id, f"Open {MY_ORDERS} and tap Update phone number on your receipt.")

    @bot.message_handler(func=lambda message: message.text == MANAGE_ORDERS)
    @bot.message_handler(commands=["orders"])
    def orders(message):
        if staff(message):
            queue(message.chat.id, 0)

    @bot.message_handler(func=lambda message: message.text == MY_ORDERS)
    @bot.message_handler(commands=["myorders"])
    def my_orders(message):
        if private(message):
            queue(message.chat.id, 0, message.from_user.id)

    def advance(chat, order, target, admin, balance_method=None):
        if target != next_status(order) or not db.advance_order(order, target, admin, balance_method):
            send(chat, f"This change is unavailable. Open {MANAGE_ORDERS} for the current stage.")
            return
        deliver_next(bot, order["id"])
        delivery = db.notification_status(order["id"], notification_event(dict(order, status=target)))
        feedback = {"delivered": "Customer notified.", "failed": "Notification failed. Contact the customer by phone."}.get(
            delivery, "Customer notification queued.")
        send(chat, f"Order #{order['id'][:8]} updated. {feedback}")

    @bot.message_handler(commands=["order", "status"])
    def order_command(message):
        if not staff(message):
            return
        parts = message.text.split()
        change = parts[0].split("@")[0] == "/status"
        try:
            oid = str(UUID(parts[1]))
        except (ValueError, IndexError):
            send(message.chat.id, f"Choose an order from {MANAGE_ORDERS} or supply its full UUID.")
            return
        order = db.order_by("id", oid)
        if not order:
            send(message.chat.id, "Order not found.")
        elif not change:
            receipt(message.chat.id, order, admin=True)
        elif len(parts) == 3 and parts[2] != "completed":
            advance(message.chat.id, order, parts[2], message.from_user.id)
        else:
            receipt(message.chat.id, order, admin=True)

    @bot.callback_query_handler(func=lambda call: True)
    def callback(call):
        uid = call.from_user.id
        message = call.message
        if not message or message.chat.type != "private" or message.chat.id != uid:
            bot.answer_callback_query(call.id, "Open a private chat.")
            return
        parts = (call.data or "").split(":")
        # Payment buttons on older messages reopen the appropriate receipt.
        action = {"pay": "mine", "checkpay": "mine", "reconcile": "staff"}.get(parts[0], parts[0])
        admin = action in ("staff", "staffpage", "advance", "settle", "deposit")
        if admin and uid not in settings.admin_telegram_ids:
            bot.answer_callback_query(call.id, "Staff access required.")
            return
        try:
            bot.answer_callback_query(call.id)
        except Exception as exc:
            if getattr(exc, "error_code", None) != 400:
                raise
        if action in ("staffpage", "minepage") and len(parts) == 2 and parts[1].isdigit():
            queue(uid, int(parts[1]), None if admin else uid)
            return
        if action not in ("mine", "staff", "advance", "settle", "deposit", "phone") or len(parts) < 2:
            return
        try:
            oid = str(UUID(parts[1]))
        except ValueError:
            return
        order = db.order_by("id", oid)
        if not order or (not admin and order["telegram_user_id"] != uid):
            send(uid, "Order unavailable.")
        elif action == "phone" and len(parts) == 2:
            if order["status"] != "pending_deposit":
                send(uid, f"This order is unavailable for phone changes. Open {MY_ORDERS}.")
                return
            bot.send_message(uid, f"{PHONE_PROMPT}{oid}\nReply with your 09, 07 or +251 phone number.",
                             reply_markup=types.ForceReply(input_field_placeholder="Your phone number"))
        elif action == "deposit" and len(parts) == 2:
            if order.get("deposit_paid") is True:
                send(uid, "Deposit already recorded.")
                return
            updated = db.confirm_deposit(order)
            if not updated:
                send(uid, f"This order has changed. Open {MANAGE_ORDERS} for its current status.")
                return
            deliver_next(bot, oid)
            receipt(uid, updated, admin=True)
        elif action == "settle" and len(parts) == 3 and parts[2] in ("cash", "transfer", "other"):
            advance(uid, order, "completed", uid, parts[2])
        elif action == "advance" and len(parts) == 3 and parts[2] != "completed":
            advance(uid, order, parts[2], uid)
        else:
            receipt(uid, order, admin=admin)
