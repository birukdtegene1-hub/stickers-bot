"""SQLite persistence for upload batches, sticker templates, and orders."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from config import settings

PAGE_SIZE = 8

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    telegram_user_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    reg_step TEXT NOT NULL DEFAULT 'ask_name' CHECK (reg_step IN ('ask_name', 'ask_phone', 'done')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS upload_batches (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'ordered', 'cleared')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_owner_status
    ON upload_batches(owner_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS upload_items (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES upload_batches(id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    channel_message_id INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    extension TEXT NOT NULL,
    size TEXT,
    quantity INTEGER NOT NULL DEFAULT 1,
    keyboard_color TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(owner_id, telegram_message_id)
);

CREATE TABLE IF NOT EXISTS cart_templates (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    template_id TEXT NOT NULL REFERENCES templates(id),
    size TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    keyboard_color TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cart_templates_owner
    ON cart_templates(owner_id, created_at ASC);

CREATE TABLE IF NOT EXISTS user_sessions (
    telegram_user_id INTEGER PRIMARY KEY,
    action TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_batch
    ON upload_items(batch_id, created_at ASC);

CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    category_name TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    default_size TEXT NOT NULL,
    custom_price TEXT,
    sample_file_id TEXT,
    sample_channel_msg_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    accent_color TEXT NOT NULL DEFAULT '#1b5845',
    svg_preview TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sticker_sizes (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    price_etb TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS discount_tiers (
    min_qty INTEGER PRIMARY KEY,
    percent INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_accounts (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    account_name TEXT NOT NULL,
    account_number TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shop_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL,
    telegram_chat_id INTEGER NOT NULL,
    customer_name TEXT NOT NULL DEFAULT '',
    batch_id TEXT REFERENCES upload_batches(id),
    items_json TEXT NOT NULL,
    subtotal TEXT NOT NULL,
    discount_percent INTEGER NOT NULL,
    total TEXT NOT NULL,
    deposit_amount TEXT NOT NULL,
    delivery_method TEXT NOT NULL CHECK (delivery_method IN ('pickup', 'delivery')),
    delivery_address TEXT,
    customer_phone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_deposit'
        CHECK (status IN ('pending_deposit', 'deposit_confirmed', 'printing', 'out_for_delivery', 'completed')),
    deposit_paid INTEGER NOT NULL DEFAULT 0,
    balance_method TEXT CHECK (balance_method IN ('cash', 'transfer', 'other')),
    balance_received_by INTEGER,
    balance_received_at TEXT,
    payment_screenshot_file_id TEXT,
    payment_screenshot_msg_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_customer
    ON orders(telegram_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_orders_status
    ON orders(status, created_at DESC);
"""


def _svg(bg: str, fg: str, accent: str, shape: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120">'
        f'<rect width="120" height="120" rx="24" fill="{bg}"/>'
        f'<rect x="8" y="8" width="104" height="104" rx="20" fill="none" stroke="{accent}" stroke-width="3" stroke-dasharray="6 4"/>'
        f'{shape.format(fg=fg, accent=accent)}</svg>'
    )


