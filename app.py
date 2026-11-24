"""WSGI endpoints and deployment commands for the sticker ordering bot."""
from datetime import datetime, timedelta, timezone
import hmac
import logging
import re
from uuid import UUID, uuid4

import click
from flask import Flask, abort, make_response, render_template, request
import telebot

from bot.handlers import register_handlers
from config import settings
from services import database as db, storage
from services.notifications import deliver_next
from services.pricing import catalog

logging.basicConfig(level=logging.INFO)
# HTTP clients may otherwise log URLs containing storage paths or tokens.
for name in ("httpx", "httpcore", "telebot", "TeleBot"):
    logging.getLogger(name).setLevel(logging.CRITICAL)

app = Flask(__name__, static_folder=None, template_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
bot = telebot.TeleBot(settings.telegram_bot_token, threaded=False)
register_handlers(bot)


@app.post("/webhook")
def telegram_webhook():
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(header.encode(), settings.telegram_webhook_secret.encode()):
        abort(403)
    data = request.get_json()
    if not isinstance(data, dict) or type(data.get("update_id")) is not int:
        abort(400)
    try:
        update = telebot.types.Update.de_json(data)
    except (KeyError, ValueError, TypeError, AttributeError):
        abort(400)
    lease = str(uuid4())
    claim = db.client().rpc("claim_telegram_update", {"p_id": update.update_id, "p_lease": lease}).execute().data
    if claim != "claimed":
        return "", 200 if claim == "done" else 503
    table = db.client().table("telegram_updates")
    try:
        bot.process_new_updates([update])
    except Exception as exc:
        # A blocked chat cannot receive replies. Retrying this update cannot help.
        if getattr(exc, "error_code", None) != 403:
            table.delete().eq("id", update.update_id).eq("lease", lease).execute()
            raise
    table.update({"done": True}).eq("id", update.update_id).eq("lease", lease).execute()
    return "", 200


@app.errorhandler(Exception)
def service_error(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    app.logger.error("Request failed on %s (%s)", request.path, type(exc).__name__)
    return "Service temporarily unavailable. Please try again.", 503


@app.get("/health")
def health():
    return "ok"


@app.get("/ready")
def ready():
    dependency = "Supabase database"
    try:
        db.client().table("orders").select("id,upload_id,deposit_paid").limit(1).execute()
        dependency = "Supabase artwork storage"
        storage.private_bucket()
        dependency = "Telegram webhook"
        if bot.get_webhook_info(timeout=8).url != settings.webhook_url:
            return "Webhook unavailable", 503
    except Exception as exc:
        error = type(exc).__name__
        code = getattr(exc, "code", None)
        if isinstance(code, str) and re.fullmatch(r"PGRST[0-9]{3}|[0-9A-Z]{5}", code):
            error += f" {code}"
        app.logger.error("Readiness check failed for %s (%s)", dependency, error)
        return f"{dependency} unavailable", 503
    return "ready"


@app.get("/webapp")
@app.get("/static/webapp.html")
def webapp():
    draft = request.args.get("draft", "")
    upload = preview = None
    try:
        upload_id = str(UUID(draft))
    except ValueError:
        upload_id = None
    if upload_id:
        upload = db.active_upload(upload_id)
        if upload and not upload["ordered"]:
            preview = storage.preview_url(upload)
    response = make_response(render_template("webapp.html", catalog=catalog(), draft=upload_id,
                             preview=preview, pickup=settings.pickup_instructions,
                             fit={"spacebar_key": settings.spacebar_fit, "phone_back": settings.phone_fit},
                             ordered=bool(upload and upload["ordered"])))
    response.headers.update({"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})
    return response


@app.cli.command("register-webhook")
def register_webhook():
    if not bot.set_webhook(url=settings.webhook_url, secret_token=settings.telegram_webhook_secret,
                           allowed_updates=["message", "callback_query"]):
        raise click.ClickException("Webhook registration failed.")
    click.echo("Telegram webhook registered.")


@app.cli.command("maintain")
def maintain():
    """Retry queued notifications and remove expired drafts and update receipts."""
    for _ in range(100):
        if deliver_next(bot) is None:
            break
    storage.cleanup()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    db.client().table("telegram_updates").delete().lt("expires_at", cutoff).execute()
