import os
import hashlib
from datetime import datetime
from flask import Flask, render_template, request, session
from .config import Config
from .services.db import init_global_db, global_db_read


def _static_hash(app, rel_path):
    """Return a short content-hash query string for a static file, e.g. ?h=a1b2c3d4"""
    try:
        full = os.path.join(app.static_folder, rel_path.lstrip("/static/"))
        with open(full, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except Exception:
        return "0"


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="../static")
    app.config.from_object(Config)

    init_global_db(app.config)

    # Allow admin to override API keys via app_settings DB (falls back to env vars)
    try:
        import sqlite3 as _sqlite3
        from .services.db import _global_local_path
        _db_path = _global_local_path(app.config)
        if __import__("os").path.exists(_db_path):
            _conn = _sqlite3.connect(_db_path)
            _conn.row_factory = _sqlite3.Row
            _rows = _conn.execute(
                "SELECT key, value FROM app_settings WHERE key IN "
                "('paystack_secret_key','paystack_public_key','gigzhub_api_key','gigzhub_base_url')"
            ).fetchall()
            _conn.close()
            _overrides = {r["key"]: r["value"] for r in _rows}
            if _overrides.get("paystack_secret_key"):
                app.config["PAYSTACK_SECRET_KEY"] = _overrides["paystack_secret_key"]
            if _overrides.get("paystack_public_key"):
                app.config["PAYSTACK_PUBLIC_KEY"] = _overrides["paystack_public_key"]
            if _overrides.get("gigzhub_api_key"):
                app.config["GIGZHUB_API_KEY"] = _overrides["gigzhub_api_key"]
            if _overrides.get("gigzhub_base_url"):
                app.config["GIGZHUB_BASE_URL"] = _overrides["gigzhub_base_url"].rstrip("/").removesuffix("/offers")
    except Exception:
        pass  # On first boot, app_settings may not have these keys yet

    # Pre-compute hashes once at startup for JS/CSS files that change with deploys
    _hashes = {
        p: _static_hash(app, p)
        for p in [
            "js/auth.js", "js/main.js", "js/wallet.js",
            "js/help-fab.js", "js/referral-popup.js", "css/base.css", "css/dashboard.css",
        ]
    }

    def static_v(rel):
        """Usage in templates: {{ static_v('js/auth.js') }} → /static/js/auth.js?h=abc12345"""
        h = _hashes.get(rel, "0")
        return f"/static/{rel}?h={h}"

    @app.template_filter("hash8")
    def hash8(value):
        """Short stable hash, used to cache-bust link-preview image URLs."""
        import hashlib
        return hashlib.md5(str(value or "").encode()).hexdigest()[:8]

    @app.context_processor
    def inject_globals():
        ctx = {
            "static_v": static_v,
            "now": datetime.utcnow(),
            "config": app.config,
            "request": request,
            "whatsapp_channel_url": "",
            "nav_pending_orders": 0,
            "nav_pending_withdrawals": 0,
            "announcement_text": "",
            "announcement_type": "info",
            "site_logo_url": "",
            # One-shot: set at sign-in, cleared as soon as a page renders it
            "is_sub_agent": bool(session.get("is_sub_agent")),
            "referral_popup_code": session.pop("referral_popup", None) if session.get("role") == "reseller" else None,
        }
        role = session.get("role")
        try:
            with global_db_read(app.config) as db:
                wa_row = db.execute(
                    "SELECT value FROM app_settings WHERE key='whatsapp_channel_url'"
                ).fetchone()
                ctx["whatsapp_channel_url"] = wa_row["value"] if wa_row else ""

                ann_text = db.execute(
                    "SELECT value FROM app_settings WHERE key='announcement_text'"
                ).fetchone()
                ann_type = db.execute(
                    "SELECT value FROM app_settings WHERE key='announcement_type'"
                ).fetchone()
                ctx["announcement_text"] = ann_text["value"] if ann_text else ""
                ctx["announcement_type"] = ann_type["value"] if ann_type else "info"

                logo_row = db.execute(
                    "SELECT value FROM app_settings WHERE key='site_logo_url'"
                ).fetchone()
                ctx["site_logo_url"] = logo_row["value"] if logo_row else ""

                if role == "admin" and request.path.startswith("/admin"):
                    ctx["nav_pending_orders"] = db.execute(
                        "SELECT COUNT(*) as c FROM orders WHERE status='pending'"
                    ).fetchone()["c"]
                    ctx["nav_pending_withdrawals"] = db.execute(
                        "SELECT COUNT(*) as c FROM wallet_withdrawals WHERE status='pending'"
                    ).fetchone()["c"]
        except Exception:
            pass
        return ctx

    @app.errorhandler(404)
    def not_found(e):
        return render_template("public/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template("public/404.html"), 500

    # Register blueprints
    from .routes.public import public_bp
    from .routes.auth import auth_bp
    from .routes.reseller import reseller_bp
    from .routes.sub_agents import sub_agents_bp
    from .routes.admin import admin_bp
    from .routes.webhook import webhook_bp
    from .routes.push import push_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(reseller_bp)
    app.register_blueprint(sub_agents_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(push_bp)

    return app