SEED_TEMPLATES = [
    (
        "dev-terminal", "tech", "Tech & Dev", "Terminal >_ ",
        "Clean monospace developer badge for laptops", "40x40", "#10b981",
        _svg("#0f172a", "#f8fafc", "#10b981",
             '<path d="M34 46l16 14-16 14" stroke="{accent}" stroke-width="7" stroke-linecap="round" stroke-linejoin="round" fill="none"/>'
             '<line x1="56" y1="74" x2="84" y2="74" stroke="{fg}" stroke-width="7" stroke-linecap="round"/>'),
    ),
    (
        "dev-bug-free", "tech", "Tech & Dev", "Works On My Machine",
        "Classic developer humor die-cut sticker", "60x60", "#38bdf8",
        _svg("#1e293b", "#f1f5f9", "#38bdf8",
             '<circle cx="60" cy="54" r="20" fill="{accent}"/>'
             '<path d="M51 54l6 6 12-12" stroke="#0f172a" stroke-width="5" stroke-linecap="round" stroke-linejoin="round" fill="none"/>'
             '<rect x="32" y="84" width="56" height="8" rx="4" fill="{fg}"/>'),
    ),
    (
        "et-bunna", "ethiopia", "Ethiopia & Culture", "Jebena Bunna",
        "Minimal Ethiopian coffee ceremony emblem", "40x40", "#d97706",
        _svg("#fef3c7", "#451a03", "#d97706",
             '<circle cx="60" cy="68" r="20" fill="{fg}"/>'
             '<rect x="55" y="34" width="10" height="18" rx="4" fill="{fg}"/>'
             '<path d="M44 58c-8-4-10-14-4-18" stroke="{accent}" stroke-width="4" stroke-linecap="round" fill="none"/>'),
    ),
    (
        "et-meskel", "ethiopia", "Ethiopia & Culture", "Addis Geometric",
        "Warm geometric star inspired by traditional weaves", "60x60", "#16a34a",
        _svg("#fffbeb", "#14532d", "#eab308",
             '<polygon points="60,22 70,50 98,60 70,70 60,98 50,70 22,60 50,50" fill="{accent}"/>'
             '<circle cx="60" cy="60" r="10" fill="{fg}"/>'),
    ),
    (
        "pop-star-bolt", "pop", "Pop & Aesthetic", "Solar Spark",
        "Bold retro starburst for notebooks and bottles", "40x40", "#f43f5e",
        _svg("#fff1f2", "#881337", "#f43f5e",
             '<polygon points="64,22 42,64 58,64 54,98 78,56 62,56" fill="{accent}"/>'),
    ),
    (
        "pop-smile", "pop", "Pop & Aesthetic", "Acid Smile",
        "Playful minimalist smiley in high-contrast ink", "20x20", "#f59e0b",
        _svg("#fef08a", "#1c1917", "#ca8a04",
             '<circle cx="60" cy="60" r="32" fill="none" stroke="{fg}" stroke-width="5"/>'
             '<circle cx="49" cy="52" r="4" fill="{fg}"/>'
             '<circle cx="71" cy="52" r="4" fill="{fg}"/>'
             '<path d="M45 68c4 9 26 9 30 0" stroke="{fg}" stroke-width="5" stroke-linecap="round" fill="none"/>'),
    ),
    (
        "kb-wave", "keyboard", "Keyboard & Spacebar", "Topo Wave Key",
        "Topographic line art sized for 6.25u spacebars", "spacebar_key", "#6366f1",
        _svg("#1e1b4b", "#e0e7ff", "#818cf8",
             '<rect x="18" y="42" width="84" height="36" rx="10" fill="{accent}"/>'
             '<path d="M24 62c14-10 28 10 42 0s22-8 30 0" stroke="{fg}" stroke-width="4" fill="none" stroke-linecap="round"/>'),
    ),
    (
        "phone-grid", "phone", "Phone Back", "Neo Grid",
        "Clean architectural grid for phone backs & cases", "phone_back", "#14b8a6",
        _svg("#042f2e", "#ccfbf1", "#14b8a6",
             '<rect x="38" y="20" width="44" height="80" rx="10" fill="none" stroke="{accent}" stroke-width="5"/>'
             '<circle cx="60" cy="60" r="12" fill="{fg}"/>'),
    ),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect():
    conn = sqlite3.connect(_db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        order_cols = {row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
        if "customer_name" not in order_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN customer_name TEXT NOT NULL DEFAULT ''")
        if "payment_screenshot_file_id" not in order_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN payment_screenshot_file_id TEXT")
        if "payment_screenshot_msg_id" not in order_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN payment_screenshot_msg_id INTEGER")
        item_cols = {row["name"] for row in conn.execute("PRAGMA table_info(upload_items)").fetchall()}
        if "size" not in item_cols:
            conn.execute("ALTER TABLE upload_items ADD COLUMN size TEXT")
        if "quantity" not in item_cols:
            conn.execute("ALTER TABLE upload_items ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
        if "keyboard_color" not in item_cols:
            conn.execute("ALTER TABLE upload_items ADD COLUMN keyboard_color TEXT")

        tpl_cols = {row["name"] for row in conn.execute("PRAGMA table_info(templates)").fetchall()}
        if "custom_price" not in tpl_cols:
            conn.execute("ALTER TABLE templates ADD COLUMN custom_price TEXT")
        if "sample_file_id" not in tpl_cols:
            conn.execute("ALTER TABLE templates ADD COLUMN sample_file_id TEXT")
        if "sample_channel_msg_id" not in tpl_cols:
            conn.execute("ALTER TABLE templates ADD COLUMN sample_channel_msg_id INTEGER")
        if "active" not in tpl_cols:
            conn.execute("ALTER TABLE templates ADD COLUMN active INTEGER NOT NULL DEFAULT 1")

        # Seed default sizes if empty
        if conn.execute("SELECT COUNT(*) FROM sticker_sizes").fetchone()[0] == 0:
            default_sizes = [
                ("20x20", "20 × 20 mm", "40", 1),
                ("40x40", "40 × 40 mm", "60", 2),
                ("60x60", "60 × 60 mm", "80", 3),
                ("spacebar_key", "Spacebar key", "80", 4),
                ("phone_back", "Phone back", "80", 5),
            ]
            conn.executemany(
                "INSERT OR IGNORE INTO sticker_sizes (key, label, price_etb, sort_order) VALUES (?, ?, ?, ?)",
                default_sizes,
            )

        # Seed default discount tiers if empty
        if conn.execute("SELECT COUNT(*) FROM discount_tiers").fetchone()[0] == 0:
            conn.executemany(
                "INSERT OR IGNORE INTO discount_tiers (min_qty, percent) VALUES (?, ?)",
                [(5, 5), (10, 10), (20, 15)],
            )

        # Seed default shop settings
        conn.execute("INSERT OR IGNORE INTO shop_settings (key, value) VALUES ('deposit_percent', '50')")
        conn.execute(
            "INSERT OR IGNORE INTO shop_settings (key, value) VALUES ('pickup_instructions', ?)",
            (settings.pickup_instructions,),
        )

        # Seed default payment account if empty
        if conn.execute("SELECT COUNT(*) FROM payment_accounts").fetchone()[0] == 0:
            conn.execute(
                """INSERT INTO payment_accounts (id, label, account_name, account_number, instructions, active, created_at)
                   VALUES (?, 'Telebirr', 'Sticker Shop', '0900000000', 'Send 50% deposit and share your name.', 1, ?)""",
                (str(uuid4()), utc_now()),
            )

        for row in SEED_TEMPLATES:
            conn.execute(
                """INSERT OR IGNORE INTO templates
                   (id, category, category_name, title, description, default_size, accent_color, svg_preview)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                row,
            )


def _row_dict(row):
    return dict(row) if row is not None else None


def _order_dict(row):
    if row is None:
        return None
    d = dict(row)
    d["items"] = json.loads(d.pop("items_json"))
    d["deposit_paid"] = bool(d["deposit_paid"])
    return d


# --- Templates ---

def list_templates() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM templates ORDER BY category_name, title"
        ).fetchall()
    return [dict(r) for r in rows]


def get_template(template_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    return _row_dict(row)


# --- Upload Batches & Items ---

def get_or_create_open_batch(owner_id: int) -> dict:
    now = utc_now()
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM upload_batches
               WHERE owner_id = ? AND status = 'open' AND expires_at > ?
               ORDER BY updated_at DESC LIMIT 1""",
            (owner_id, now),
        ).fetchone()
        if row:
            return dict(row)
        batch_id = str(uuid4())
        conn.execute(
            """INSERT INTO upload_batches (id, owner_id, status, created_at, updated_at, expires_at)
               VALUES (?, ?, 'open', ?, ?, ?)""",
            (batch_id, owner_id, now, now, expires),
        )
        row = conn.execute("SELECT * FROM upload_batches WHERE id = ?", (batch_id,)).fetchone()
        return dict(row)


def get_open_batch(owner_id: int) -> dict | None:
    now = utc_now()
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM upload_batches
               WHERE owner_id = ? AND status = 'open' AND expires_at > ?
               ORDER BY updated_at DESC LIMIT 1""",
            (owner_id, now),
        ).fetchone()
    return _row_dict(row)


def get_batch(batch_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM upload_batches WHERE id = ?", (batch_id,)).fetchone()
    return _row_dict(row)


def get_active_batch(batch_id: str) -> dict | None:
    now = utc_now()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM upload_batches WHERE id = ? AND status = 'open' AND expires_at > ?",
            (batch_id, now),
        ).fetchone()
    return _row_dict(row)


def clear_open_batch(owner_id: int) -> int:
    now = utc_now()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE upload_batches SET status = 'cleared', updated_at = ? WHERE owner_id = ? AND status = 'open'",
            (now, owner_id),
        )
        return cur.rowcount


def add_upload_item(
    batch_id: str,
    owner_id: int,
    telegram_message_id: int,
    channel_message_id: int,
    file_id: str,
    file_name: str,
    mime_type: str,
    extension: str,
) -> tuple[dict, bool]:
    """Insert an upload item or return the existing one on duplicate message_id.
    Returns (item_dict, created_bool).
    """
    now = utc_now()
    item_id = str(uuid4())
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM upload_items WHERE owner_id = ? AND telegram_message_id = ?",
            (owner_id, telegram_message_id),
        ).fetchone()
        if existing:
            return dict(existing), False
        conn.execute(
            """INSERT INTO upload_items
               (id, batch_id, owner_id, telegram_message_id, channel_message_id,
                file_id, file_name, mime_type, extension, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item_id, batch_id, owner_id, telegram_message_id, channel_message_id,
             file_id, file_name, mime_type, extension, now),
        )
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        conn.execute(
            "UPDATE upload_batches SET updated_at = ?, expires_at = ? WHERE id = ?",
            (now, expires, batch_id),
        )
        row = conn.execute("SELECT * FROM upload_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row), True


def list_batch_items(batch_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM upload_items WHERE batch_id = ? ORDER BY created_at ASC",
            (batch_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_upload_item(item_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM upload_items WHERE id = ?", (item_id,)).fetchone()
    return _row_dict(row)


def update_upload_item_config(item_id: str, size: str, quantity: int, keyboard_color: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE upload_items SET size = ?, quantity = ?, keyboard_color = ? WHERE id = ?",
            (size, quantity, keyboard_color, item_id),
        )


def bulk_configure_batch(batch_id: str, size: str, quantity: int, keyboard_color: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE upload_items SET size = ?, quantity = ?, keyboard_color = ? WHERE batch_id = ?",
            (size, quantity, keyboard_color, batch_id),
        )


# --- Template Cart & Sessions ---

def list_template_categories(*, include_inactive: bool = False) -> list[tuple[str, str]]:
    with connect() as conn:
        sql = "SELECT DISTINCT category, category_name FROM templates"
        if not include_inactive:
            sql += " WHERE active = 1"
        sql += " ORDER BY category_name"
        rows = conn.execute(sql).fetchall()
    return [(r["category"], r["category_name"]) for r in rows]


def list_templates_by_category(category: str, *, include_inactive: bool = False) -> list[dict]:
    with connect() as conn:
        sql = "SELECT * FROM templates WHERE category = ?"
        if not include_inactive:
            sql += " AND active = 1"
        sql += " ORDER BY title"
        rows = conn.execute(sql, (category,)).fetchall()
    return [dict(r) for r in rows]


def create_template(
    template_id: str,
    category: str,
    category_name: str,
    title: str,
    description: str,
    default_size: str,
    custom_price: str | None = None,
    sample_file_id: str | None = None,
    sample_channel_msg_id: int | None = None,
) -> dict:
    with connect() as conn:
        conn.execute(
            """INSERT INTO templates
               (id, category, category_name, title, description, default_size,
                custom_price, sample_file_id, sample_channel_msg_id, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (template_id, category, category_name, title, description, default_size,
             custom_price, sample_file_id, sample_channel_msg_id),
        )
        row = conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    return dict(row)


def update_template(template_id: str, **fields) -> dict | None:
    allowed = {
        "category", "category_name", "title", "description", "default_size",
        "custom_price", "sample_file_id", "sample_channel_msg_id", "active",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_template(template_id)
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [template_id]
    with connect() as conn:
        conn.execute(f"UPDATE templates SET {cols} WHERE id = ?", vals)
        row = conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    return _row_dict(row)


def delete_template(template_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM cart_templates WHERE template_id = ?", (template_id,))
        conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))


# --- Sizes, Discounts, Payment Accounts & Settings CRUD ---

def list_sizes(*, include_inactive: bool = False) -> list[dict]:
    with connect() as conn:
        sql = "SELECT * FROM sticker_sizes"
        if not include_inactive:
            sql += " WHERE active = 1"
        sql += " ORDER BY sort_order ASC, label ASC"
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def get_size(key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM sticker_sizes WHERE key = ?", (key,)).fetchone()
    return _row_dict(row)


def upsert_size(key: str, label: str, price_etb: str, sort_order: int = 10, active: int = 1) -> dict:
    with connect() as conn:
        conn.execute(
            """INSERT INTO sticker_sizes (key, label, price_etb, sort_order, active)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET label = ?, price_etb = ?, sort_order = ?, active = ?""",
            (key, label, price_etb, sort_order, active, label, price_etb, sort_order, active),
        )
        row = conn.execute("SELECT * FROM sticker_sizes WHERE key = ?", (key,)).fetchone()
    return dict(row)


def delete_size(key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sticker_sizes WHERE key = ?", (key,))


def list_discount_tiers() -> list[tuple[int, int]]:
    with connect() as conn:
        rows = conn.execute("SELECT min_qty, percent FROM discount_tiers ORDER BY min_qty DESC").fetchall()
    return [(r["min_qty"], r["percent"]) for r in rows]


def upsert_discount_tier(min_qty: int, percent: int) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO discount_tiers (min_qty, percent) VALUES (?, ?)
               ON CONFLICT(min_qty) DO UPDATE SET percent = ?""",
            (min_qty, percent, percent),
        )


def delete_discount_tier(min_qty: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM discount_tiers WHERE min_qty = ?", (min_qty,))


def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM shop_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO shop_settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = ?""",
            (key, value, value),
        )


def list_payment_accounts(*, include_inactive: bool = False) -> list[dict]:
    with connect() as conn:
        sql = "SELECT * FROM payment_accounts"
        if not include_inactive:
            sql += " WHERE active = 1"
        sql += " ORDER BY created_at ASC"
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def get_payment_account(account_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM payment_accounts WHERE id = ?", (account_id,)).fetchone()
    return _row_dict(row)


def create_payment_account(label: str, account_name: str, account_number: str, instructions: str = "") -> dict:
    aid = str(uuid4())
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """INSERT INTO payment_accounts (id, label, account_name, account_number, instructions, active, created_at)
               VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (aid, label, account_name, account_number, instructions, now),
        )
        row = conn.execute("SELECT * FROM payment_accounts WHERE id = ?", (aid,)).fetchone()
    return dict(row)


def update_payment_account(account_id: str, **fields) -> dict | None:
    allowed = {"label", "account_name", "account_number", "instructions", "active"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_payment_account(account_id)
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [account_id]
    with connect() as conn:
        conn.execute(f"UPDATE payment_accounts SET {cols} WHERE id = ?", vals)
        row = conn.execute("SELECT * FROM payment_accounts WHERE id = ?", (account_id,)).fetchone()
    return _row_dict(row)


def delete_payment_account(account_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM payment_accounts WHERE id = ?", (account_id,))


def add_cart_template(owner_id: int, template_id: str, size: str, quantity: int = 1, keyboard_color: str | None = None) -> dict:
    now = utc_now()
    cid = str(uuid4())
    with connect() as conn:
        conn.execute(
            """INSERT INTO cart_templates (id, owner_id, template_id, size, quantity, keyboard_color, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cid, owner_id, template_id, size, quantity, keyboard_color, now),
        )
        row = conn.execute("SELECT * FROM cart_templates WHERE id = ?", (cid,)).fetchone()
    return dict(row)


def list_cart_templates(owner_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.*, t.title FROM cart_templates c
               JOIN templates t ON t.id = c.template_id
               WHERE c.owner_id = ? ORDER BY c.created_at ASC""",
            (owner_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def remove_cart_template(owner_id: int, cart_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM cart_templates WHERE id = ? AND owner_id = ?", (cart_id, owner_id))


def clear_cart_templates(owner_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM cart_templates WHERE owner_id = ?", (owner_id,))


def get_session(user_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM user_sessions WHERE telegram_user_id = ?", (user_id,)).fetchone()
    if not row:
        return {"action": "", "data": {}}
    return {"action": row["action"], "data": json.loads(row["data_json"])}


def set_session(user_id: int, action: str = "", data: dict | None = None) -> None:
    now = utc_now()
    payload = json.dumps(data or {})
    with connect() as conn:
        conn.execute(
            """INSERT INTO user_sessions (telegram_user_id, action, data_json, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_user_id) DO UPDATE SET action = ?, data_json = ?, updated_at = ?""",
            (user_id, action, payload, now, action, payload, now),
        )


# --- Users & Registration ---

def get_user(user_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (user_id,)).fetchone()
    return _row_dict(row)


def is_registered(user_id: int) -> bool:
    user = get_user(user_id)
    return bool(user and user["reg_step"] == "done" and user["full_name"] and user["phone"])


def start_registration(user_id: int) -> dict:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """INSERT INTO users (telegram_user_id, full_name, phone, reg_step, created_at, updated_at)
               VALUES (?, '', '', 'ask_name', ?, ?)
               ON CONFLICT(telegram_user_id) DO UPDATE SET reg_step = 'ask_name', updated_at = ?""",
            (user_id, now, now, now),
        )
        row = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (user_id,)).fetchone()
    return dict(row)


def set_user_name(user_id: int, full_name: str) -> dict:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """INSERT INTO users (telegram_user_id, full_name, phone, reg_step, created_at, updated_at)
               VALUES (?, ?, '', 'ask_phone', ?, ?)
               ON CONFLICT(telegram_user_id) DO UPDATE SET full_name = ?, reg_step = 'ask_phone', updated_at = ?""",
            (user_id, full_name, now, now, full_name, now),
        )
        row = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (user_id,)).fetchone()
    return dict(row)


def complete_registration(user_id: int, phone: str) -> dict:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """UPDATE users SET phone = ?, reg_step = 'done', updated_at = ?
               WHERE telegram_user_id = ?""",
            (phone, now, user_id),
        )
        row = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (user_id,)).fetchone()
    return dict(row)


# --- Orders ---

def save_order(data: dict) -> dict:
    now = utc_now()
    order_id = str(uuid4())
    batch_id = data.get("batch_id")
    with connect() as conn:
        if batch_id:
            existing = conn.execute("SELECT * FROM orders WHERE batch_id = ?", (batch_id,)).fetchone()
            if existing:
                return _order_dict(existing)
            cur = conn.execute(
                "UPDATE upload_batches SET status = 'ordered', updated_at = ? WHERE id = ? AND status = 'open'",
                (now, batch_id),
            )
            if cur.rowcount == 0:
                raise ValueError("Upload batch is no longer available.")
        conn.execute(
            """INSERT INTO orders
               (id, telegram_user_id, telegram_chat_id, customer_name, batch_id, items_json, subtotal,
                discount_percent, total, deposit_amount, delivery_method, delivery_address,
                customer_phone, status, deposit_paid, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_deposit', 0, ?, ?)""",
            (
                order_id,
                data["telegram_user_id"],
                data["telegram_chat_id"],
                data.get("customer_name", ""),
                batch_id,
                json.dumps(data["items"]),
                str(data["subtotal"]),
                data["discount_percent"],
                str(data["total"]),
                str(data["deposit_amount"]),
                data["delivery_method"],
                data.get("delivery_address"),
                data["customer_phone"],
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return _order_dict(row)


def order_by_id(order_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return _order_dict(row)


def order_by_batch(batch_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE batch_id = ?", (batch_id,)).fetchone()
    return _order_dict(row)


def list_orders(page: int = 0, owner_id: int | None = None) -> list[dict]:
    offset = page * PAGE_SIZE
    with connect() as conn:
        if owner_id is None:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status != 'completed' ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
                (PAGE_SIZE + 1, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orders WHERE telegram_user_id = ? ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
                (owner_id, PAGE_SIZE + 1, offset),
            ).fetchall()
    return [_order_dict(r) for r in rows]


def confirm_deposit(order: dict) -> dict | None:
    now = utc_now()
    new_status = "deposit_confirmed" if order["status"] == "pending_deposit" else order["status"]
    with connect() as conn:
        cur = conn.execute(
            """UPDATE orders SET deposit_paid = 1, status = ?, updated_at = ?
               WHERE id = ? AND status = ? AND deposit_paid = 0""",
            (new_status, now, order["id"], order["status"]),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order["id"],)).fetchone()
    return _order_dict(row)


def advance_order(order: dict, target: str, admin_id: int, balance_method: str | None = None) -> dict | None:
    now = utc_now()
    with connect() as conn:
        if target == "completed":
            if balance_method not in ("cash", "transfer", "other"):
                raise ValueError("Choose the method used to settle the balance.")
            cur = conn.execute(
                """UPDATE orders
                   SET status = ?, balance_method = ?, balance_received_by = ?,
                       balance_received_at = ?, updated_at = ?
                   WHERE id = ? AND status = ? AND deposit_paid = 1""",
                (target, balance_method, admin_id, now, now, order["id"], order["status"]),
            )
        else:
            cur = conn.execute(
                """UPDATE orders SET status = ?, updated_at = ?
                   WHERE id = ? AND status = ? AND deposit_paid = 1""",
                (target, now, order["id"], order["status"]),
            )
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order["id"],)).fetchone()
    return _order_dict(row)


def correct_contact_phone(order: dict, user_id: int, phone: str) -> dict | None:
    now = utc_now()
    with connect() as conn:
        cur = conn.execute(
            """UPDATE orders SET customer_phone = ?, updated_at = ?
               WHERE id = ? AND telegram_user_id = ? AND status = 'pending_deposit'""",
            (phone, now, order["id"], user_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order["id"],)).fetchone()
    return _order_dict(row)


def attach_payment_screenshot(order_id: str, file_id: str, channel_msg_id: int | None) -> dict | None:
    now = utc_now()
    with connect() as conn:
        cur = conn.execute(
            """UPDATE orders
               SET payment_screenshot_file_id = ?, payment_screenshot_msg_id = ?, updated_at = ?
               WHERE id = ?""",
            (file_id, channel_msg_id, now, order_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return _order_dict(row)


def list_pending_deposit_orders(user_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM orders
               WHERE telegram_user_id = ? AND status = 'pending_deposit' AND deposit_paid = 0
               ORDER BY created_at DESC""",
            (user_id,),
        ).fetchall()
    return [_order_dict(r) for r in rows]
