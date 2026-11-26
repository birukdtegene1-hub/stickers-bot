"""Entry point for running the Sticker Shop Telegram bot via long polling."""
import logging

import telebot

from bot.handlers import register_handlers
from config import settings
from services import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
for name in ("httpx", "httpcore", "telebot", "TeleBot"):
    logging.getLogger(name).setLevel(logging.WARNING)


def create_bot() -> telebot.TeleBot:
    db.init_db()
    bot = telebot.TeleBot(settings.telegram_bot_token, threaded=False)
    register_handlers(bot)
    return bot


def main():
    bot = create_bot()
    bot.remove_webhook()
    logging.info("Sticker Shop bot is running (long polling)...")
    bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
