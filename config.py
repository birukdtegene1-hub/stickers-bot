"""Deployment configuration. Importing this module makes no network requests."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Set {name} before starting the bot.")
    return value


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_webhook_secret: str
    pa_domain: str
    webapp_url: str
    supabase_url: str
    supabase_key: str
    supabase_bucket: str
    legacy_bucket: str
    admin_telegram_ids: tuple[int, ...]
    pickup_instructions: str
    spacebar_fit: str
    phone_fit: str

    @property
    def webhook_url(self):
        return f"https://{self.pa_domain}/webhook"


def load_config():
    domain = required("PA_DOMAIN")
    if not re.fullmatch(r"[A-Za-z0-9.-]+(:[0-9]+)?", domain):
        raise RuntimeError("PA_DOMAIN must be a hostname, optionally with a port.")
    url = os.getenv("WEBAPP_URL", "").strip() or f"https://{domain}/webapp"
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
        raise RuntimeError("WEBAPP_URL must be an absolute HTTPS URL without a query or fragment.")
    secret = required("TELEGRAM_WEBHOOK_SECRET")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
        raise RuntimeError("Invalid TELEGRAM_WEBHOOK_SECRET format.")
    return Config(
        telegram_bot_token=required("TELEGRAM_BOT_TOKEN"), telegram_webhook_secret=secret,
        pa_domain=domain, webapp_url=url, supabase_url=required("SUPABASE_URL").rstrip("/").removesuffix("/rest/v1"),
        supabase_key=required("SUPABASE_KEY"),
        supabase_bucket=os.getenv("ARTWORK_BUCKET", "sticker-artwork-private"),
        legacy_bucket=os.getenv("SUPABASE_BUCKET", "sticker-main-portal-upload"),
        admin_telegram_ids=tuple(int(v) for v in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if v.strip()),
        pickup_instructions=os.getenv("PICKUP_INSTRUCTIONS", "Wait for the pickup message before travelling. Contact the shop for the collection address."),
        spacebar_fit=os.getenv("SPACEBAR_FIT", "Confirm the spacebar dimensions with the shop before ordering."),
        phone_fit=os.getenv("PHONE_BACK_FIT", "Confirm your phone model and dimensions with the shop before ordering."),
    )


settings = load_config()
