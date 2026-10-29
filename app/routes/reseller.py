import uuid
from functools import wraps
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, current_app, jsonify)
from werkzeug.security import check_password_hash, generate_password_hash
from ..services.db import global_db, global_db_read, user_db, user_db_read
from ..services.storage import upload_asset, delete_asset
from ..services.paystack import create_transfer_recipient, initiate_transfer, resolve_account
from ..services.pricing_chain import sub_agent_floors

MOMO_BANK_CODES = {"mtn": "MTN", "telecel": "VOD", "airteltigo": "ATL"}

ALLOWED_LOGO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2 MB

reseller_bp = Blueprint("reseller", __name__, url_prefix="/dashboard")


def reseller_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "reseller":
            if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"error": "Session expired. Please log in again."}), 401
            return redirect(url_for("auth.login"))

        # Suspended or removed accounts are signed out on their next request
        with global_db_read(current_app.config) as db:
            acct = db.execute(
                "SELECT is_suspended, removed_at FROM users WHERE id=?", (session.get("user_id"),)
            ).fetchone()
        if not acct or acct["removed_at"] or acct["is_suspended"]:
            session.clear()
            if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"error": "Your account is no longer active."}), 401
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


def _ctx(config):
    uid = session["user_id"]
    with global_db_read(config) as db:
        user = dict(db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
        store_row = db.execute("SELECT * FROM stores WHERE user_id=?", (uid,)).fetchone()
        store = dict(store_row) if store_row else None
    return uid, user, store


@reseller_bp.route("/")
@reseller_required
def dashboard():
    config = current_app.config
    uid, user, store = _ctx(config)

    recent_orders, total_earned, total_orders = [], 0, 0
    with user_db_read(config, uid) as udb:
        rows = udb.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 10").fetchall()
        recent_orders = [dict(r) for r in rows]
        row = udb.execute("SELECT COALESCE(SUM(amount_pesewas),0) AS t FROM earnings").fetchone()
        total_earned = row["t"] if row else 0
        cnt = udb.execute("SELECT COUNT(*) as c FROM orders").fetchone()
        total_orders = cnt["c"] if cnt else 0

    return render_template("reseller/dashboard.html",
                           user=user, store=store,
                           recent_orders=recent_orders,
                           total_earned=total_earned,
                           total_orders=total_orders)


@reseller_bp.route("/referrals")
@reseller_required
def referrals():
    config = current_app.config
    uid, user, store = _ctx(config)
    with global_db_read(config) as db:
        rows = db.execute(
            """SELECT u.full_name, u.created_at,
                      COALESCE((SELECT SUM(amount_pesewas) FROM referral_earnings re
                                WHERE re.referred_id=u.id AND re.referrer_id=?),0) AS earned
               FROM users u WHERE u.referred_by=? ORDER BY u.created_at DESC""",
            (uid, uid)
        ).fetchall()
    referred = [dict(r) for r in rows]
    total_earned = sum(r["earned"] for r in referred)
    return render_template("reseller/referrals.html", user=user, store=store,
                           referred=referred, total_earned=total_earned)


@reseller_bp.route("/notifications")
@reseller_required
def notifications():
    config = current_app.config
    uid, user, _ = _ctx(config)
    last_seen = request.args.get("last_seen", "")
    items = []

    # Orders from the reseller's personal DB — read-only, no R2 upload
    with user_db_read(config, uid) as udb:
        for o in udb.execute(
            "SELECT bundle_label, network, profit_pesewas, created_at "
            "FROM orders ORDER BY created_at DESC LIMIT 8"
        ).fetchall():
            profit = "GHS %.2f" % (o["profit_pesewas"] / 100)
            items.append({
                "icon": "green",
                "msg":  f"<strong>{o['bundle_label'] or o['network'].upper()}</strong> order — +{profit} profit",
                "time": o["created_at"][:16].replace("T", " "),
                "url":  "/dashboard/orders",
                "ts":   o["created_at"],
            })
        unread_orders = udb.execute(
            "SELECT COUNT(*) as c FROM orders WHERE created_at > ?" if last_seen
            else "SELECT COUNT(*) as c FROM orders WHERE created_at >= datetime('now','-24 hours')",
            (last_seen,) if last_seen else ()
        ).fetchone()["c"]

    # Broadcasts from global DB — read-only, no R2 upload
    with global_db_read(config) as gdb:
        for b in gdb.execute(
            "SELECT title, body, created_at FROM broadcasts ORDER BY created_at DESC LIMIT 5"
        ).fetchall():
            items.append({
                "icon": "broadcast",
                "msg":  f"<strong>{b['title']}</strong> — {b['body']}",
                "time": b["created_at"][:16].replace("T", " "),
                "url":  "/dashboard",
                "ts":   b["created_at"],
            })
        unread_broadcasts = gdb.execute(
            "SELECT COUNT(*) as c FROM broadcasts WHERE created_at > ?" if last_seen
            else "SELECT COUNT(*) as c FROM broadcasts WHERE created_at >= datetime('now','-24 hours')",
            (last_seen,) if last_seen else ()
        ).fetchone()["c"]

    items.sort(key=lambda x: x["ts"], reverse=True)
    for item in items:
        del item["ts"]

    return jsonify({"items": items, "unread": unread_orders + unread_broadcasts})


@reseller_bp.route("/orders")
@reseller_required
def orders():
    config = current_app.config
    uid, user, store = _ctx(config)

    with user_db_read(config, uid) as udb:
        rows = udb.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()

    return render_template("reseller/orders.html",
                           user=user, store=store,
                           orders=[dict(r) for r in rows])


@reseller_bp.route("/pricing", methods=["GET", "POST"])
@reseller_required
def pricing():
    config = current_app.config
    uid, user, store = _ctx(config)

    if not store:
        return redirect(url_for("reseller.dashboard"))

    with global_db_read(config) as db:
        bundles = [dict(b) for b in db.execute(
            "SELECT * FROM data_bundles WHERE is_active=1 ORDER BY network, volume_mb"
        ).fetchall()]
        existing = {
            r["bundle_id"]: r["price_pesewas"]
            for r in db.execute(
                "SELECT bundle_id, price_pesewas FROM store_pricing WHERE store_id=?",
                (store["id"],)
            ).fetchall()
        }

    # A sub-agent's "base price" is whatever their agent charges them
    if user.get("parent_id"):
        with global_db_read(config) as db:
            floors = sub_agent_floors(db, user["parent_id"])
        for b in bundles:
            b["base_price_pesewas"] = max(floors.get(b["id"], b["base_price_pesewas"]), b["base_price_pesewas"])
        existing = {k: max(v, next((b["base_price_pesewas"] for b in bundles if b["id"] == k), v))
                    for k, v in existing.items()}

    if request.method == "POST":
        data = request.get_json()
        with global_db(config) as db:
            for bundle_id, price_str in data.items():
                try:
                    price_pesewas = round(float(price_str) * 100)
                except (ValueError, TypeError):
                    continue
                base = next((b["base_price_pesewas"] for b in bundles if b["id"] == bundle_id), None)
                if base is None or price_pesewas < base:
                    continue
                db.execute(
                    """INSERT INTO store_pricing (id, store_id, bundle_id, price_pesewas)
                       VALUES (?,?,?,?)
                       ON CONFLICT(store_id, bundle_id) DO UPDATE SET price_pesewas=excluded.price_pesewas""",
                    (str(uuid.uuid4()), store["id"], bundle_id, price_pesewas)
                )
        return jsonify({"ok": True})

    return render_template("reseller/pricing.html",
                           user=user, store=store,
                           bundles=bundles, existing=existing)


@reseller_bp.route("/wallet")
@reseller_required
def wallet():
    config = current_app.config
    uid, user, store = _ctx(config)

    with global_db_read(config) as db:
        rows = db.execute(
            "SELECT * FROM wallet_withdrawals WHERE user_id=? ORDER BY created_at DESC",
            (uid,)
        ).fetchall()
        min_row = db.execute(
            "SELECT value FROM app_settings WHERE key='min_withdrawal_pesewas'"
        ).fetchone()
        fee_row = db.execute(
            "SELECT value FROM app_settings WHERE key='withdrawal_fee_pct'"
        ).fetchone()
        min_withdrawal = int(min_row["value"]) if min_row else 10000
        withdrawal_fee_pct = float(fee_row["value"]) if fee_row else 0.0

    return render_template("reseller/wallet.html",
                           user=user, store=store,
                           withdrawals=[dict(r) for r in rows],
                           min_withdrawal=min_withdrawal,
                           withdrawal_fee_pct=withdrawal_fee_pct)


@reseller_bp.route("/wallet/resolve-account")
@reseller_required
def resolve_account_name():
    """Proxy to Paystack account resolve — keeps secret key server-side."""
    config  = current_app.config
    number  = request.args.get("number", "").strip()
    network = request.args.get("network", "").strip().lower()
    bank_code = MOMO_BANK_CODES.get(network)
    if not number or not bank_code:
        return jsonify({"ok": False, "error": "Number and network are required."}), 400
    try:
        result = resolve_account(config["PAYSTACK_SECRET_KEY"], number, bank_code)
        name = result.get("data", {}).get("account_name", "")
        if not name:
            return jsonify({"ok": False, "error": "Could not resolve account name."}), 404
        return jsonify({"ok": True, "name": name})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@reseller_bp.route("/wallet/setup-payout", methods=["POST"])
@reseller_required
def setup_payout():
    """Save a permanent Paystack mobile-money recipient for this reseller."""
    config = current_app.config
    uid = session["user_id"]
    data = request.get_json() or {}

    phone = data.get("phone", "").strip()
    account_name = data.get("account_name", "").strip()
    network = data.get("network", "").strip().lower()

    if not phone or not account_name or not network:
        return jsonify({"error": "Phone, account name, and network are required."}), 400
    bank_code = MOMO_BANK_CODES.get(network)
    if not bank_code:
        return jsonify({"error": "Invalid network. Choose mtn, telecel, or airteltigo."}), 400

    try:
        result = create_transfer_recipient(
            config["PAYSTACK_SECRET_KEY"], account_name, phone, bank_code
        )
        recipient_code = result["data"]["recipient_code"]
    except Exception as exc:
        return jsonify({"error": f"Could not create payout recipient: {exc}"}), 502

    with global_db(config) as db:
        db.execute(
            """UPDATE users
               SET payout_recipient_code=?, momo_network=?, momo_number=?
               WHERE id=?""",
            (recipient_code, network, phone, uid)
        )

    return jsonify({"ok": True, "recipient_code": recipient_code})


@reseller_bp.route("/wallet/withdraw", methods=["POST"])
@reseller_required
def withdraw():
    config = current_app.config
    uid = session["user_id"]
    data = request.get_json() or {}

    try:
        amount_pesewas = round(float(data.get("amount", 0)) * 100)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid amount."}), 400

    with global_db(config) as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        min_row = db.execute(
            "SELECT value FROM app_settings WHERE key='min_withdrawal_pesewas'"
        ).fetchone()
        fee_row = db.execute(
            "SELECT value FROM app_settings WHERE key='withdrawal_fee_pct'"
        ).fetchone()
        minimum = int(min_row["value"]) if min_row else 10000
        fee_pct = float(fee_row["value"]) if fee_row else 0.0

        if user["withdrawals_blocked"]:
            return jsonify({"error": "Withdrawals are disabled on your account. Please contact the agent who manages you or support."}), 403
        if not user["payout_recipient_code"]:
            return jsonify({"error": "Set up your payout profile first before withdrawing."}), 400
        if amount_pesewas < minimum:
            return jsonify({"error": f"Minimum withdrawal is GHS {minimum/100:.2f}."}), 400
        if amount_pesewas > user["wallet_pesewas"]:
            return jsonify({"error": "Insufficient wallet balance."}), 400

        fee_pesewas = round(amount_pesewas * fee_pct / 100)
        payout_pesewas = amount_pesewas - fee_pesewas

        if payout_pesewas <= 0:
            return jsonify({"error": "Amount too small after fee deduction."}), 400

        wd_id = str(uuid.uuid4())
        reference = f"WD-{wd_id[:8].upper()}"

        # Deduct full requested amount from reseller balance
        db.execute(
            "UPDATE users SET wallet_pesewas = wallet_pesewas - ? WHERE id=?",
            (amount_pesewas, uid)
        )
        db.execute(
            """INSERT INTO wallet_withdrawals
               (id, user_id, amount_pesewas, fee_pesewas, mobile_number, network, status, paystack_transfer_code)
               VALUES (?,?,?,?,?,?,'pending',?)""",
            (wd_id, uid, amount_pesewas, fee_pesewas,
             user["momo_number"], user["momo_network"], reference)
        )

    # Return success - transfer will be initiated when admin approves
    fee_msg = f" (fee: GHS {fee_pesewas/100:.2f})" if fee_pesewas else ""
    return jsonify({
        "ok": True,
        "message": f"Withdrawal request submitted. GHS {payout_pesewas/100:.2f} will be sent to {user['momo_number']} after admin approval.{fee_msg}"
    })


@reseller_bp.route("/account", methods=["GET"])
@reseller_required
def account_settings():
    config = current_app.config
    uid, user, store = _ctx(config)
    return render_template("reseller/account.html", user=user, store=store)


@reseller_bp.route("/account/change-username", methods=["POST"])
@reseller_required
def change_username():
    import re
    config = current_app.config
    uid  = session["user_id"]
    data = request.get_json() or {}
    username = data.get("username", "").strip().lower()

    if not username:
        return jsonify({"error": "Username is required."}), 400
    pattern = re.compile(r'^[a-z0-9][a-z0-9_-]{1,28}[a-z0-9]$|^[a-z0-9]{3}$')
    if not pattern.match(username) or re.search(r'[_-]{2,}', username):
        return jsonify({"error": "Username must be 3–30 characters, start and end with a letter or number, no consecutive - or _."}), 400

    with global_db(config) as db:
        existing = db.execute(
            "SELECT id FROM users WHERE username=? AND id!=?", (username, uid)
        ).fetchone()
        if existing:
            return jsonify({"error": "Username already taken."}), 400
        db.execute("UPDATE users SET username=? WHERE id=?", (username, uid))
    return jsonify({"ok": True, "message": "Username updated successfully.", "username": username})


@reseller_bp.route("/account/change-password", methods=["POST"])
@reseller_required
def change_password():
    config = current_app.config
    uid = session["user_id"]
    data = request.get_json() or {}
    current_pw  = data.get("current_password", "")
    new_pw      = data.get("new_password", "").strip()
    confirm_pw  = data.get("confirm_password", "").strip()

    if not current_pw or not new_pw or not confirm_pw:
        return jsonify({"error": "All fields are required."}), 400
    if new_pw != confirm_pw:
        return jsonify({"error": "New passwords do not match."}), 400
    if len(new_pw) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    with global_db(config) as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not check_password_hash(user["password_hash"], current_pw):
            return jsonify({"error": "Current password is incorrect."}), 400
        db.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (generate_password_hash(new_pw), uid)
        )
    return jsonify({"ok": True, "message": "Password changed successfully."})


