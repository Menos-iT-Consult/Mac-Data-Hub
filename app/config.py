import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ["FLASK_SECRET_KEY"]
    ENV = os.getenv("FLASK_ENV", "production")
    DEBUG = ENV == "development"

    # Admin (env-only, never in DB)
    ADMIN_EMAIL = os.environ["ADMIN_EMAIL"]
    ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]

    # Paystack
    PAYSTACK_SECRET_KEY = os.environ["PAYSTACK_SECRET_KEY"]
    PAYSTACK_PUBLIC_KEY = os.environ["PAYSTACK_PUBLIC_KEY"]

    # GigzHub
    GIGZHUB_API_KEY = os.environ["GIGZHUB_API_KEY"]
    GIGZHUB_BASE_URL = "https://gigzhub.net/api/v1"
    # Path used to look up one order's live status; {id} = GigzHub's order id.
    # Matches GigzHub's documented GET /api/v1/order/status/:orderIdOrReference
    GIGZHUB_STATUS_PATH = os.getenv("GIGZHUB_STATUS_PATH", "/order/status/{id}")
    # Seconds between automatic status checks (0 turns the background sync off)
    ORDER_SYNC_INTERVAL = int(os.getenv("ORDER_SYNC_INTERVAL", "60"))

    # Cloudflare R2 — private bucket (databases)
    R2_ACCOUNT_ID        = os.environ["R2_ACCOUNT_ID"]
    R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
    R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
    R2_BUCKET_NAME       = os.environ["R2_BUCKET_NAME"]
    R2_ENDPOINT_URL      = os.environ["R2_ENDPOINT_URL"]

    # Cloudflare R2 — public assets bucket (logos, media)
    # Same API token, same endpoint — only bucket name and public URL differ
    R2_ASSETS_BUCKET_NAME = os.environ["R2_ASSETS_BUCKET_NAME"]
    R2_ASSETS_PUBLIC_URL  = os.environ["R2_ASSETS_PUBLIC_URL"]

    # App
    APP_URL = os.getenv("APP_URL", "http://localhost:5000")

    # Local DB cache dir (temp disk, synced to/from R2)
    DB_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    GLOBAL_DB_NAME = "global.db"
    USERS_DB_DIR = "users"
