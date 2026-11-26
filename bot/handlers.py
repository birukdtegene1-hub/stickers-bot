"""Button-driven Telegram sticker ordering, registration, templates, and Admin CRUD."""
from decimal import Decimal, InvalidOperation
import logging
import re
import threading
from uuid import UUID, uuid4

from telebot import types

from bot.keyboards import (
    ADMIN_PANEL,
    BROWSE_TEMPLATES,
    HELP,
    MANAGE_ORDERS,
    MY_CART,
    MY_ORDERS,
    UPLOAD_ARTWORK,
    actions,
    location_share_keyboard,
    main_menu_keyboard,
    phone_share_keyboard,
)
from config import settings
from services import database as db, storage
from services.notifications import notify_customer, notify_staff_new_order, notify_staff_payment_screenshot
from services.pricing import (
    COLORS,
    MAX_ADDRESS,
    calculate_order,
    size_labels_map,
    size_prices_map,
)

ARTWORK_INSTRUCTIONS = (
    "Send or forward your sticker artwork as a File (PNG, JPEG, or plain SVG, up to 8 MiB).\n"
    "You can send multiple files one after another — I'll count them and give you buttons to size them all at once or one by one."
)
PHONE_PROMPT = "Update contact number for order "
QUANTITIES = (1, 2, 3, 5, 10, 20)
MENU_TEXTS = {BROWSE_TEMPLATES, UPLOAD_ARTWORK, MY_CART, MY_ORDERS, HELP, MANAGE_ORDERS, ADMIN_PANEL}


class PhoneValidationError(ValueError):
    pass


def normalize_phone(value: str) -> str:
    number = re.sub(r"[ ()-]", "", value.strip()) if isinstance(value, str) else ""
    if number.startswith("+251"):
        number = "0" + number[4:]
    elif number.startswith("251") and len(number) == 12:
        number = "0" + number[3:]
    if not re.fullmatch(r"0[79][0-9]{8}", number):
        raise PhoneValidationError("Enter a 10-digit 09 or 07 phone number, or use +251.")
    return number


def payment_instructions_text() -> str:
    accounts = db.list_payment_accounts()
    if not accounts:
        return settings.payment_instructions
    lines = ["Pay your 50% deposit to any account below:"]
    for acc in accounts:
        extra = f" — {acc['instructions']}" if acc.get("instructions") else ""
        lines.append(f"• {acc['label']}: {acc['account_name']} ({acc['account_number']}){extra}")
    return "\n".join(lines)


def pickup_instructions_text() -> str:
    return db.get_setting("pickup_instructions", settings.pickup_instructions)


def status_label(order: dict) -> str:
    status = order["status"]
    if status == "out_for_delivery":
        return "Ready for pickup" if order["delivery_method"] == "pickup" else "Dispatched"
    if status == "completed":
        return "Collected" if order["delivery_method"] == "pickup" else "Delivered"
    return status.replace("_", " ").capitalize()


def progress_bar(order: dict) -> str:
    steps = [
        ("pending_deposit", "1. Deposit Due"),
        ("deposit_confirmed", "2. Confirmed"),
        ("printing", "3. Printing"),
        ("out_for_delivery", "4. Ready" if order["delivery_method"] == "pickup" else "4. Dispatched"),
        ("completed", "5. Done"),
    ]
    order_map = {k: i for i, (k, _) in enumerate(steps)}
    current = order_map.get(order["status"], 0)
    return "  →  ".join(f"{'✅' if idx <= current else '⬜'} {label}" for idx, (_, label) in enumerate(steps))


def next_status(order: dict) -> str | None:
    if not order.get("deposit_paid"):
        return None
    return {
        "deposit_confirmed": "printing",
        "printing": "out_for_delivery",
        "out_for_delivery": "completed",
    }.get(order["status"])


def summary(order: dict) -> str:
    labels = size_labels_map()
    lines = [f"Order #{order['id'][:8]} · {status_label(order)}", progress_bar(order), ""]
    for item in order["items"]:
        size_name = labels.get(item["size"], item["size"])
        label = item.get("label") or size_name
        color = f" ({item['keyboard_color']})" if item.get("keyboard_color") else ""
        lines.append(
            f"• {label} · {size_name}{color} · {item['quantity']} × {item['unit_price']} ETB = {item['line_total']} ETB"
        )
    total = Decimal(str(order["total"]))
    deposit = Decimal(str(order["deposit_amount"]))
    subtotal = Decimal(str(order["subtotal"]))
    lines.extend([
        "",
        f"Subtotal · {subtotal:.2f} ETB",
        f"Discount · {order['discount_percent']}% ({subtotal - total:.2f} ETB)",
        f"Total · {total:.2f} ETB",
    ])
    deposit_label = "received" if order.get("deposit_paid") else "due"
    lines.append(f"Deposit {deposit_label} · {deposit:.2f} ETB")
    if order.get("balance_received_at"):
        lines.append(f"Balance received · {total - deposit:.2f} ETB ({order.get('balance_method') or 'recorded'})")
    else:
        lines.append(f"Balance at handover · {total - deposit:.2f} ETB")
    if order.get("customer_name"):
        lines.append(f"Name · {order['customer_name']}")
    lines.append(f"Contact · {order['customer_phone']}")
    if order["delivery_method"] == "pickup":
        lines.append(f"Pickup · {pickup_instructions_text()}")
    else:
        lines.append(f"Delivery Location · {order.get('delivery_address') or 'Confirm address with customer'}")
    return "\n".join(lines)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or uuid4().hex[:8]


