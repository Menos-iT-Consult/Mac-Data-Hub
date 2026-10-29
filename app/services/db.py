import os
import sqlite3
from contextlib import contextmanager
from .storage import download_db, upload_db
from ..models.schema_global import GLOBAL_SCHEMA
from ..models.schema_user import USER_SCHEMA


def _global_r2_key():
    return "global.db"


def _user_r2_key(user_id: str):
    return f"users/{user_id}.db"


def _global_local_path(config) -> str:
    return os.path.join(config["DB_CACHE_DIR"], config["GLOBAL_DB_NAME"])


def _user_local_path(config, user_id: str) -> str:
    return os.path.join(config["DB_CACHE_DIR"], config["USERS_DB_DIR"], f"{user_id}.db")


def init_global_db(config) -> None:
    """Pull global.db from R2 on every startup so the local file is never stale after a reboot."""
    local = _global_local_path(config)
    found = download_db(config, _global_r2_key(), local)
    _apply_schema(local, GLOBAL_SCHEMA)
    if not found:
        # Brand-new deployment — upload the freshly created schema
        upload_db(config, local, _global_r2_key())


def init_user_db(config, user_id: str) -> None:
    """Pull or create a per-reseller DB and apply schema + migrations."""
    local = _user_local_path(config, user_id)
    is_new = False
    if not os.path.exists(local):
        found = download_db(config, _user_r2_key(user_id), local)
        is_new = not found
    # Always apply schema and migrations regardless of where the file came from
    _apply_schema(local, USER_SCHEMA)
    if is_new:
        upload_db(config, local, _user_r2_key(user_id))


_MIGRATIONS = [
    # Broadcast messages from admin to all resellers
    """CREATE TABLE IF NOT EXISTS broadcasts (
        id         TEXT PRIMARY KEY,
        title      TEXT NOT NULL,
        body       TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    # Add guest_price_pesewas to data_bundles (idempotent — ignored if column exists)
    "ALTER TABLE data_bundles ADD COLUMN guest_price_pesewas INTEGER NOT NULL DEFAULT 0",
    # Push subscriptions table (CREATE TABLE IF NOT EXISTS is idempotent but listed here for clarity)
    """CREATE TABLE IF NOT EXISTS push_subscriptions (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        endpoint TEXT UNIQUE NOT NULL,
        p256dh TEXT NOT NULL,
        auth TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    # Store logo URL
    "ALTER TABLE stores ADD COLUMN logo_url TEXT",
    # Payout profile columns (mobile money recipient saved permanently per reseller)
    "ALTER TABLE users ADD COLUMN payout_recipient_code TEXT",
    "ALTER TABLE users ADD COLUMN momo_network TEXT",
    "ALTER TABLE users ADD COLUMN momo_number TEXT",
    # GigzHub dispatch error storage for failed orders
    "ALTER TABLE orders ADD COLUMN gigzhub_error TEXT",
    # WhatsApp support number per reseller store
    "ALTER TABLE stores ADD COLUMN support_whatsapp TEXT",
    # Withdrawal fee charged by admin (stored on the withdrawal record)
    "ALTER TABLE wallet_withdrawals ADD COLUMN fee_pesewas INTEGER NOT NULL DEFAULT 0",
    # Reseller username for login and display (unique, nullable for existing accounts)
    "ALTER TABLE users ADD COLUMN username TEXT",
    # Referral programme: each reseller's shareable code + who referred them
    "ALTER TABLE users ADD COLUMN referral_code TEXT",
    "ALTER TABLE users ADD COLUMN referred_by TEXT",
    # Sub-agents: an agent (parent_id NULL) can own sub-agents who get their own dashboard + store
    "ALTER TABLE users ADD COLUMN parent_id TEXT",
    "ALTER TABLE users ADD COLUMN is_suspended INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN withdrawals_blocked INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN removed_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_users_parent ON users(parent_id)",
    # Orders from a sub-agent's store: which agent gets a cut and how much
    "ALTER TABLE orders ADD COLUMN agent_id TEXT",
    "ALTER TABLE orders ADD COLUMN agent_profit_pesewas INTEGER NOT NULL DEFAULT 0",
]


def _apply_schema(db_path: str, schema: str) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema)
        for migration in _MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # Column already exists
        conn.commit()


@contextmanager
def global_db(config):
    """Open global DB, commit and upload to R2 on clean exit. Use for writes."""
    local = _global_local_path(config)
    conn = sqlite3.connect(local)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
        upload_db(config, local, _global_r2_key())
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def global_db_read(config):
    """Open global DB read-only — no commit, no R2 upload."""
    local = _global_local_path(config)
    if not os.path.exists(local):
        init_global_db(config)
    conn = sqlite3.connect(local)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def user_db(config, user_id: str):
    """Open reseller DB, commit and upload to R2 on clean exit. Use for writes."""
    local = _user_local_path(config, user_id)
    if not os.path.exists(local):
        init_user_db(config, user_id)
    conn = sqlite3.connect(local)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
        upload_db(config, local, _user_r2_key(user_id))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def user_db_read(config, user_id: str):
    """Open reseller DB read-only — no commit, no R2 upload."""
    local = _user_local_path(config, user_id)
    if not os.path.exists(local):
        init_user_db(config, user_id)
    conn = sqlite3.connect(local)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()
