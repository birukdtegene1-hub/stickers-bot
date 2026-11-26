"""Regression tests for the button-driven Telegram sticker bot."""
from io import BytesIO
import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
from telebot import types

TMP_DB = Path(tempfile.gettempdir()) / f"stickers_bot_test_{uuid4().hex}.db"
TEST_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:offline-test-token",
    "STORAGE_CHANNEL_ID": "-1001234567890",
    "ADMIN_TELEGRAM_IDS": "7",
    "DATABASE_PATH": str(TMP_DB),
}

with patch.dict(os.environ, TEST_ENV):
    import run
    from services import database as db, storage

bot = run.create_bot()


class ButtonBotTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(TMP_DB) + suffix)
            if p.exists():
                p.unlink()

    def setUp(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM orders")
            conn.execute("DELETE FROM cart_templates")
            conn.execute("DELETE FROM upload_items")
            conn.execute("DELETE FROM upload_batches")
            conn.execute("DELETE FROM user_sessions")
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM payment_accounts")
            conn.execute("DELETE FROM templates WHERE id LIKE 'custom-%'")
            conn.execute("DELETE FROM sticker_sizes WHERE key = '80x80'")

    def message(self, uid, message_id=10, **content):
        msg = {
            "message_id": message_id,
            "date": 1,
            "chat": {"id": uid, "type": "private"},
            "from": {"id": uid, "is_bot": False, "first_name": "User"},
            **content,
        }
        bot.process_new_updates([types.Update.de_json({"update_id": message_id, "message": msg})])

    def click(self, uid, data, chat_type="private"):
        user = {"id": uid, "is_bot": False, "first_name": "User"}
        msg = {"message_id": 2, "date": 1, "chat": {"id": uid, "type": chat_type}, "text": "Menu"}
        query = {"id": "cb1", "from": user, "chat_instance": "chat", "message": msg, "data": data}
        bot.process_new_updates([types.Update.de_json({"update_id": 2, "callback_query": query})])

    def register_user(self, uid=13, name="Leul Kassahun", phone="0912345678"):
        db.set_user_name(uid, name)
        db.complete_registration(uid, phone)

    def test_start_registration_flow(self):
        with patch.object(bot, "send_message") as send:
            self.message(13, message_id=1, text="/start")
            self.assertIn("full name", send.call_args.args[1])
            self.message(13, message_id=2, text="Leul Kassahun")
            self.assertIn("phone number", send.call_args.args[1])
            self.message(13, message_id=3, text="+251 912 345 678")
            self.assertIn("Registration complete", send.call_args.args[1])
            user = db.get_user(13)
            self.assertEqual(user["full_name"], "Leul Kassahun")
            self.assertEqual(user["phone"], "0912345678")

    def test_upload_stores_name_and_phone_and_bulk_checkout(self):
        self.register_user(13, "Leul Kassahun", "0912345678")
        png_bytes = BytesIO()
        Image.new("RGB", (4, 4)).save(png_bytes, "PNG")

        with patch.object(storage, "download_telegram_file", return_value=png_bytes.getvalue()), \
             patch.object(storage, "store_in_channel", side_effect=[("f_1", 101), ("f_2", 102)]) as store_mock, \
             patch.object(bot, "send_message") as send, \
             patch.object(bot, "send_photo") as send_photo, \
             patch.object(bot, "answer_callback_query"), \
             patch.object(bot, "edit_message_caption") as edit_cap:
            for idx, name in enumerate(("a.png", "b.png"), 1):
                self.message(
                    13,
                    message_id=20 + idx,
                    document={
                        "file_id": f"orig_{idx}",
                        "file_unique_id": f"uniq_{idx}",
                        "file_name": name,
                        "mime_type": "image/png",
                        "file_size": 100,
                    },
                )
            self.assertEqual(store_mock.call_args.kwargs["full_name"], "Leul Kassahun")
            self.assertEqual(store_mock.call_args.kwargs["phone"], "0912345678")
            batch = db.get_open_batch(13)
            self.assertEqual(len(db.list_batch_items(batch["id"])), 2)

            # Bulk size all 2 stickers as 40x40, 3 each (total 6 -> 5% discount: 360 - 18 = 342.00)
            self.click(13, f"cfg:mode:{batch['id']}")
            self.click(13, f"cfg:bulk:{batch['id']}")
            self.click(13, f"cfg:bsize:{batch['id']}:40x40")
            self.click(13, f"cfg:bqty:{batch['id']}:40x40:_:3")
            self.assertIn("342.00 ETB", send.call_args.args[1])

            # Checkout pickup -> place order
            self.click(13, "checkout:pickup")
            self.click(13, "checkout:place")
            orders = db.list_orders(0, owner_id=13)
            self.assertEqual(len(orders), 1)
            order = orders[0]
            self.assertEqual(order["total"], "342.00")
            self.assertEqual(order["customer_name"], "Leul Kassahun")
            self.assertEqual(edit_cap.call_count, 2)
            self.assertIn("Leul Kassahun", edit_cap.call_args.kwargs["caption"])
            self.assertIn("0912345678", edit_cap.call_args.kwargs["caption"])

    def test_individual_sizing_and_template_cart_and_fulfillment(self):
        self.register_user(13, "Leul Kassahun", "0912345678")
        batch = db.get_or_create_open_batch(13)
        db.add_upload_item(batch["id"], 13, 1, 50, "chan_f1", "one.png", "image/png", "png")

        with patch.object(bot, "send_message"), \
             patch.object(bot, "answer_callback_query"), \
             patch.object(bot, "edit_message_caption"):
            # Individual sizing for 1 sticker
            self.click(13, f"cfg:indiv:{batch['id']}:0")
            self.click(13, f"cfg:isize:{batch['id']}:0:20x20")
            self.click(13, f"cfg:iqty:{batch['id']}:0:20x20:_:2")

            # Add template via buttons (new format: tpl:add:<id>:<size>:<qty>)
            self.click(13, "tpl:cats")
            self.click(13, "tpl:cat:tech")
            self.click(13, "tpl:pick:dev-terminal")
            self.click(13, "tpl:add:dev-terminal:40x40:3")

            # Checkout -> place (2*40 + 3*60 = 260, 5 total -> 5% discount = 247.00)
            self.click(13, "checkout:pickup")
            self.click(13, "checkout:place")
            order = db.list_orders(0, owner_id=13)[0]
            self.assertEqual(order["total"], "247.00")

            # Staff fulfillment
            self.click(7, f"deposit:{order['id']}")
            self.click(7, f"advance:{order['id']}:printing")
            self.click(7, f"advance:{order['id']}:out_for_delivery")
            self.click(7, f"settle:{order['id']}:cash")
            done = db.order_by_id(order["id"])
            self.assertEqual(done["status"], "completed")
            self.assertEqual(done["balance_method"], "cash")

    def test_upload_validation_rejects_bad_svg(self):
        with self.assertRaises(storage.ArtworkError):
            storage.validate_image(b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>', "image/svg+xml")

    def test_visual_contact_sheet_and_preview_generation(self):
        img_bytes = BytesIO()
        Image.new("RGB", (64, 64), (200, 50, 50)).save(img_bytes, "PNG")
        items = [
            {"id": "1", "file_id": "f1", "file_name": "a.png", "extension": "png", "size": "40x40", "quantity": 2},
            {"id": "2", "file_id": "f2", "file_name": "b.png", "extension": "png", "size": None, "quantity": 1},
        ]
        with patch.object(storage, "download_telegram_file", return_value=img_bytes.getvalue()):
            sheet = storage.build_batch_contact_sheet(bot, items)
            self.assertIsNotNone(sheet)
            with Image.open(BytesIO(sheet)) as out:
                self.assertEqual(out.format, "PNG")
            preview = storage.build_sticker_preview(bot, items[0])
            self.assertIsNotNone(preview)

    def test_admin_crud_templates_pricing_and_payment_accounts(self):
        self.register_user(13, "Leul Kassahun", "0912345678")
        # 1. Admin creates a payment account
        with patch.object(bot, "send_message") as send, \
             patch.object(bot, "send_photo"), \
             patch.object(bot, "answer_callback_query"):
            self.click(7, "adm:pay_add")
            self.message(7, message_id=201, text="CBE")
            self.message(7, message_id=202, text="Sticker Shop PLC")
            self.message(7, message_id=203, text="1000123456789")
            self.message(7, message_id=204, text="Send screenshot after transfer")
            accs = db.list_payment_accounts()
            self.assertEqual(len(accs), 1)
            self.assertEqual(accs[0]["label"], "CBE")

            # 2. Admin adds a new size (80x80 -> 100 ETB)
            self.click(7, "adm:size_add")
            self.message(7, message_id=205, text="80x80")
            self.message(7, message_id=206, text="100")
            self.assertEqual(db.get_size("80x80")["price_etb"], "100.00")

            # 3. Admin adds a template with sample photo stored in channel
            with patch.object(storage, "store_template_sample", return_value=("sample_fid_1", 900)) as store_sample:
                self.click(7, "adm:tpl_newcat")
                self.message(7, message_id=207, text="Cars")
                self.message(
                    7,
                    message_id=208,
                    photo=[{"file_id": "raw_photo_1", "file_unique_id": "up1", "width": 100, "height": 100}],
                )
                store_sample.assert_called_once()
                self.message(7, message_id=209, text="BMW M4")
                self.message(7, message_id=210, text="Technical blueprint")
                self.click(7, "adm:tpl_nsize:80x80")
                self.message(7, message_id=211, text="120")

            cars = db.list_templates_by_category("cars")
            self.assertEqual(len(cars), 1)
            tpl = cars[0]
            self.assertEqual(tpl["sample_file_id"], "sample_fid_1")
            self.assertEqual(tpl["custom_price"], "120.00")

        # 4. Customer opens template -> sees sample photo + custom price -> places order -> sees CBE account
        with patch.object(bot, "send_photo") as send_photo, \
             patch.object(bot, "send_message") as send_msg, \
             patch.object(bot, "answer_callback_query"):
            self.click(13, f"tpl:pick:{tpl['id']}")
            send_photo.assert_called_once()
            self.assertEqual(send_photo.call_args.args[1], "sample_fid_1")
            self.assertIn("120.00 ETB", send_photo.call_args.kwargs["caption"])

            self.click(13, f"tpl:add:{tpl['id']}:80x80:1")
            self.click(13, "checkout:pickup")
            self.click(13, "checkout:place")
            receipt_msg = [c.args[1] for c in send_msg.call_args_list if "1000123456789" in c.args[1]]
            self.assertTrue(receipt_msg)

    def test_payment_screenshot_sends_order_package_to_channel(self):
        self.register_user(13, "Leul Kassahun", "0912345678")
        batch = db.get_or_create_open_batch(13)
        db.add_upload_item(batch["id"], 13, 1, 50, "chan_f1", "car.png", "image/png", "png")
        db.update_upload_item_config(db.list_batch_items(batch["id"])[0]["id"], "40x40", 2, None)

        with patch.object(bot, "send_message"), \
             patch.object(bot, "answer_callback_query"), \
             patch.object(bot, "edit_message_caption"):
            self.click(13, "checkout:pickup")
            self.click(13, "checkout:place")
            order = db.list_orders(0, owner_id=13)[0]

            # Customer sends payment screenshot
            with patch.object(bot, "send_media_group") as send_group, \
                 patch.object(bot, "send_photo") as send_photo, \
                 patch.object(bot, "send_document"):
                fake_photo_msg = type("Msg", (), {"message_id": 777, "photo": [type("P", (), {"file_id": "chan_shot_1"})()]})()
                send_group.return_value = [fake_photo_msg]

                self.click(13, f"payshot:{order['id']}")
                self.message(
                    13,
                    message_id=300,
                    photo=[{"file_id": "user_shot_1", "file_unique_id": "us1", "width": 200, "height": 200}],
                )

                # Channel received media group(s) with HTML bold/italic caption
                chan_group_calls = [c for c in send_group.call_args_list if c.args[0] == -1001234567890]
                self.assertEqual(len(chan_group_calls), 2)
                photo_media = chan_group_calls[0].args[1]
                cap = photo_media[0].caption
                self.assertIn("<b>", cap)
                self.assertIn("<i>", cap)
                self.assertIn(order["id"][:8], cap)
                self.assertIn("Leul Kassahun", cap)
                self.assertIn("13", cap)
                self.assertIn("0912345678", cap)
                self.assertIn("car.png", cap)

                # Staff (id=7) also received the payment screenshot
                staff_photo_calls = [c for c in send_photo.call_args_list if c.args[0] == 7]
                self.assertEqual(len(staff_photo_calls), 1)

                updated = db.order_by_id(order["id"])
                self.assertEqual(updated["payment_screenshot_file_id"], "chan_shot_1")
