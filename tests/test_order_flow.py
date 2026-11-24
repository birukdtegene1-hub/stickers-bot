"""Regression tests. External Telegram, storage, and database calls are isolated."""
from contextlib import ExitStack
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch
import json
import os
from uuid import uuid4

from PIL import Image

TEST_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:offline", "TELEGRAM_WEBHOOK_SECRET": "telegram-test",
    "PA_DOMAIN": "shop.invalid", "WEBAPP_URL": "", "SUPABASE_URL": "https://store.invalid",
    "SUPABASE_KEY": "offline", "ADMIN_TELEGRAM_IDS": "7",
}
with patch.dict(os.environ, TEST_ENV):
    import app
    from bot import handlers
    from bot.webapp_data import parse_webapp_data, PayloadValidationError
    from config import load_config
    from services import database as db, storage
    from services.pricing import calculate_order


class OrderFlowTests(TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.uid = str(uuid4())
        self.payload = dict(upload_id=self.uid, customer_phone="+251 912 345 678", delivery_method="pickup",
                            items=[dict(size="40x40", quantity=5)])
        self.order = dict(id=str(uuid4()), items=[], subtotal="300", total="285", discount_percent=5,
                          telegram_user_id=13, customer_phone="0912345678", deposit_amount="142.50",
                          status="pending_deposit", deposit_paid=False, delivery_method="pickup")

    def test_supabase_rest_url_is_normalized_to_project_root(self):
        for url in ("https://store.invalid", "https://store.invalid/rest/v1/"):
            with self.subTest(url=url), patch.dict(os.environ, TEST_ENV | {"SUPABASE_URL": url}):
                self.assertEqual(load_config().supabase_url, "https://store.invalid")

    def test_parse_and_price(self):
        data = parse_webapp_data(json.dumps(self.payload))
        self.assertEqual(data.customer_phone, "0912345678")
        self.assertEqual(str(calculate_order([(i.size, i.quantity) for i in data.items]).deposit), "142.50")
        for fields in ({"customer_phone": 12}, {"delivery_method": "delivery", "delivery_address": "  "},
                       {"items": [{"size": [], "quantity": 1}]}, {"items": [{"size": "20x20", "quantity": True}]},
                       {"items": [{"size": "spacebar_key", "quantity": 1, "keyboard_color": "orange"}]}):
            with self.subTest(fields=fields), self.assertRaises(PayloadValidationError):
                parse_webapp_data(json.dumps(self.payload | fields))

    def test_telegram_replay_and_transient_error(self):
        self.assertEqual(self.client.post("/webhook", json={"update_id": 1}).status_code, 403)
        headers = {"X-Telegram-Bot-Api-Secret-Token": "telegram-test"}
        with patch.object(db, "client") as client, patch.object(app.bot, "process_new_updates") as process:
            rpc = client.return_value.rpc.return_value.execute
            rpc.return_value.data = "claimed"
            process.side_effect = TimeoutError()
            def post():
                return self.client.post("/webhook", json={"update_id": 1}, headers=headers).status_code
            self.assertEqual(post(), 503)
            process.side_effect = None
            self.assertEqual(post(), 200)
            rpc.return_value.data = "done"
            self.assertEqual(post(), 200)
            self.assertEqual(process.call_count, 2)

    def test_removed_payment_endpoints(self):
        self.assertEqual(self.client.post("/chapa-webhook", json={}).status_code, 404)
        self.assertEqual(self.client.get("/chapa-webhook").status_code, 404)
        self.assertEqual(self.client.get("/chapa-callback").status_code, 404)

    def message(self, uid, **content):
        message = {"message_id": 10, "date": 1, "chat": {"id": uid, "type": "private"},
                   "from": {"id": uid, "is_bot": False, "first_name": "User"}, **content}
        app.bot.process_new_updates([app.telebot.types.Update.de_json({"update_id": 10, "message": message})])

    def test_start_opens_shop_and_staff_menu_is_restricted(self):
        with patch.object(app.bot, "send_message") as send, patch.object(db, "list_orders", return_value=[]) as orders:
            for uid in (13, 7):
                self.message(uid, text="/start")
                markup = json.loads(send.call_args.kwargs["reply_markup"].to_json())
                buttons = {button["text"]: button for row in markup["keyboard"] for button in row}
                self.assertEqual(buttons["Open sticker shop"]["web_app"]["url"], app.settings.webapp_url)
                self.assertIn("Upload artwork", buttons)
                self.assertIn("My orders", buttons)
                self.assertEqual("Manage orders" in buttons, uid == 7)
            self.message(13, text="Manage orders")
            orders.assert_not_called()
            self.message(7, text="Manage orders")
            orders.assert_called_once_with(0, None)

    def test_storefront_shortcuts_use_the_chat_workflow(self):
        page = self.client.get("/webapp")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Custom stickers", page.data)
        with patch.object(app.bot, "send_message") as send, patch.object(db, "list_orders", return_value=[]) as orders:
            self.message(13, web_app_data={"data": "upload-artwork", "button_text": "Open sticker shop"})
            self.assertIn("paperclip", send.call_args.args[1])
            self.message(13, web_app_data={"data": "view-orders", "button_text": "Open sticker shop"})
            self.assertIn("My orders", send.call_args.args[1])
            self.message(13, text="My orders")
            self.assertEqual([call.args for call in orders.call_args_list], [(0, 13), (0, 13)])

    def test_upload_validation(self):
        image = BytesIO()
        Image.new("RGB", (2, 2)).save(image, "PNG")
        self.assertEqual(storage.validate_image(image.getvalue(), "image/png"), image.getvalue())
        with self.assertRaises(storage.ArtworkError):
            storage.validate_image(image.getvalue(), "image/jpeg")
        with self.assertRaises(storage.ArtworkError):
            storage.validate_image(b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>', "image/svg+xml")
        self.assertTrue(storage.validate_image(b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>', "image/svg+xml"))

    def test_submission_uses_owned_artwork_and_reuses_order(self):
        bot = app.bot
        message = {"message_id": 99, "date": 1, "chat": {"id": 13, "type": "private"},
                   "from": {"id": 13, "is_bot": False, "first_name": "Test"},
                   "web_app_data": {"data": json.dumps(self.payload), "button_text": "Configure stickers"}}
        update = app.telebot.types.Update.de_json({"update_id": 99, "message": message})
        upload = dict(id=self.uid, owner_id=13, extension="png")
        with ExitStack() as stack:
            lookup = stack.enter_context(patch.object(db, "order_by", return_value=None))
            stack.enter_context(patch.object(db, "active_upload", return_value=upload))
            create = stack.enter_context(patch.object(db, "save_order", side_effect=lambda data: data | {"id": self.order["id"]}))
            send = stack.enter_context(patch.object(bot, "send_message"))
            bot.process_new_updates([update])
            data = create.call_args.args[0]
            self.assertEqual(data["deposit_amount"], "142.50")
            self.assertEqual(data["image_url"], storage.object_path(upload))
            self.assertIn("142.50 ETB", send.call_args.args[1])
            self.assertFalse(data["deposit_paid"])
            self.assertIn("shop will contact you", send.call_args.args[1])
            receipt_buttons = send.call_args.kwargs["reply_markup"].to_dict()["inline_keyboard"]
            self.assertEqual(receipt_buttons[0][0]["text"], "Update phone number")
            menu = json.loads(send.call_args_list[-2].kwargs["reply_markup"].to_json())
            self.assertTrue(menu["is_persistent"])
            lookup.return_value = data | {"id": self.order["id"], "chapa_checkout_url": "https://old.invalid/pay"}
            bot.process_new_updates([update])
            create.assert_called_once()
            self.assertEqual(send.call_args.kwargs["reply_markup"].to_dict()["inline_keyboard"], receipt_buttons)
            lookup.return_value = None
            upload["owner_id"] = 14
            bot.process_new_updates([update])
            create.assert_called_once()
            self.assertIn("unavailable", send.call_args.args[1])

    def test_fulfillment(self):
        self.assertIsNone(handlers.next_status(self.order))
        ready = self.order | {"status": "out_for_delivery", "deposit_paid": True}
        self.assertEqual(handlers.status_label(ready), "Ready for pickup")
        self.assertEqual(handlers.next_status(ready), "completed")
        self.assertEqual(handlers.status_label(ready | {"status": "completed"}), "Collected")
        self.assertEqual(handlers.status_label(ready | {"delivery_method": "delivery"}), "Dispatched")

    def test_form_hides_expired_draft(self):
        with patch.object(db, "active_upload", return_value=None):
            response = self.client.get("/webapp?draft=" + self.uid)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b'<form id="form"', response.data)

    def click(self, uid, data, chat_type="private"):
        user = {"id": uid, "is_bot": False, "first_name": "User"}
        message = {"message_id": 2, "date": 1, "chat": {"id": uid, "type": chat_type}, "text": "Order"}
        query = {"id": "button", "from": user, "chat_instance": "chat", "message": message, "data": data}
        app.bot.process_new_updates([app.telebot.types.Update.de_json({"update_id": 2, "callback_query": query})])

    def test_only_staff_can_confirm_deposit_once(self):
        oid = self.order["id"]
        button = f"deposit:{oid}"
        paid = self.order | {"deposit_paid": True, "status": "deposit_confirmed"}
        with ExitStack() as stack:
            lookup = stack.enter_context(patch.object(db, "order_by", return_value=self.order))
            record = stack.enter_context(patch.object(db, "confirm_deposit", return_value=paid))
            notify = stack.enter_context(patch.object(handlers, "deliver_next"))
            stack.enter_context(patch.object(db, "notification_status", return_value=None))
            stack.enter_context(patch.object(storage, "print_url", return_value=None))
            stack.enter_context(patch.object(app.bot, "answer_callback_query"))
            send = stack.enter_context(patch.object(app.bot, "send_message"))
            self.click(13, button)
            self.click(7, button, "group")
            lookup.assert_not_called()
            self.click(7, f"staff:{oid}")
            keyboard = send.call_args.kwargs["reply_markup"].to_dict()["inline_keyboard"]
            self.assertIn(button, [b["callback_data"] for row in keyboard for b in row])
            for old_action, uid in (("pay", 13), ("checkpay", 13), ("reconcile", 7)):
                self.click(uid, f"{old_action}:{oid}")
            record.assert_not_called()
            self.click(7, button)
            record.assert_called_once_with(self.order)
            self.assertIn("Deposit received", send.call_args.args[1])
            lookup.return_value = paid
            self.click(7, button)
            record.assert_called_once()
            notify.assert_called_once_with(app.bot, oid)
            self.assertIn("already recorded", send.call_args.args[1])

    def test_confirmation_preserves_historical_fulfillment_stage(self):
        for stage in ("pending_deposit", "printing", "out_for_delivery", "completed"):
            order = self.order | {"status": stage, "deposit_paid": None}
            with self.subTest(stage=stage), patch.object(db, "client") as client:
                update = client.return_value.table.return_value.update
                db.confirm_deposit(order)
                values = update.call_args.args[0]
                self.assertTrue(values["deposit_paid"])
                if stage == "pending_deposit":
                    self.assertEqual(values["status"], "deposit_confirmed")
                else:
                    self.assertNotIn("status", values)

    def test_staff_button_enforces_access_and_reports_delivery(self):
        order = self.order | {"status": "printing", "deposit_paid": True}
        button = f"advance:{order['id']}:out_for_delivery"
        with ExitStack() as stack:
            lookup = stack.enter_context(patch.object(db, "order_by", return_value=order))
            advance = stack.enter_context(patch.object(db, "advance_order", return_value=order))
            stack.enter_context(patch.object(db, "notification_status", return_value="failed"))
            notify = stack.enter_context(patch.object(handlers, "deliver_next"))
            stack.enter_context(patch.object(app.bot, "answer_callback_query"))
            send = stack.enter_context(patch.object(app.bot, "send_message"))
            self.click(8, button)
            self.click(7, button, "group")
            lookup.assert_not_called()
            self.click(7, button)
            advance.assert_called_once_with(order, "out_for_delivery", 7, None)
            notify.assert_called_once()
            self.assertIn("Notification failed", send.call_args.args[1])
            order["deposit_paid"] = False
            self.click(7, button)
            advance.assert_called_once()

    def test_customer_receipt_requires_ownership(self):
        owner = 13
        call = {"id": "button", "from": {"id": owner, "is_bot": False, "first_name": "Test"},
                "chat_instance": "chat", "data": f"mine:{self.order['id']}",
                "message": {"message_id": 1, "date": 1, "chat": {"id": owner, "type": "private"}, "text": "Orders"}}
        with patch.object(db, "order_by", return_value=self.order | {"telegram_user_id": 14}), \
                patch.object(app.bot, "answer_callback_query"), patch.object(app.bot, "send_message") as send:
            app.bot.process_new_updates([app.telebot.types.Update.de_json({"update_id": 42, "callback_query": call})])
            self.assertEqual(send.call_args.args, (owner, "Order unavailable."))

    def test_phone_button_only_uses_replies_to_the_bot(self):
        order_id = self.order["id"]
        updated = self.order | {"customer_phone": "0712345678"}
        with ExitStack() as stack:
            stack.enter_context(patch.object(db, "order_by", return_value=self.order))
            save = stack.enter_context(patch.object(db, "correct_contact_phone", return_value=updated))
            stack.enter_context(patch.object(app.bot, "answer_callback_query"))
            send = stack.enter_context(patch.object(app.bot, "send_message"))
            self.click(7, f"phone:{order_id}")
            self.assertIn("unavailable", send.call_args.args[1])
            self.click(13, f"phone:{order_id}")
            self.assertTrue(json.loads(send.call_args.kwargs["reply_markup"].to_json())["force_reply"])
            prompt = {"message_id": 11, "date": 1, "chat": {"id": 13, "type": "private"},
                      "from": {"id": 13, "is_bot": False, "first_name": "User"}, "text": send.call_args.args[1]}
            self.message(13, text="+251 712 345 678", reply_to_message=prompt)
            save.assert_not_called()
            prompt["from"] = {"id": int(app.bot.token.partition(":")[0]), "is_bot": True, "first_name": "Shop"}
            self.message(13, text="+251 712 345 678", reply_to_message=prompt)
            save.assert_called_once_with(self.order, 13, "0712345678")
            self.assertIn("0712345678", send.call_args.args[1])