def register_handlers(bot):
    def send(chat_id: int, text: str, rows=(), cols: int = 1):
        bot.send_message(chat_id, text, reply_markup=actions(rows, cols=cols), disable_web_page_preview=True)

    def send_menu(message, text: str):
        uid = message.from_user.id
        keyboard = main_menu_keyboard(staff=uid in settings.admin_telegram_ids)
        bot.send_message(message.chat.id, text, reply_markup=keyboard, disable_web_page_preview=True)

    def private(message) -> bool:
        if message.chat.type == "private":
            return True
        send(message.chat.id, "Open a private chat with the bot to order stickers.")
        return False

    def staff(message) -> bool:
        if not private(message):
            return False
        if message.from_user.id in settings.admin_telegram_ids:
            return True
        send(message.chat.id, "This command is for shop staff.")
        return False

    def ensure_registered(message) -> bool:
        uid = message.from_user.id
        if db.is_registered(uid):
            return True
        db.start_registration(uid)
        bot.send_message(
            message.chat.id,
            "Please register first! Enter your full name:",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        return False

    def collect_cart_items(uid: int) -> tuple[dict | None, list[dict], list[dict]]:
        batch = db.get_open_batch(uid)
        uploads = [it for it in db.list_batch_items(batch["id"]) if it.get("size")] if batch else []
        tpls = db.list_cart_templates(uid)
        return batch, uploads, tpls

    def show_cart(chat_id: int, uid: int):
        batch = db.get_open_batch(uid)
        all_uploads = db.list_batch_items(batch["id"]) if batch else []
        unconfigured = [u for u in all_uploads if not u.get("size")]
        configured_uploads = [u for u in all_uploads if u.get("size")]
        tpls = db.list_cart_templates(uid)

        if not all_uploads and not tpls:
            send(
                chat_id,
                "Your cart is empty.",
                [
                    ("🎨 Browse Templates", "callback_data", "tpl:cats"),
                    ("📤 Upload Stickers", "callback_data", "hint:upload"),
                ],
            )
            return

        if unconfigured:
            send(
                chat_id,
                f"You have {len(unconfigured)} uploaded sticker(s) that still need a size.",
                [
                    (f"✅ Choose sizes for {len(all_uploads)} stickers", "callback_data", f"cfg:mode:{batch['id']}"),
                    ("🗑 Clear uploaded batch", "callback_data", f"clearbatch:{batch['id']}"),
                ],
            )
            return

        labels = size_labels_map()
        calc_input = [(u["size"], u["quantity"], None) for u in configured_uploads]
        for t in tpls:
            tpl = db.get_template(t["template_id"])
            custom = tpl["custom_price"] if (tpl and tpl.get("custom_price") and t["size"] == tpl["default_size"]) else None
            calc_input.append((t["size"], t["quantity"], custom))

        calc = calculate_order(calc_input)

        lines = ["🛒 Your Cart Summary", ""]
        if configured_uploads:
            configs = {(u["size"], u["quantity"], u.get("keyboard_color")) for u in configured_uploads}
            if len(configs) == 1 and len(configured_uploads) > 1:
                u0 = configured_uploads[0]
                color = f" ({u0['keyboard_color']})" if u0.get("keyboard_color") else ""
                lines.append(
                    f"• Stickers #1–#{len(configured_uploads)} ({len(configured_uploads)} designs) · "
                    f"{labels.get(u0['size'], u0['size'])}{color} × {u0['quantity']} each"
                )
            else:
                for idx, u in enumerate(configured_uploads, 1):
                    color = f" ({u['keyboard_color']})" if u.get("keyboard_color") else ""
                    lines.append(f"• #{idx} · {labels.get(u['size'], u['size'])}{color} × {u['quantity']}")
        for t in tpls:
            color = f" ({t['keyboard_color']})" if t.get("keyboard_color") else ""
            lines.append(f"• Template: {t['title']} · {labels.get(t['size'], t['size'])}{color} × {t['quantity']}")

        lines.extend([
            "",
            f"Subtotal · {calc.subtotal:.2f} ETB",
            f"Discount ({calc.discount_percent}%) · -{calc.discount_amount:.2f} ETB",
            f"Total · {calc.total:.2f} ETB",
            f"Deposit due now · {calc.deposit:.2f} ETB",
            f"Balance at handover · {calc.total - calc.deposit:.2f} ETB",
        ])

        rows = [
            ("🏪 Checkout · Pickup", "callback_data", "checkout:pickup"),
            ("🚚 Checkout · Delivery", "callback_data", "checkout:delivery"),
            ("🎨 Add More Templates", "callback_data", "tpl:cats"),
            ("🗑 Clear Cart", "callback_data", "clearall"),
        ]
        send(chat_id, "\n".join(lines), rows)

    def show_checkout_confirm(chat_id: int, uid: int, method: str, address: str | None = None):
        user = db.get_user(uid)
        phone = user["phone"] if user else ""
        db.set_session(uid, "checkout_ready", {"method": method, "address": address, "phone": phone})
        lines = [
            "Confirm Your Order",
            f"Handover · {'Pickup' if method == 'pickup' else 'Delivery'}",
            f"Delivery Location · {address}" if method == "delivery" else f"Pickup · {pickup_instructions_text()}",
            f"Phone · {phone}",
        ]
        send(
            chat_id,
            "\n".join(lines),
            [
                (f"✅ Place Order (using {phone})", "callback_data", "checkout:place"),
                ("📞 Change Phone Number", "callback_data", "checkout:phone"),
                ("⬅️ Back to Cart", "callback_data", "cart:view"),
            ],
        )

    def place_order_from_cart(chat_id: int, uid: int, method: str, address: str | None, phone: str):
        batch, uploads, tpls = collect_cart_items(uid)
        if not uploads and not tpls:
            send(chat_id, "Your cart is empty.")
            return

        calc_input = [(u["size"], u["quantity"], None) for u in uploads]
        for t in tpls:
            tpl = db.get_template(t["template_id"])
            custom = tpl["custom_price"] if (tpl and tpl.get("custom_price") and t["size"] == tpl["default_size"]) else None
            calc_input.append((t["size"], t["quantity"], custom))

        calc = calculate_order(calc_input)

        stored_items = []
        idx = 0
        for u in uploads:
            line = calc.line_items[idx]
            idx += 1
            stored_items.append({
                "kind": "upload",
                "ref_id": u["id"],
                "label": u["file_name"],
                "size": u["size"],
                "quantity": u["quantity"],
                "keyboard_color": u.get("keyboard_color"),
                "unit_price": str(line.unit_price),
                "line_total": str(line.line_total),
            })
        for t in tpls:
            line = calc.line_items[idx]
            idx += 1
            stored_items.append({
                "kind": "template",
                "ref_id": t["template_id"],
                "label": f"Template: {t['title']}",
                "size": t["size"],
                "quantity": t["quantity"],
                "keyboard_color": t.get("keyboard_color"),
                "unit_price": str(line.unit_price),
                "line_total": str(line.line_total),
            })

        user = db.get_user(uid)
        customer_name = user["full_name"] if user and user.get("full_name") else "Customer"
        order = db.save_order({
            "telegram_user_id": uid,
            "telegram_chat_id": chat_id,
            "customer_name": customer_name,
            "batch_id": batch["id"] if batch and uploads else None,
            "items": stored_items,
            "subtotal": str(calc.subtotal),
            "discount_percent": calc.discount_percent,
            "total": str(calc.total),
            "deposit_amount": str(calc.deposit),
            "delivery_method": method,
            "delivery_address": address,
            "customer_phone": phone,
        })
        db.clear_cart_templates(uid)
        db.set_session(uid, "", {})
        storage.tag_order_in_channel(bot, order, uploads)
        receipt(chat_id, order)
        notify_staff_new_order(bot, order)

    def receipt(chat_id: int, order: dict, admin: bool = False):
        oid = order["id"]
        rows = []
        note = ""
        if admin:
            if order.get("batch_id"):
                rows.append(("Send artwork files", "callback_data", f"artwork:{oid}"))
            note += f"\nFull order ID · {oid}"
            if not order.get("deposit_paid"):
                rows.append(("Confirm deposit received", "callback_data", f"deposit:{oid}"))
            target = next_status(order)
            if target == "completed":
                handover = "collection" if order["delivery_method"] == "pickup" else "delivery"
                note += f"\nConfirm {handover} after receiving the balance."
                for method in ("cash", "transfer", "other"):
                    rows.append((f"Confirm {handover} · paid by {method}", "callback_data", f"settle:{oid}:{method}"))
            elif target:
                caption = {
                    "printing": "Start printing",
                    "out_for_delivery": "Ready for pickup" if order["delivery_method"] == "pickup" else "Dispatch",
                }[target]
                rows.append((caption, "callback_data", f"advance:{oid}:{target}"))
        else:
            rows.append(("🔄 Refresh Status", "callback_data", f"mine:{oid}"))
            if order["status"] == "pending_deposit":
                note = f"\n\n{payment_instructions_text()}"
                shot_label = "📸 Resend Payment Screenshot" if order.get("payment_screenshot_file_id") else "📸 Send Payment Screenshot"
                rows.append((shot_label, "callback_data", f"payshot:{oid}"))
                rows.append(("📞 Update phone number", "callback_data", f"phone:{oid}"))
        send(chat_id, summary(order) + note, rows)

    # --- Admin Panel Views ---

    def show_admin_menu(chat_id: int):
        send(
            chat_id,
            "⚙️ Admin Panel — Manage Shop",
            [
                ("🎨 Templates (Add / Edit / Delete)", "callback_data", "adm:tpls"),
                ("📏 Sizes & Prices (Add / Edit / Delete)", "callback_data", "adm:sizes"),
                ("🏷 Discounts & Deposit %", "callback_data", "adm:disc"),
                ("💳 Payment Accounts (Add / Edit / Delete)", "callback_data", "adm:pay"),
                ("🛠 Manage Orders", "callback_data", "staffpage:0"),
            ],
        )

    def show_admin_templates(chat_id: int):
        cats = db.list_template_categories(include_inactive=True)
        rows = [("➕ Add New Template", "callback_data", "adm:tpl_add")]
        for slug, name in cats:
            rows.append((f"📁 {name}", "callback_data", f"adm:tpl_cat:{slug}"))
        rows.append(("⬅️ Admin Menu", "callback_data", "adm:menu"))
        send(chat_id, "🎨 Admin · Templates & Categories:", rows)

    def show_admin_template_detail(chat_id: int, tpl: dict):
        labels = size_labels_map()
        prices = size_prices_map(include_inactive=True)
        base_p = prices.get(tpl["default_size"], Decimal("0"))
        price_str = f"{tpl['custom_price']} ETB (custom)" if tpl.get("custom_price") else f"{base_p} ETB (size price)"
        status = "Active ✅" if tpl.get("active", 1) else "Hidden 🚫"
        text = (
            f"🎨 {tpl['title']}\n"
            f"Category: {tpl['category_name']}\n"
            f"Description: {tpl['description']}\n"
            f"Default Size: {labels.get(tpl['default_size'], tpl['default_size'])}\n"
            f"Price: {price_str}\n"
            f"Status: {status}"
        )
        rows = [
            ("🖼 Change Sample Image", "callback_data", f"adm:tpl_e:img:{tpl['id']}"),
            ("✏️ Edit Title", "callback_data", f"adm:tpl_e:title:{tpl['id']}"),
            ("📝 Edit Description", "callback_data", f"adm:tpl_e:desc:{tpl['id']}"),
            ("📏 Change Default Size", "callback_data", f"adm:tpl_e:size:{tpl['id']}"),
            ("💰 Set Custom Price", "callback_data", f"adm:tpl_e:price:{tpl['id']}"),
            ("👁 Toggle Active/Hidden", "callback_data", f"adm:tpl_toggle:{tpl['id']}"),
            ("🗑 Delete Template", "callback_data", f"adm:tpl_del:{tpl['id']}"),
            ("⬅️ Back", "callback_data", f"adm:tpl_cat:{tpl['category']}"),
        ]
        if tpl.get("sample_file_id"):
            bot.send_photo(chat_id, tpl["sample_file_id"], caption=text, reply_markup=actions(rows))
        else:
            send(chat_id, text, rows)

    def show_admin_sizes(chat_id: int):
        sizes = db.list_sizes(include_inactive=True)
        rows = [("➕ Add New Size", "callback_data", "adm:size_add")]
        for s in sizes:
            flag = "✅" if s["active"] else "🚫"
            rows.append((f"{flag} {s['label']} — {s['price_etb']} ETB", "callback_data", f"adm:size_v:{s['key']}"))
        rows.append(("⬅️ Admin Menu", "callback_data", "adm:menu"))
        send(chat_id, "📏 Admin · Sticker Sizes & Prices:", rows)

    def show_admin_discounts(chat_id: int):
        tiers = db.list_discount_tiers()
        dep = db.get_setting("deposit_percent", "50")
        pickup = pickup_instructions_text()
        lines = [
            "🏷 Admin · Discounts & Shop Settings",
            f"Deposit Required: {dep}%",
            f"Pickup Info: {pickup}",
            "",
            "Volume Discount Tiers:",
        ]
        rows = [
            ("➕ Add / Update Discount Tier", "callback_data", "adm:disc_add"),
            (f"💵 Edit Deposit % ({dep}%)", "callback_data", "adm:set_dep"),
            ("📍 Edit Pickup Instructions", "callback_data", "adm:set_pickup"),
        ]
        for min_q, pct in tiers:
            lines.append(f"• {min_q}+ stickers → {pct}% off")
            rows.append((f"🗑 Delete {min_q}+ ({pct}%)", "callback_data", f"adm:disc_del:{min_q}"))
        rows.append(("⬅️ Admin Menu", "callback_data", "adm:menu"))
        send(chat_id, "\n".join(lines), rows)

    def show_admin_payments(chat_id: int):
        accs = db.list_payment_accounts(include_inactive=True)
        rows = [("➕ Add Payment Account", "callback_data", "adm:pay_add")]
        for a in accs:
            flag = "✅" if a["active"] else "🚫"
            rows.append((f"{flag} {a['label']} ({a['account_number']})", "callback_data", f"adm:pay_v:{a['id']}"))
        rows.append(("⬅️ Admin Menu", "callback_data", "adm:menu"))
        send(chat_id, "💳 Admin · Payment Accounts:", rows)

    # --- Customer Template Sample View ---

    def show_customer_template(chat_id: int, tpl: dict, chosen_size: str | None = None):
        labels = size_labels_map()
        prices = size_prices_map(include_inactive=True)
        size = chosen_size or tpl["default_size"]
        if tpl.get("custom_price") and size == tpl["default_size"]:
            unit_price = Decimal(str(tpl["custom_price"]))
        else:
            unit_price = prices.get(size, Decimal("60"))

        caption = (
            f"🎨 {tpl['title']}\n"
            f"{tpl['description']}\n\n"
            f"Size: {labels.get(size, size)} · {unit_price} ETB each\n"
            f"Choose quantity to add to cart:"
        )
        rows = [(f"Add {q}", "callback_data", f"tpl:add:{tpl['id']}:{size}:{q}") for q in QUANTITIES]
        rows.append(("📏 Choose Different Size", "callback_data", f"tpl:sizes:{tpl['id']}"))
        rows.append(("⬅️ Back to Category", "callback_data", f"tpl:cat:{tpl['category']}"))
        if tpl.get("sample_file_id"):
            bot.send_photo(chat_id, tpl["sample_file_id"], caption=caption, reply_markup=actions(rows, cols=3))
        else:
            send(chat_id, caption, rows, cols=3)

    help_text = (
        "Welcome to Sticker Shop!\n\n"
        f"• Tap {BROWSE_TEMPLATES} to view sample designs by category.\n"
        f"• Tap {UPLOAD_ARTWORK} (or send/forward PNG, JPEG, or plain SVG files) to print your own stickers.\n"
        f"• Tap {MY_CART} to review & checkout.\n"
        f"• Tap {MY_ORDERS} anytime to track your progress."
    )

    @bot.message_handler(commands=["start"])
    def start(message):
        if not private(message):
            return
        uid = message.from_user.id
        if not db.is_registered(uid):
            db.start_registration(uid)
            bot.send_message(
                message.chat.id,
                "Welcome to Sticker Shop! Before we begin, please enter your full name:",
                reply_markup=types.ReplyKeyboardRemove(),
            )
            return
        send_menu(message, help_text)

    @bot.message_handler(commands=["register"])
    def register_cmd(message):
        if not private(message):
            return
        db.start_registration(message.from_user.id)
        bot.send_message(
            message.chat.id,
            "Let's update your registration. Please enter your full name:",
            reply_markup=types.ReplyKeyboardRemove(),
        )

    @bot.message_handler(commands=["help"])
    @bot.message_handler(func=lambda m: m.text == HELP)
    def help_cmd(message):
        if private(message):
            send_menu(message, help_text)

    @bot.message_handler(commands=["admin"])
    @bot.message_handler(func=lambda m: m.text == ADMIN_PANEL)
    def admin_panel_cmd(message):
        if staff(message):
            show_admin_menu(message.chat.id)

    @bot.message_handler(func=lambda m: m.text == BROWSE_TEMPLATES)
    def browse_templates_cmd(message):
        if private(message) and ensure_registered(message):
            cats = db.list_template_categories()
            if not cats:
                send(message.chat.id, "No templates available yet.")
                return
            rows = [(name, "callback_data", f"tpl:cat:{slug}") for slug, name in cats]
            send(message.chat.id, "🎨 Choose a sticker category:", rows, cols=2)

    @bot.message_handler(func=lambda m: m.text == MY_CART)
    def my_cart_cmd(message):
        if private(message) and ensure_registered(message):
            show_cart(message.chat.id, message.from_user.id)

    @bot.message_handler(func=lambda m: m.text == UPLOAD_ARTWORK)
    def prompt_artwork(message):
        if private(message) and ensure_registered(message):
            send(message.chat.id, ARTWORK_INSTRUCTIONS)

    batch_msg_lock = threading.Lock()
    batch_prompt_msgs: dict[str, int] = {}

    def send_batch_ready_prompt(chat_id: int, batch_id: str, prefix: str = ""):
        items = db.list_batch_items(batch_id)
        count = len(items)
        noun = "sticker" if count == 1 else "stickers"
        msg = f"{prefix}You have {count} {noun} ready.\nSend more files or tap below to choose sizes."
        markup = actions([
            (f"✅ Done — Choose Sizes ({count})", "callback_data", f"cfg:mode:{batch_id}"),
            ("🗑 Clear current batch", "callback_data", f"clearbatch:{batch_id}"),
        ])
        with batch_msg_lock:
            existing_msg_id = batch_prompt_msgs.get(batch_id)
            if existing_msg_id:
                try:
                    bot.edit_message_text(msg, chat_id, existing_msg_id, reply_markup=markup)
                    return
                except Exception:
                    pass
            sent = bot.send_message(chat_id, msg, reply_markup=markup, disable_web_page_preview=True)
            if sent and hasattr(sent, "message_id"):
                batch_prompt_msgs[batch_id] = sent.message_id

    def handle_payment_screenshot(chat_id: int, uid: int, order: dict, file_id: str, *, is_photo: bool):
        batch_items = db.list_batch_items(order["batch_id"]) if order.get("batch_id") else []
        stored_fid, msg_id = storage.send_order_package_to_channel(
            bot, order, batch_items, file_id, is_photo=is_photo
        )
        db.attach_payment_screenshot(order["id"], stored_fid or file_id, msg_id)
        db.set_session(uid, "", {})
        notify_staff_payment_screenshot(bot, order, stored_fid or file_id, is_photo=is_photo)
        send(
            chat_id,
            f"Payment screenshot received for Order #{order['id'][:8]}! Our staff will verify and confirm your deposit shortly.",
            [("🔄 Refresh Status", "callback_data", f"mine:{order['id']}")],
        )

    @bot.message_handler(content_types=["photo"])
    def photo_received(message):
        if not private(message):
            return
        uid = message.from_user.id
        sess = db.get_session(uid)
        if uid in settings.admin_telegram_ids and sess["action"] in ("adm_tpl_image", "adm_tpl_edit_img"):
            file_id = message.photo[-1].file_id
            data = sess["data"]
            if sess["action"] == "adm_tpl_image":
                stored_id, msg_id = storage.store_template_sample(bot, file_id, data.get("title", "Sample"), is_photo=True)
                data["sample_file_id"] = stored_id
                data["sample_channel_msg_id"] = msg_id
                db.set_session(uid, "adm_tpl_title", data)
                send(message.chat.id, "Sample image saved! Now enter the template title:", [("Cancel", "callback_data", "adm:tpls")])
            else:
                tpl = db.get_template(data["tpl_id"])
                if tpl:
                    stored_id, msg_id = storage.store_template_sample(bot, file_id, tpl["title"], is_photo=True)
                    updated = db.update_template(tpl["id"], sample_file_id=stored_id, sample_channel_msg_id=msg_id)
                    db.set_session(uid, "", {})
                    show_admin_template_detail(message.chat.id, updated)
            return

        if not ensure_registered(message):
            return

        if sess["action"] == "await_payment_screenshot":
            order = db.order_by_id(sess["data"].get("order_id", ""))
            if order and order["telegram_user_id"] == uid and order["status"] == "pending_deposit":
                handle_payment_screenshot(message.chat.id, uid, order, message.photo[-1].file_id, is_photo=True)
                return

        pending = db.list_pending_deposit_orders(uid)
        if len(pending) == 1:
            handle_payment_screenshot(message.chat.id, uid, pending[0], message.photo[-1].file_id, is_photo=True)
            return
        if len(pending) > 1:
            rows = [(f"Order #{o['id'][:8]} · {o['deposit_amount']} ETB", "callback_data", f"payshot:{o['id']}") for o in pending[:5]]
            send(message.chat.id, "You have multiple pending orders. Tap the order you paid for, then send the screenshot:", rows)
            return

        send(
            message.chat.id,
            "Please send or forward your sticker as a File (Document) — PNG, JPEG, or plain SVG — "
            "so we keep full print quality.",
        )

    @bot.message_handler(content_types=["sticker"])
    def sticker_received(message):
        if private(message) and ensure_registered(message):
            send(
                message.chat.id,
                "Please send or forward your sticker as a File (Document) — PNG, JPEG, or plain SVG — "
                "so we keep full print quality.",
            )

    @bot.message_handler(content_types=["document"])
    def document(message):
        if not private(message) or not ensure_registered(message):
            return
        uid = message.from_user.id
        sess = db.get_session(uid)
        doc = message.document
        mime = doc.mime_type or ""

        # Allow admin to upload template sample as PNG/JPEG document too
        if uid in settings.admin_telegram_ids and sess["action"] in ("adm_tpl_image", "adm_tpl_edit_img") and mime in ("image/png", "image/jpeg"):
            data = sess["data"]
            if sess["action"] == "adm_tpl_image":
                stored_id, msg_id = storage.store_template_sample(bot, doc.file_id, data.get("title", "Sample"), is_photo=False)
                data["sample_file_id"] = stored_id
                data["sample_channel_msg_id"] = msg_id
                db.set_session(uid, "adm_tpl_title", data)
                send(message.chat.id, "Sample image saved! Now enter the template title:", [("Cancel", "callback_data", "adm:tpls")])
            else:
                tpl = db.get_template(data["tpl_id"])
                if tpl:
                    stored_id, msg_id = storage.store_template_sample(bot, doc.file_id, tpl["title"], is_photo=False)
                    updated = db.update_template(tpl["id"], sample_file_id=stored_id, sample_channel_msg_id=msg_id)
                    db.set_session(uid, "", {})
                    show_admin_template_detail(message.chat.id, updated)
            return

        if sess["action"] == "await_payment_screenshot" and mime in ("image/png", "image/jpeg"):
            order = db.order_by_id(sess["data"].get("order_id", ""))
            if order and order["telegram_user_id"] == uid and order["status"] == "pending_deposit":
                handle_payment_screenshot(message.chat.id, uid, order, doc.file_id, is_photo=False)
                return

        if mime not in storage.EXTENSIONS:
            send(message.chat.id, "Choose a PNG, JPEG or plain SVG and send it as a File.")
            return
        try:
            raw = storage.download_telegram_file(bot, doc.file_id, doc.file_size)
            data = storage.validate_image(raw, mime)
        except storage.ArtworkError as exc:
            send(message.chat.id, str(exc))
            return

        user = db.get_user(uid)
        batch = db.get_or_create_open_batch(uid)
        filename = doc.file_name or f"sticker.{storage.EXTENSIONS[mime]}"
        try:
            channel_file_id, channel_msg_id = storage.store_in_channel(
                bot,
                data,
                filename,
                uid,
                batch["id"],
                full_name=user["full_name"] if user else "",
                phone=user["phone"] if user else "",
            )
        except Exception as exc:
            logging.error("Failed storing artwork in channel (%s): %s", type(exc).__name__, exc)
            send(message.chat.id, "Could not save your file right now. Please try again.")
            return

        item, created = db.add_upload_item(
            batch_id=batch["id"],
            owner_id=uid,
            telegram_message_id=message.message_id,
            channel_message_id=channel_msg_id,
            file_id=channel_file_id,
            file_name=filename,
            mime_type=mime,
            extension=storage.EXTENSIONS[mime],
        )
        prefix = f"Received '{item['file_name']}'. " if created else f"'{item['file_name']}' is already in your batch. "
        send_batch_ready_prompt(message.chat.id, batch["id"], prefix)

    def pending_reg_step(message) -> str | None:
        if message.chat.type != "private":
            return None
        user = db.get_user(message.from_user.id)
        if user and user["reg_step"] in ("ask_name", "ask_phone"):
            return user["reg_step"]
        return None

    @bot.message_handler(content_types=["contact"])
    def contact_received(message):
        if not private(message):
            return
        uid = message.from_user.id
        if pending_reg_step(message) != "ask_phone":
            return
        try:
            phone = normalize_phone(message.contact.phone_number)
        except PhoneValidationError as exc:
            bot.send_message(message.chat.id, f"{exc} Please type your 09, 07, or +251 number:")
            return
        user = db.complete_registration(uid, phone)
        send_menu(message, f"Registration complete, {user['full_name']} ({user['phone']})!\n\n{help_text}")

    @bot.message_handler(content_types=["location"])
    def location_received(message):
        if not private(message):
            return
        uid = message.from_user.id
        sess = db.get_session(uid)
        if sess["action"] != "await_address":
            return
        loc = message.location
        addr = f"https://maps.google.com/?q={loc.latitude:.6f},{loc.longitude:.6f}"
        bot.send_message(
            message.chat.id,
            "Delivery location saved!",
            reply_markup=main_menu_keyboard(staff=uid in settings.admin_telegram_ids),
        )
        show_checkout_confirm(message.chat.id, uid, "delivery", address=addr)

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

    @bot.message_handler(
        func=lambda m: (
            m.chat.type == "private"
            and bool(m.text)
            and not m.text.startswith("/")
            and m.text not in MENU_TEXTS
            and (pending_reg_step(m) is not None or bool(db.get_session(m.from_user.id)["action"]))
        ),
        content_types=["text"],
    )
    def wizard_text(message):
        uid = message.from_user.id
        text = message.text.strip()
        reg_step = pending_reg_step(message)
        if reg_step == "ask_name":
            if len(text) < 2 or len(text) > 80:
                bot.send_message(message.chat.id, "Please enter a valid full name (2–80 characters):")
                return
            db.set_user_name(uid, text)
            bot.send_message(
                message.chat.id,
                f"Thanks, {text}! Now tap 'Share my phone number' below or type your 09, 07, or +251 phone number:",
                reply_markup=phone_share_keyboard(),
            )
            return
        if reg_step == "ask_phone":
            try:
                phone = normalize_phone(text)
            except PhoneValidationError as exc:
                bot.send_message(message.chat.id, f"{exc} Try again:")
                return
            user = db.complete_registration(uid, phone)
            send_menu(message, f"Registration complete, {user['full_name']} ({user['phone']})!\n\n{help_text}")
            return

        sess = db.get_session(uid)
        act, data = sess["action"], sess["data"]

        # Customer checkout prompts
        if act == "await_address":
            if not 1 <= len(text) <= MAX_ADDRESS:
                send(message.chat.id, f"Please enter a delivery address (1–{MAX_ADDRESS} characters).")
                return
            bot.send_message(
                message.chat.id,
                "Delivery location saved!",
                reply_markup=main_menu_keyboard(staff=uid in settings.admin_telegram_ids),
            )
            show_checkout_confirm(message.chat.id, uid, "delivery", address=text)
            return
        if act == "await_checkout_phone":
            try:
                phone = normalize_phone(text)
            except PhoneValidationError as exc:
                send(message.chat.id, f"{exc} Try again:")
                return
            data["phone"] = phone
            db.set_session(uid, "checkout_ready", data)
            show_checkout_confirm(message.chat.id, uid, data["method"], address=data.get("address"))
            return

        # Admin CRUD prompts
        if uid not in settings.admin_telegram_ids:
            return

        if act == "adm_tpl_new_cat":
            data["category_name"] = text
            data["category"] = _slugify(text)
            db.set_session(uid, "adm_tpl_image", data)
            send(message.chat.id, "Send the template's sample image (photo or PNG/JPEG file):", [("Cancel", "callback_data", "adm:tpls")])
        elif act == "adm_tpl_title":
            data["title"] = text
            db.set_session(uid, "adm_tpl_desc", data)
            send(message.chat.id, "Enter a short description:", [("Cancel", "callback_data", "adm:tpls")])
        elif act == "adm_tpl_desc":
            data["description"] = text
            db.set_session(uid, "adm_tpl_size", data)
            rows = [(f"{s['label']} ({s['price_etb']} ETB)", "callback_data", f"adm:tpl_nsize:{s['key']}") for s in db.list_sizes()]
            send(message.chat.id, "Pick the default size for this template:", rows, cols=2)
        elif act == "adm_tpl_price":
            try:
                price = str(Decimal(text).quantize(Decimal("0.01")))
                if Decimal(price) <= 0:
                    raise InvalidOperation()
            except (InvalidOperation, ValueError):
                send(message.chat.id, "Enter a valid positive price in ETB (e.g. 75):")
                return
            tpl = db.create_template(
                template_id=_slugify(data["title"]) + "-" + uuid4().hex[:4],
                category=data["category"],
                category_name=data["category_name"],
                title=data["title"],
                description=data["description"],
                default_size=data["default_size"],
                custom_price=price,
                sample_file_id=data.get("sample_file_id"),
                sample_channel_msg_id=data.get("sample_channel_msg_id"),
            )
            db.set_session(uid, "", {})
            show_admin_template_detail(message.chat.id, tpl)
        elif act == "adm_tpl_edit_field":
            tpl_id, field = data["tpl_id"], data["field"]
            if field == "price":
                try:
                    val = str(Decimal(text).quantize(Decimal("0.01")))
                    if Decimal(val) <= 0:
                        raise InvalidOperation()
                except (InvalidOperation, ValueError):
                    send(message.chat.id, "Enter a valid positive price in ETB:")
                    return
                updated = db.update_template(tpl_id, custom_price=val)
            else:
                updated = db.update_template(tpl_id, **{field: text})
            db.set_session(uid, "", {})
            if updated:
                show_admin_template_detail(message.chat.id, updated)
        elif act == "adm_size_add_label":
            data["label"] = text
            data["key"] = _slugify(text).replace("-", "_")
            db.set_session(uid, "adm_size_add_price", data)
            send(message.chat.id, f"Enter price in ETB for '{text}' (e.g. 90):")
        elif act == "adm_size_add_price":
            try:
                price = str(Decimal(text).quantize(Decimal("0.01")))
                if Decimal(price) <= 0:
                    raise InvalidOperation()
            except (InvalidOperation, ValueError):
                send(message.chat.id, "Enter a valid positive price in ETB:")
                return
            db.upsert_size(data["key"], data["label"], price)
            db.set_session(uid, "", {})
            show_admin_sizes(message.chat.id)
        elif act == "adm_size_edit_price":
            try:
                price = str(Decimal(text).quantize(Decimal("0.01")))
                if Decimal(price) <= 0:
                    raise InvalidOperation()
            except (InvalidOperation, ValueError):
                send(message.chat.id, "Enter a valid positive price in ETB:")
                return
            s = db.get_size(data["key"])
            if s:
                db.upsert_size(s["key"], s["label"], price, s["sort_order"], s["active"])
            db.set_session(uid, "", {})
            show_admin_sizes(message.chat.id)
        elif act == "adm_size_edit_label":
            s = db.get_size(data["key"])
            if s:
                db.upsert_size(s["key"], text, s["price_etb"], s["sort_order"], s["active"])
            db.set_session(uid, "", {})
            show_admin_sizes(message.chat.id)
        elif act == "adm_disc_add":
            parts = text.split()
            try:
                min_q, pct = int(parts[0]), int(parts[1])
                if min_q < 1 or not 0 <= pct <= 90:
                    raise ValueError()
            except (ValueError, IndexError):
                send(message.chat.id, "Send `<min_quantity> <percent>` (example: `15 12` for 12% off 15+):")
                return
            db.upsert_discount_tier(min_q, pct)
            db.set_session(uid, "", {})
            show_admin_discounts(message.chat.id)
        elif act == "adm_set_dep":
            try:
                pct = int(text)
                if not 0 <= pct <= 100:
                    raise ValueError()
            except ValueError:
                send(message.chat.id, "Enter a whole percentage between 0 and 100:")
                return
            db.set_setting("deposit_percent", str(pct))
            db.set_session(uid, "", {})
            show_admin_discounts(message.chat.id)
        elif act == "adm_set_pickup":
            db.set_setting("pickup_instructions", text)
            db.set_session(uid, "", {})
            show_admin_discounts(message.chat.id)
        elif act == "adm_pay_label":
            data["label"] = text
            db.set_session(uid, "adm_pay_name", data)
            send(message.chat.id, "Enter the account holder name (e.g. Sticker Shop):")
        elif act == "adm_pay_name":
            data["account_name"] = text
            db.set_session(uid, "adm_pay_num", data)
            send(message.chat.id, "Enter the account/phone number:")
        elif act == "adm_pay_num":
            data["account_number"] = text
            db.set_session(uid, "adm_pay_inst", data)
            send(message.chat.id, "Enter short payment instructions (or type `-` for none):")
        elif act == "adm_pay_inst":
            inst = "" if text == "-" else text
            db.create_payment_account(data["label"], data["account_name"], data["account_number"], inst)
            db.set_session(uid, "", {})
            show_admin_payments(message.chat.id)
        elif act == "adm_pay_edit_field":
            db.update_payment_account(data["acc_id"], **{data["field"]: text})
            db.set_session(uid, "", {})
            show_admin_payments(message.chat.id)

    def queue(chat_id: int, page: int, owner_id: int | None = None):
        rows = db.list_orders(page, owner_id)
        mode = "staff" if owner_id is None else "mine"
        buttons = [
            (f"#{o['id'][:8]} · {status_label(o)} · {o['total']} ETB", "callback_data", f"{mode}:{o['id']}")
            for o in rows[:db.PAGE_SIZE]
        ]
        if page > 0:
            buttons.append(("Previous page", "callback_data", f"{mode}page:{page - 1}"))
        if len(rows) > db.PAGE_SIZE:
            buttons.append(("Next page", "callback_data", f"{mode}page:{page + 1}"))
        title = "Active orders" if owner_id is None else "My orders"
        send(chat_id, f"{title} · Page {page + 1}" if rows else f"{title} · No orders here.", buttons)

    def update_phone(message, order_id: str, raw: str):
        try:
            oid = str(UUID(order_id))
            phone = normalize_phone(raw)
        except PhoneValidationError as exc:
            send(message.chat.id, f"{exc} Tap Update phone number to try again.")
            return
        except (ValueError, TypeError):
            send(message.chat.id, f"Open {MY_ORDERS} and tap Update phone number on your receipt.")
            return
        order = db.order_by_id(oid)
        updated = db.correct_contact_phone(order, message.from_user.id, phone) if order else None
        if updated:
            receipt(message.chat.id, updated)
        else:
            send(message.chat.id, f"This order is unavailable for phone changes. Open {MY_ORDERS}.")

    @bot.message_handler(commands=["phone"])
    def correct_phone(message):
        if private(message):
            parts = message.text.split(maxsplit=2)
            if len(parts) == 3:
                update_phone(message, parts[1], parts[2])
            else:
                send(message.chat.id, f"Open {MY_ORDERS} and tap Update phone number on your receipt.")

    @bot.message_handler(func=lambda m: m.text == MANAGE_ORDERS)
    @bot.message_handler(commands=["orders"])
    def orders_cmd(message):
        if staff(message):
            queue(message.chat.id, 0)

    @bot.message_handler(func=lambda m: m.text == MY_ORDERS)
    @bot.message_handler(commands=["myorders"])
    def my_orders_cmd(message):
        if private(message) and ensure_registered(message):
            queue(message.chat.id, 0, message.from_user.id)

    def advance(chat_id: int, order: dict, target: str, admin_id: int, balance_method: str | None = None):
        if target != next_status(order):
            send(chat_id, f"This change is unavailable. Open {MANAGE_ORDERS} for the current stage.")
            return
        updated = db.advance_order(order, target, admin_id, balance_method)
        if not updated:
            send(chat_id, f"This change is unavailable. Open {MANAGE_ORDERS} for the current stage.")
            return
        delivery = notify_customer(bot, updated)
        feedback = {
            "delivered": "Customer notified.",
            "failed": "Notification failed. Contact the customer by phone.",
        }.get(delivery, "Status updated.")
        send(chat_id, f"Order #{updated['id'][:8]} updated to {status_label(updated)}. {feedback}")

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
        order = db.order_by_id(oid)
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
        action = parts[0]
        admin = action in ("staff", "staffpage", "advance", "settle", "deposit", "artwork", "adm")
        if admin and uid not in settings.admin_telegram_ids:
            bot.answer_callback_query(call.id, "Staff access required.")
            return

        try:
            bot.answer_callback_query(call.id)
        except Exception as exc:
            if getattr(exc, "error_code", None) != 400:
                raise

        # --- Admin Panel Callbacks ---
        if action == "adm":
            sub = parts[1] if len(parts) > 1 else "menu"
            if sub == "menu":
                db.set_session(uid, "", {})
                show_admin_menu(uid)
                return
            if sub == "tpls":
                db.set_session(uid, "", {})
                show_admin_templates(uid)
                return
            if sub == "tpl_cat" and len(parts) == 3:
                tpls = db.list_templates_by_category(parts[2], include_inactive=True)
                rows = [("➕ Add New Template", "callback_data", "adm:tpl_add")]
                for t in tpls:
                    flag = "✅" if t.get("active", 1) else "🚫"
                    rows.append((f"{flag} {t['title']}", "callback_data", f"adm:tpl_v:{t['id']}"))
                rows.append(("⬅️ Back", "callback_data", "adm:tpls"))
                send(uid, "Select a template to view/edit/delete:", rows)
                return
            if sub == "tpl_v" and len(parts) == 3:
                tpl = db.get_template(parts[2])
                if tpl:
                    show_admin_template_detail(uid, tpl)
                return
            if sub == "tpl_add":
                cats = db.list_template_categories(include_inactive=True)
                rows = [(name, "callback_data", f"adm:tpl_pickcat:{slug}") for slug, name in cats]
                rows.append(("➕ New Category", "callback_data", "adm:tpl_newcat"))
                rows.append(("Cancel", "callback_data", "adm:tpls"))
                send(uid, "Step 1: Choose a category for the new template:", rows, cols=2)
                return
            if sub == "tpl_newcat":
                db.set_session(uid, "adm_tpl_new_cat", {})
                send(uid, "Enter the new category name (e.g. Anime):", [("Cancel", "callback_data", "adm:tpls")])
                return
            if sub == "tpl_pickcat" and len(parts) == 3:
                cats = dict(db.list_template_categories(include_inactive=True))
                db.set_session(uid, "adm_tpl_image", {"category": parts[2], "category_name": cats.get(parts[2], parts[2])})
                send(uid, "Step 2: Send the template's sample image (photo or PNG/JPEG file):", [("Cancel", "callback_data", "adm:tpls")])
                return
            if sub == "tpl_nsize" and len(parts) == 3:
                sess = db.get_session(uid)
                data = sess["data"]
                data["default_size"] = parts[2]
                db.set_session(uid, "adm_tpl_price", data)
                send(
                    uid,
                    "Enter a custom price in ETB, or tap below to use the global size price:",
                    [("Use Global Size Price", "callback_data", "adm:tpl_use_global")],
                )
                return
            if sub == "tpl_use_global":
                sess = db.get_session(uid)
                data = sess["data"]
                tpl = db.create_template(
                    template_id=_slugify(data["title"]) + "-" + uuid4().hex[:4],
                    category=data["category"],
                    category_name=data["category_name"],
                    title=data["title"],
                    description=data["description"],
                    default_size=data["default_size"],
                    custom_price=None,
                    sample_file_id=data.get("sample_file_id"),
                    sample_channel_msg_id=data.get("sample_channel_msg_id"),
                )
                db.set_session(uid, "", {})
                show_admin_template_detail(uid, tpl)
                return
            if sub == "tpl_e" and len(parts) == 4:
                field, tpl_id = parts[2], parts[3]
                if field == "img":
                    db.set_session(uid, "adm_tpl_edit_img", {"tpl_id": tpl_id})
                    send(uid, "Send the new sample image (photo or PNG/JPEG file):")
                elif field == "size":
                    rows = [(s["label"], "callback_data", f"adm:tpl_setsize:{tpl_id}:{s['key']}") for s in db.list_sizes()]
                    send(uid, "Choose new default size:", rows, cols=2)
                elif field == "price":
                    db.set_session(uid, "adm_tpl_edit_field", {"tpl_id": tpl_id, "field": "price"})
                    send(uid, "Enter custom price in ETB:", [("Clear Custom Price (use size price)", "callback_data", f"adm:tpl_clrprice:{tpl_id}")])
                else:
                    db.set_session(uid, "adm_tpl_edit_field", {"tpl_id": tpl_id, "field": "description" if field == "desc" else field})
                    send(uid, f"Enter new {field}:")
                return
            if sub == "tpl_setsize" and len(parts) == 4:
                updated = db.update_template(parts[2], default_size=parts[3])
                if updated:
                    show_admin_template_detail(uid, updated)
                return
            if sub == "tpl_clrprice" and len(parts) == 3:
                db.set_session(uid, "", {})
                updated = db.update_template(parts[2], custom_price=None)
                if updated:
                    show_admin_template_detail(uid, updated)
                return
            if sub == "tpl_toggle" and len(parts) == 3:
                tpl = db.get_template(parts[2])
                if tpl:
                    updated = db.update_template(tpl["id"], active=0 if tpl.get("active", 1) else 1)
                    show_admin_template_detail(uid, updated)
                return
            if sub == "tpl_del" and len(parts) == 3:
                db.delete_template(parts[2])
                show_admin_templates(uid)
                return

            # Sizes CRUD
            if sub == "sizes":
                show_admin_sizes(uid)
                return
            if sub == "size_add":
                db.set_session(uid, "adm_size_add_label", {})
                send(uid, "Enter the new size label (e.g. `80 × 80 mm`):")
                return
            if sub == "size_v" and len(parts) == 3:
                s = db.get_size(parts[2])
                if s:
                    send(
                        uid,
                        f"📏 {s['label']} ({s['key']})\nPrice: {s['price_etb']} ETB\nStatus: {'Active' if s['active'] else 'Hidden'}",
                        [
                            ("💰 Edit Price", "callback_data", f"adm:size_ep:{s['key']}"),
                            ("✏️ Edit Label", "callback_data", f"adm:size_el:{s['key']}"),
                            ("👁 Toggle Active/Hidden", "callback_data", f"adm:size_tog:{s['key']}"),
                            ("🗑 Delete Size", "callback_data", f"adm:size_del:{s['key']}"),
                            ("⬅️ Back", "callback_data", "adm:sizes"),
                        ],
                    )
                return
            if sub == "size_ep" and len(parts) == 3:
                db.set_session(uid, "adm_size_edit_price", {"key": parts[2]})
                send(uid, "Enter new price in ETB:")
                return
            if sub == "size_el" and len(parts) == 3:
                db.set_session(uid, "adm_size_edit_label", {"key": parts[2]})
                send(uid, "Enter new size label:")
                return
            if sub == "size_tog" and len(parts) == 3:
                s = db.get_size(parts[2])
                if s:
                    db.upsert_size(s["key"], s["label"], s["price_etb"], s["sort_order"], 0 if s["active"] else 1)
                show_admin_sizes(uid)
                return
            if sub == "size_del" and len(parts) == 3:
                db.delete_size(parts[2])
                show_admin_sizes(uid)
                return

            # Discounts & settings CRUD
            if sub == "disc":
                show_admin_discounts(uid)
                return
            if sub == "disc_add":
                db.set_session(uid, "adm_disc_add", {})
                send(uid, "Send `<min_quantity> <percent>` (example: `15 12` for 12% off 15+ stickers):")
                return
            if sub == "disc_del" and len(parts) == 3 and parts[2].isdigit():
                db.delete_discount_tier(int(parts[2]))
                show_admin_discounts(uid)
                return
            if sub == "set_dep":
                db.set_session(uid, "adm_set_dep", {})
                send(uid, "Enter deposit percentage (0–100, e.g. 50):")
                return
            if sub == "set_pickup":
                db.set_session(uid, "adm_set_pickup", {})
                send(uid, "Enter new pickup instructions:")
                return

            # Payment accounts CRUD
            if sub == "pay":
                show_admin_payments(uid)
                return
            if sub == "pay_add":
                db.set_session(uid, "adm_pay_label", {})
                send(uid, "Enter payment method / bank name (e.g. Telebirr, CBE):")
                return
            if sub == "pay_v" and len(parts) == 3:
                a = db.get_payment_account(parts[2])
                if a:
                    send(
                        uid,
                        f"💳 {a['label']}\nName: {a['account_name']}\nNumber: {a['account_number']}\nNote: {a['instructions'] or 'None'}\nStatus: {'Active' if a['active'] else 'Hidden'}",
                        [
                            ("✏️ Edit Method/Bank", "callback_data", f"adm:pay_e:label:{a['id']}"),
                            ("👤 Edit Account Name", "callback_data", f"adm:pay_e:account_name:{a['id']}"),
                            ("🔢 Edit Account Number", "callback_data", f"adm:pay_e:account_number:{a['id']}"),
                            ("📝 Edit Instructions", "callback_data", f"adm:pay_e:instructions:{a['id']}"),
                            ("👁 Toggle Active/Hidden", "callback_data", f"adm:pay_tog:{a['id']}"),
                            ("🗑 Delete Account", "callback_data", f"adm:pay_del:{a['id']}"),
                            ("⬅️ Back", "callback_data", "adm:pay"),
                        ],
                    )
                return
            if sub == "pay_e" and len(parts) == 4:
                db.set_session(uid, "adm_pay_edit_field", {"field": parts[2], "acc_id": parts[3]})
                send(uid, f"Enter new {parts[2].replace('_', ' ')}:")
                return
            if sub == "pay_tog" and len(parts) == 3:
                a = db.get_payment_account(parts[2])
                if a:
                    db.update_payment_account(a["id"], active=0 if a["active"] else 1)
                show_admin_payments(uid)
                return
            if sub == "pay_del" and len(parts) == 3:
                db.delete_payment_account(parts[2])
                show_admin_payments(uid)
                return

        if action == "hint" and len(parts) == 2 and parts[1] == "upload":
            send(uid, ARTWORK_INSTRUCTIONS)
            return

        if action == "clearbatch" and len(parts) == 2:
            db.clear_open_batch(uid)
            send(uid, "Upload batch cleared. Send new sticker files anytime.")
            return

        if action == "clearall":
            db.clear_open_batch(uid)
            db.clear_cart_templates(uid)
            db.set_session(uid, "", {})
            send(uid, "Cart cleared.")
            return

        if action == "cart" and len(parts) == 2 and parts[1] == "view":
            show_cart(uid, uid)
            return

        # --- Customer Template Browsing ---
        if action == "tpl":
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "cats":
                cats = db.list_template_categories()
                rows = [(name, "callback_data", f"tpl:cat:{slug}") for slug, name in cats]
                send(uid, "🎨 Choose a sticker category:", rows, cols=2)
                return
            if sub == "cat" and len(parts) == 3:
                tpls = db.list_templates_by_category(parts[2])
                prices = size_prices_map(include_inactive=True)
                labels = size_labels_map()
                rows = []
                for t in tpls:
                    p = t["custom_price"] if t.get("custom_price") else prices.get(t["default_size"], "60")
                    rows.append((f"{t['title']} ({labels.get(t['default_size'], t['default_size'])} · {p} ETB)", "callback_data", f"tpl:pick:{t['id']}"))
                rows.append(("⬅️ Back to Categories", "callback_data", "tpl:cats"))
                send(uid, "Select a template to see its sample image:", rows)
                return
            if sub == "pick" and len(parts) == 3:
                tpl = db.get_template(parts[2])
                if not tpl:
                    send(uid, "Template not found.")
                    return
                show_customer_template(uid, tpl)
                return
            if sub == "sizes" and len(parts) == 3:
                tpl = db.get_template(parts[2])
                if not tpl:
                    return
                rows = [(f"{s['label']} ({s['price_etb']} ETB)", "callback_data", f"tpl:picksize:{tpl['id']}:{s['key']}") for s in db.list_sizes()]
                send(uid, f"Choose size for {tpl['title']}:", rows, cols=2)
                return
            if sub == "picksize" and len(parts) == 4:
                tpl = db.get_template(parts[2])
                if tpl:
                    show_customer_template(uid, tpl, chosen_size=parts[3])
                return
            if sub == "add" and len(parts) == 5 and parts[4].isdigit():
                tpl = db.get_template(parts[2])
                if not tpl:
                    send(uid, "Template not found.")
                    return
                size, qty = parts[3], int(parts[4])
                color = COLORS[0] if size == "spacebar_key" else None
                db.add_cart_template(uid, tpl["id"], size, qty, color)
                send(
                    uid,
                    f"Added {qty} × {tpl['title']} to your cart!",
                    [
                        ("🛒 View Cart & Checkout", "callback_data", "cart:view"),
                        ("🎨 Add More Templates", "callback_data", "tpl:cats"),
                    ],
                )
                return

        # --- Batch Sizing Wizard (Bulk or Individual) ---
        if action == "cfg":
            sub = parts[1] if len(parts) > 1 else ""
            sizes = db.list_sizes()
            valid_keys = {s["key"]: s for s in sizes}

            def send_indiv_prompt(batch_id: str, idx: int, items: list[dict]):
                item = items[idx]
                rows = [(f"{s['label']} ({s['price_etb']} ETB)", "callback_data", f"cfg:isize:{batch_id}:{idx}:{s['key']}") for s in sizes]
                caption = f"🎯 Sticker #{idx + 1} of {len(items)}\nChoose size:"
                preview = storage.build_sticker_preview(bot, item)
                if preview:
                    bot.send_photo(uid, preview, caption=caption, reply_markup=actions(rows, cols=2))
                else:
                    send(uid, f"{caption} ({item['file_name']})", rows, cols=2)

            if sub == "mode" and len(parts) == 3:
                batch_id = parts[2]
                items = db.list_batch_items(batch_id)
                if not items:
                    send(uid, "No stickers in this batch.")
                    return
                rows = [
                    (f"📦 Bulk size all ({len(items)} stickers)", "callback_data", f"cfg:bulk:{batch_id}"),
                    ("🎯 Size each sticker one by one", "callback_data", f"cfg:indiv:{batch_id}:0"),
                ]
                caption = f"You have {len(items)} sticker(s). How would you like to choose sizes?"
                sheet = storage.build_batch_contact_sheet(bot, items)
                if sheet:
                    bot.send_photo(uid, sheet, caption=caption, reply_markup=actions(rows))
                else:
                    send(uid, caption, rows)
                return

            if sub == "bulk" and len(parts) == 3:
                batch_id = parts[2]
                rows = [(f"{s['label']} ({s['price_etb']} ETB)", "callback_data", f"cfg:bsize:{batch_id}:{s['key']}") for s in sizes]
                send(uid, "📦 Pick ONE size for all stickers in this batch:", rows, cols=2)
                return

            if sub == "bsize" and len(parts) == 4 and parts[3] in valid_keys:
                batch_id, size = parts[2], parts[3]
                if size == "spacebar_key":
                    rows = [(c.capitalize(), "callback_data", f"cfg:bcolor:{batch_id}:{c}") for c in COLORS]
                    send(uid, "Choose spacebar color for all stickers:", rows, cols=3)
                    return
                rows = [(f"{q} each", "callback_data", f"cfg:bqty:{batch_id}:{size}:_:{q}") for q in QUANTITIES]
                send(uid, f"Size: {valid_keys[size]['label']}\nChoose quantity per sticker:", rows, cols=3)
                return

            if sub == "bcolor" and len(parts) == 4 and parts[3] in COLORS:
                batch_id, color = parts[2], parts[3]
                rows = [(f"{q} each", "callback_data", f"cfg:bqty:{batch_id}:spacebar_key:{color}:{q}") for q in QUANTITIES]
                send(uid, f"Size: Spacebar ({color})\nChoose quantity per sticker:", rows, cols=3)
                return

            if sub == "bqty" and len(parts) == 6 and parts[5].isdigit():
                batch_id, size, color_raw, qty = parts[2], parts[3], parts[4], int(parts[5])
                color = None if color_raw == "_" else color_raw
                db.bulk_configure_batch(batch_id, size, qty, color)
                show_cart(uid, uid)
                return

            if sub == "indiv" and len(parts) == 4 and parts[3].isdigit():
                batch_id, idx = parts[2], int(parts[3])
                items = db.list_batch_items(batch_id)
                if idx >= len(items):
                    show_cart(uid, uid)
                    return
                send_indiv_prompt(batch_id, idx, items)
                return

            if sub == "isize" and len(parts) == 5 and parts[3].isdigit() and parts[4] in valid_keys:
                batch_id, idx, size = parts[2], int(parts[3]), parts[4]
                if size == "spacebar_key":
                    rows = [(c.capitalize(), "callback_data", f"cfg:icolor:{batch_id}:{idx}:{c}") for c in COLORS]
                    send(uid, "Choose spacebar color:", rows, cols=3)
                    return
                rows = [(f"{q}", "callback_data", f"cfg:iqty:{batch_id}:{idx}:{size}:_:{q}") for q in QUANTITIES]
                send(uid, f"Size: {valid_keys[size]['label']}\nChoose quantity:", rows, cols=3)
                return

            if sub == "icolor" and len(parts) == 5 and parts[3].isdigit() and parts[4] in COLORS:
                batch_id, idx, color = parts[2], int(parts[3]), parts[4]
                rows = [(f"{q}", "callback_data", f"cfg:iqty:{batch_id}:{idx}:spacebar_key:{color}:{q}") for q in QUANTITIES]
                send(uid, f"Spacebar ({color})\nChoose quantity:", rows, cols=3)
                return

            if sub == "iqty" and len(parts) == 7 and parts[3].isdigit() and parts[6].isdigit():
                batch_id, idx, size, color_raw, qty = parts[2], int(parts[3]), parts[4], parts[5], int(parts[6])
                color = None if color_raw == "_" else color_raw
                items = db.list_batch_items(batch_id)
                if idx < len(items):
                    db.update_upload_item_config(items[idx]["id"], size, qty, color)
                next_idx = idx + 1
                if next_idx < len(items):
                    send_indiv_prompt(batch_id, next_idx, items)
                else:
                    show_cart(uid, uid)
                return

        # --- Checkout ---
        if action == "checkout" and len(parts) == 2:
            sub = parts[1]
            if sub == "pickup":
                show_checkout_confirm(uid, uid, "pickup", None)
                return
            if sub == "delivery":
                db.set_session(uid, "await_address", {})
                bot.send_message(
                    uid,
                    "Tap '📍 Share my delivery location' below or type your delivery address (neighborhood, street, or landmark):",
                    reply_markup=location_share_keyboard(),
                )
                send(uid, "Or cancel and go back:", [("⬅️ Back to Cart", "callback_data", "cart:view")])
                return
            if sub == "phone":
                sess = db.get_session(uid)
                if sess["action"] != "checkout_ready":
                    show_cart(uid, uid)
                    return
                db.set_session(uid, "await_checkout_phone", sess["data"])
                send(uid, "Enter the 09, 07, or +251 phone number for this order:", [("⬅️ Back to Cart", "callback_data", "cart:view")])
                return
            if sub == "place":
                sess = db.get_session(uid)
                if sess["action"] != "checkout_ready":
                    show_cart(uid, uid)
                    return
                data = sess["data"]
                place_order_from_cart(uid, uid, data["method"], data.get("address"), data["phone"])
                return

        # --- Orders & Staff Fulfillment ---
        if action in ("staffpage", "minepage") and len(parts) == 2 and parts[1].isdigit():
            queue(uid, int(parts[1]), None if admin else uid)
            return

        if action not in ("mine", "staff", "advance", "settle", "deposit", "phone", "artwork", "payshot") or len(parts) < 2:
            return

        try:
            oid = str(UUID(parts[1]))
        except ValueError:
            return

        order = db.order_by_id(oid)
        if not order or (not admin and order["telegram_user_id"] != uid):
            send(uid, "Order unavailable.")
            return

        if action == "payshot" and len(parts) == 2:
            if order["status"] != "pending_deposit":
                send(uid, f"This order is no longer awaiting deposit. Open {MY_ORDERS}.")
                return
            db.set_session(uid, "await_payment_screenshot", {"order_id": oid})
            send(
                uid,
                f"Please send your payment screenshot (photo or PNG/JPEG file) for Order #{oid[:8]}:",
                [("⬅️ Back to Order", "callback_data", f"mine:{oid}")],
            )
            return

        if action == "artwork" and len(parts) == 2:
            if not order.get("batch_id"):
                send(uid, "This order only contains catalog templates.")
                return
            items = db.list_batch_items(order["batch_id"])
            if not items:
                send(uid, "No artwork files found for this order.")
                return
            for idx, it in enumerate(items, 1):
                bot.send_document(uid, it["file_id"], caption=f"Order #{oid[:8]} · {idx}/{len(items)} · {it['file_name']}")
        elif action == "phone" and len(parts) == 2:
            if order["status"] != "pending_deposit":
                send(uid, f"This order is unavailable for phone changes. Open {MY_ORDERS}.")
                return
            bot.send_message(
                uid,
                f"{PHONE_PROMPT}{oid}\nReply with your 09, 07 or +251 phone number.",
                reply_markup=types.ForceReply(input_field_placeholder="Your phone number"),
            )
        elif action == "deposit" and len(parts) == 2:
            if order.get("deposit_paid"):
                send(uid, "Deposit already recorded.")
                return
            updated = db.confirm_deposit(order)
            if not updated:
                send(uid, f"This order has changed. Open {MANAGE_ORDERS} for its current status.")
                return
            notify_customer(bot, updated)
            receipt(uid, updated, admin=True)
        elif action == "settle" and len(parts) == 3 and parts[2] in ("cash", "transfer", "other"):
            advance(uid, order, "completed", uid, parts[2])
        elif action == "advance" and len(parts) == 3 and parts[2] != "completed":
            advance(uid, order, parts[2], uid)
        else:
            receipt(uid, order, admin=admin)