@reseller_bp.route("/store", methods=["GET", "POST"])
@reseller_required
def store_settings():
    config = current_app.config
    uid, user, store = _ctx(config)

    if not store:
        return redirect(url_for("reseller.dashboard"))

    if request.method == "POST":
        data = request.get_json()
        store_name = data.get("store_name", "").strip()
        description = data.get("description", "").strip()
        support_whatsapp = data.get("support_whatsapp", "").strip()

        if not store_name:
            return jsonify({"error": "Store name is required."}), 400

        with global_db(config) as db:
            db.execute(
                "UPDATE stores SET store_name=?, description=?, support_whatsapp=? WHERE id=?",
                (store_name, description or None, support_whatsapp or None, store["id"])
            )
        return jsonify({"ok": True})

    return render_template("reseller/store.html", user=user, store=store)


@reseller_bp.route("/store/logo", methods=["POST"])
@reseller_required
def upload_logo():
    config = current_app.config
    uid, user, store = _ctx(config)

    if not store:
        return jsonify({"error": "Store not found."}), 404

    file = request.files.get("logo")
    if not file or not file.filename:
        return jsonify({"error": "No file provided."}), 400

    content_type = file.content_type or ""
    if content_type not in ALLOWED_LOGO_TYPES:
        return jsonify({"error": "Only JPEG, PNG, WebP or GIF images are allowed."}), 400

    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > MAX_LOGO_BYTES:
        return jsonify({"error": "Image must be under 2 MB."}), 400

    ext = content_type.split("/")[-1].replace("jpeg", "jpg")
    r2_key = f"logos/{store['id']}.{ext}"

    try:
        url = upload_asset(config, file, r2_key, content_type)
    except Exception as e:
        return jsonify({"error": f"Upload failed: {str(e)}"}), 500
    # Same R2 key is reused on re-upload; a version suffix makes browsers and link
    # scrapers pick up the new image instead of a cached one.
    import time
    url = f"{url}?v={int(time.time())}"

    with global_db(config) as db:
        db.execute("UPDATE stores SET logo_url=? WHERE id=?", (url, store["id"]))

    return jsonify({"ok": True, "logo_url": url})


@reseller_bp.route("/store/logo/remove", methods=["POST"])
@reseller_required
def remove_logo():
    config = current_app.config
    uid, user, store = _ctx(config)

    if not store:
        return jsonify({"error": "Store not found."}), 404

    # Best-effort delete from assets bucket for each possible extension
    for ext in ("jpg", "png", "webp", "gif"):
        delete_asset(config, f"logos/{store['id']}.{ext}")

    with global_db(config) as db:
        db.execute("UPDATE stores SET logo_url=NULL WHERE id=?", (store["id"],))

    return jsonify({"ok": True})
