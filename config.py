"""Application configuration loaded from environment variables and .env."""
from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Set {name} in .env or environment before starting the bot.")
    return value


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    storage_channel_id: int
    database_path: Path
    admin_telegram_ids: tuple[int, ...]
    payment_instructions: str
    pickup_instructions: str
    spacebar_fit: str
    phone_fit: str


def load_config() -> Config:
    channel_raw = required("STORAGE_CHANNEL_ID")
    try:
        storage_channel_id = int(channel_raw)
    except ValueError as exc:
        raise RuntimeError("STORAGE_CHANNEL_ID must be a numeric Telegram chat ID (e.g. -100...).") from exc

    db_raw = os.getenv("DATABASE_PATH", "").strip()
    db_path = Path(db_raw) if db_raw else BASE_DIR / "data" / "stickers.db"
    admin_ids = tuple(int(v) for v in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if v.strip())

    return Config(
        telegram_bot_token=required("TELEGRAM_BOT_TOKEN"),
        storage_channel_id=storage_channel_id,
        database_path=db_path,
        admin_telegram_ids=admin_ids,
        payment_instructions=os.getenv(
            "PAYMENT_INSTRUCTIONS",
            "Pay the 50% deposit via Telebirr or bank transfer. Our staff will confirm receipt and start printing.",
        ),
        pickup_instructions=os.getenv(
            "PICKUP_INSTRUCTIONS",
            "Wait for the pickup message before travelling. Contact the shop for the collection address.",
        ),
        spacebar_fit=os.getenv(
            "SPACEBAR_FIT",
            "Standard 6.25u mechanical spacebar (confirm custom sizes with the shop).",
        ),
        phone_fit=os.getenv(
            "PHONE_BACK_FIT",
            "Confirm your phone model with the shop before ordering.",
        ),
    )


settings = load_config()
