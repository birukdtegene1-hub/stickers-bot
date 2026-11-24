"""Deliver status notifications from the transactional outbox."""
import logging
from services import database as db
from config import settings

TEXT = {
    "deposit_confirmed": "Deposit received.",
    "printing": "Your stickers are being printed.",
    "ready_for_pickup": "Your stickers are ready for pickup.",
    "out_for_delivery": "Your stickers have been dispatched.",
    "collected": "Collection and final payment recorded. Thank you!",
    "delivered": "Delivery and final payment recorded. Thank you!",
}


def deliver_next(bot, order_id=None):
    job = db.first(db.client().rpc("claim_notification", {"p_order": order_id}))
    if not job:
        return None
    result = db.client().table("order_notifications")
    try:
        text = TEXT[job["event"]]
        if job["event"] == "ready_for_pickup":
            text += " " + settings.pickup_instructions
        bot.send_message(job["chat_id"], f"Order #{job['order_id'][:8]} · {text}")
    except Exception as exc:
        permanent = getattr(exc, "error_code", None) in (400, 403)
        if permanent:
            result.update({"failed": True}).eq("id", job["id"]).execute()
        logging.warning("Notification %s delivery failed (%s)", job["id"], type(exc).__name__)
        return "failed" if permanent else "queued"
    result.update({"sent_at": db.now()}).eq("id", job["id"]).execute()
    return job["event"]
