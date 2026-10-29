GLOBAL_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Platform-wide settings (registration fee, platform name, etc.)
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO app_settings (key, value) VALUES
    ('platform_name',                   'Mac Data Hub'),
    ('reseller_registration_fee_pesewas','5000'),
    ('min_withdrawal_pesewas',           '10000');

-- Resellers (admin is env-only, not stored here)
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,           -- UUID
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    phone         TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'reseller',
    wallet_pesewas INTEGER NOT NULL DEFAULT 0,
    is_active     INTEGER NOT NULL DEFAULT 0, -- 1 after registration fee paid
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    payout_recipient_code TEXT,              -- Paystack transfer recipient code
    momo_network          TEXT,              -- mtn | telecel | airteltigo
    momo_number           TEXT               -- registered mobile money number
);

-- Reseller storefronts
CREATE TABLE IF NOT EXISTS stores (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    slug        TEXT UNIQUE NOT NULL,
    store_name  TEXT NOT NULL,
    description TEXT,
    logo_url    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Master bundle catalogue (admin-managed)
CREATE TABLE IF NOT EXISTS data_bundles (
    id              TEXT PRIMARY KEY,
    network         TEXT NOT NULL,            -- mtn | telecel | airteltigo
    offer_slug      TEXT NOT NULL,            -- GigzHub offer slug
    label           TEXT NOT NULL,            -- e.g. "1GB - 30 Days"
    volume_mb       INTEGER NOT NULL,
    validity_days   INTEGER NOT NULL,
    base_price_pesewas  INTEGER NOT NULL,
    guest_price_pesewas INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-store markup prices set by resellers
CREATE TABLE IF NOT EXISTS store_pricing (
    id             TEXT PRIMARY KEY,
    store_id       TEXT NOT NULL REFERENCES stores(id),
    bundle_id      TEXT NOT NULL REFERENCES data_bundles(id),
    price_pesewas  INTEGER NOT NULL,
    UNIQUE(store_id, bundle_id)
);

-- Reseller registration fee payments
CREATE TABLE IF NOT EXISTS reseller_registrations (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id),
    amount_pesewas      INTEGER NOT NULL,
    paystack_reference  TEXT UNIQUE,
    status              TEXT NOT NULL DEFAULT 'pending', -- pending | paid
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Wallet withdrawal requests
CREATE TABLE IF NOT EXISTS wallet_withdrawals (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    amount_pesewas  INTEGER NOT NULL,
    mobile_number   TEXT NOT NULL,
    network         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending | processing | paid | failed
    paystack_transfer_code TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Web Push subscriptions (one row per browser/device per user)
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          TEXT PRIMARY KEY,
    user_id     TEXT,                               -- NULL = anonymous guest
    endpoint    TEXT UNIQUE NOT NULL,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Referral bonuses credited to a referrer's wallet (one per order)
CREATE TABLE IF NOT EXISTS referral_earnings (
    id             TEXT PRIMARY KEY,
    order_id       TEXT UNIQUE NOT NULL REFERENCES orders(id),
    referrer_id    TEXT NOT NULL REFERENCES users(id),
    referred_id    TEXT NOT NULL REFERENCES users(id),
    amount_pesewas INTEGER NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Guest orders (store_id NULL = direct/admin store purchase)
CREATE TABLE IF NOT EXISTS orders (
    id                  TEXT PRIMARY KEY,
    store_id            TEXT REFERENCES stores(id),
    bundle_id           TEXT NOT NULL REFERENCES data_bundles(id),
    customer_phone      TEXT NOT NULL,
    customer_email      TEXT NOT NULL,
    network             TEXT NOT NULL,
    volume_mb           INTEGER NOT NULL,
    amount_pesewas      INTEGER NOT NULL,
    base_cost_pesewas   INTEGER NOT NULL,
    profit_pesewas      INTEGER NOT NULL DEFAULT 0,
    paystack_reference  TEXT UNIQUE,
    status              TEXT NOT NULL DEFAULT 'pending', -- pending | paid | dispatched | failed
    gigzhub_order_id    TEXT,
    gigzhub_error       TEXT,              -- last dispatch error message
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
