"""Database operations performed using the bot's server-side Supabase role."""
from datetime import datetime, timezone
from functools import lru_cache

from supabase import ClientOptions, create_client

from config import settings

PAGE_SIZE = 8


@lru_cache(maxsize=1)
def client():
    return create_client(settings.supabase_url, settings.supabase_key,
                         options=ClientOptions(postgrest_client_timeout=8))


def now():
    return datetime.now(timezone.utc).isoformat()


def first(query):
    rows = query.execute().data
    return rows[0] if rows else None


def order_by(column, value):
    return first(client().table("orders").select("*").eq(column, value).limit(1))


def list_orders(page, owner=None):
    query = client().table("orders").select("*")
    if owner is None:
        query = query.neq("status", "completed")
    else:
        query = query.eq("telegram_user_id", owner)
    return query.order("created_at", desc=True).order("id").range(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).execute().data


def register_upload(owner, message_id, extension):
    table = client().table("artwork_uploads")
    row = first(table.upsert({"owner_id": owner, "message_id": message_id, "extension": extension},
                             on_conflict="owner_id,message_id", ignore_duplicates=True))
    return row or first(table.select("*").eq("owner_id", owner).eq("message_id", message_id))


def active_upload(upload_id):
    return first(client().table("artwork_uploads").select("*").eq("id", upload_id)
                 .eq("deleting", False).gt("expires_at", now()))


def save_order(data):
    row = first(client().table("orders").upsert(data, on_conflict="upload_id", ignore_duplicates=True))
    return row or order_by("upload_id", data["upload_id"])


def confirm_deposit(order):
    values = {"deposit_paid": True, "updated_at": now()}
    if order["status"] == "pending_deposit":
        values["status"] = "deposit_confirmed"
    return first(client().table("orders").update(values).eq("id", order["id"])
                 .eq("status", order["status"])
                 .or_("deposit_paid.is.null,deposit_paid.eq.false"))


def advance_order(order, target, admin, balance_method=None):
    values = {"status": target, "updated_at": now()}
    if target == "completed":
        if balance_method not in ("cash", "transfer", "other"):
            raise ValueError("Choose the method used to settle the balance.")
        values.update(balance_received_at=now(), balance_received_by=admin, balance_method=balance_method)
    return first(client().table("orders").update(values).eq("id", order["id"])
                 .eq("status", order["status"]).eq("deposit_paid", True))


def correct_contact_phone(order, user_id, phone):
    return first(client().table("orders").update({"customer_phone": phone})
                 .eq("id", order["id"]).eq("telegram_user_id", user_id)
                 .eq("status", "pending_deposit"))


def notification_status(order_id, event):
    row = first(client().table("order_notifications").select("sent_at,failed")
                .eq("order_id", order_id).eq("event", event))
    if not row:
        return None
    if row["sent_at"]:
        return "delivered"
    return "failed" if row["failed"] else "queued"
