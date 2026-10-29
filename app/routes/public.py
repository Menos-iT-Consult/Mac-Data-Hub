import uuid
from flask import Blueprint, render_template, request, jsonify, current_app, redirect, url_for, send_from_directory
import os
from ..services.db import global_db, global_db_read, user_db
from ..services.paystack import initialize_transaction, verify_transaction, add_paystack_charge
from ..services.gigzhub import dispatch_bundle
from ..services.push import broadcast_push
from ..services.referrals import credit_referral
from ..services.pricing_chain import resolve_store_price, sub_agent_floors
from ..services.team import credit_agent_cut

public_bp = Blueprint("public", __name__)


def _store_unavailable(db, store) -> bool:
    """True when the store's owner is suspended or removed."""
    if not store:
        return False
    owner = db.execute(
        "SELECT is_suspended, removed_at FROM users WHERE id=?", (store["user_id"],)
    ).fetchone()
    return bool(owner and (owner["is_suspended"] or owner["removed_at"]))


@public_bp.route("/.well-known/assetlinks.json")
def assetlinks():
    """Digital Asset Links — required for TWA to run without browser chrome."""
    # Replace SHA256_FINGERPRINT with the value from: bubblewrap fingerprint list
    data = [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "com.macdatahubgh.app",
                "sha256_cert_fingerprints": [
                    "D3:33:1D:4D:2C:4A:F1:89:92:13:5D:0F:28:0E:F9:18:27:E8:62:43:88:50:5E:5B:42:05:70:5F:34:36:E8:00"
                ]
            }
        }
    ]
    from flask import json
    resp = current_app.response_class(
        json.dumps(data, indent=2),
        mimetype="application/json"
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@public_bp.route("/download/apk")
def download_apk():
    """Serve the Mac Data Hub Android APK."""
    static_dir = os.path.join(current_app.root_path, "..", "static")
    return send_from_directory(
        os.path.abspath(static_dir), "macdatahub.apk",
        as_attachment=True,
        download_name="MacDataHub.apk"
    )


@public_bp.route("/sw.js")
def service_worker():
    """Serve SW from root so it has scope over the whole origin."""
    static_dir = os.path.join(current_app.root_path, "..", "static")
    resp = send_from_directory(os.path.abspath(static_dir), "sw.js")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@public_bp.route("/offline")
def offline():
    return render_template("public/offline.html"), 200


@public_bp.route("/")
def home():
    config = current_app.config
    with global_db_read(config) as db:
        bundles = db.execute(
            "SELECT * FROM data_bundles WHERE is_active=1 ORDER BY network, volume_mb"
        ).fetchall()
        settings = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM app_settings").fetchall()}
    support_whatsapp = settings.get("support_whatsapp", "")
    return render_template("public/home.html", bundles=bundles, settings=settings,
                           support_whatsapp=support_whatsapp)


@public_bp.route("/s/<slug>")
def storefront(slug):
    config = current_app.config
    with global_db_read(config) as db:
        store = db.execute("SELECT * FROM stores WHERE slug=?", (slug,)).fetchone()
        if not store or _store_unavailable(db, store):
            return render_template("public/404.html"), 404
        rows = db.execute(
            """SELECT sp.price_pesewas AS sp_price, db.base_price_pesewas AS base_price,
                      db.id as bundle_id, db.label, db.network,
                      db.volume_mb, db.validity_days, db.offer_slug
               FROM data_bundles db
               LEFT JOIN store_pricing sp ON sp.bundle_id = db.id AND sp.store_id=?
               WHERE db.is_active=1
               ORDER BY db.network, db.volume_mb""",
            (store["id"],)
        ).fetchall()
        owner = db.execute("SELECT parent_id FROM users WHERE id=?", (store["user_id"],)).fetchone()
        parent_id = owner["parent_id"] if owner else None
        floors = sub_agent_floors(db, parent_id) if parent_id else {}
        pricing = []
        for r in rows:
            d = dict(r)
            base = d["base_price"]
            if parent_id:
                floor = max(floors.get(d["bundle_id"], base), base)
                d["price_pesewas"] = max(d["sp_price"], floor) if d["sp_price"] is not None else floor
            else:
                d["price_pesewas"] = d["sp_price"] if d["sp_price"] is not None else base
            pricing.append(d)
    return render_template("public/storefront.html", store=store, pricing=pricing,
                           support_whatsapp=store["support_whatsapp"] if store["support_whatsapp"] else "")


@public_bp.route("/checkout", methods=["GET", "POST"])
def checkout():
    config = current_app.config
    if request.method == "GET":
        bundle_id = request.args.get("bundle_id")
        store_id = request.args.get("store_id")
        with global_db_read(config) as db:
            bundle = db.execute("SELECT * FROM data_bundles WHERE id=?", (bundle_id,)).fetchone()
            store = db.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone() if store_id else None
            split = resolve_store_price(db, store, bundle) if (store and bundle) else None
            unavailable = _store_unavailable(db, store)
        if not bundle or unavailable:
            return redirect(url_for("public.home"))
        if split:
            price = split["price"]
        else:
            price = bundle["guest_price_pesewas"] or bundle["base_price_pesewas"]
        charge, fee = add_paystack_charge(price)
        return render_template("public/checkout.html", bundle=bundle, store=store,
                               price=price, fee_pesewas=fee, charge_pesewas=charge)

    # POST: initiate Paystack payment
    data = request.get_json()
    bundle_id = data.get("bundle_id")
    store_id = data.get("store_id")
    phone = data.get("phone", "").strip()
    email = data.get("email", "").strip()

    with global_db(config) as db:
        bundle = db.execute("SELECT * FROM data_bundles WHERE id=?", (bundle_id,)).fetchone()
        if not bundle:
            return jsonify({"error": "Bundle not found."}), 404
        store = db.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone() if store_id else None
        if _store_unavailable(db, store):
            return jsonify({"error": "This store is currently unavailable."}), 403
        if store:
            split = resolve_store_price(db, store, bundle)
            price, profit = split["price"], split["profit"]
            agent_id, agent_profit = split["agent_id"], split["agent_profit"]
        else:
            price = bundle["guest_price_pesewas"] or bundle["base_price_pesewas"]
            profit, agent_id, agent_profit = 0, None, 0
        order_id = str(uuid.uuid4())
        reference = f"MDH-{order_id[:8].upper()}"
        # Customer pays price + Paystack fee so seller receives full price
        charge_pesewas, fee_pesewas = add_paystack_charge(price)

        db.execute(
            """INSERT INTO orders
               (id, store_id, bundle_id, customer_phone, customer_email, network,
                volume_mb, amount_pesewas, base_cost_pesewas, profit_pesewas,
                agent_id, agent_profit_pesewas, paystack_reference, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending')""",
            (order_id, store_id, bundle_id, phone, email,
             bundle["network"], bundle["volume_mb"],
             price, bundle["base_price_pesewas"], profit,
             agent_id, agent_profit, reference)
        )

    callback_url = f"{config['APP_URL']}/checkout/verify?ref={reference}"
    result = initialize_transaction(
        config["PAYSTACK_SECRET_KEY"], email, charge_pesewas, reference, callback_url,
        metadata={"order_id": order_id, "phone": phone}
    )
    return jsonify({"authorization_url": result["data"]["authorization_url"],
                    "reference": reference,
                    "fee_pesewas": fee_pesewas})


@public_bp.route("/track")
def track_orders():
    phone = request.args.get("phone", "").strip()
    store_id = request.args.get("store_id", "").strip() or None
    if not phone:
        return jsonify({"error": "Phone number is required."}), 400
    config = current_app.config
    with global_db_read(config) as db:
        if store_id:
            rows = db.execute(
                """SELECT o.id, o.network, o.volume_mb, o.amount_pesewas,
                          o.status, o.created_at, b.label
                   FROM orders o
                   LEFT JOIN data_bundles b ON b.id = o.bundle_id
                   WHERE o.customer_phone=? AND o.store_id=?
                   ORDER BY o.created_at DESC LIMIT 20""",
                (phone, store_id)
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT o.id, o.network, o.volume_mb, o.amount_pesewas,
                          o.status, o.created_at, b.label
                   FROM orders o
                   LEFT JOIN data_bundles b ON b.id = o.bundle_id
                   WHERE o.customer_phone=?
                   ORDER BY o.created_at DESC LIMIT 20""",
                (phone,)
            ).fetchall()
    orders = [dict(r) for r in rows]
    return jsonify({"ok": True, "orders": orders, "phone": phone})


@public_bp.route("/checkout/verify")
def checkout_verify():
    ref = request.args.get("ref", "")
    return render_template("public/checkout_verify.html", reference=ref)


@public_bp.route("/verify-payment", methods=["POST"])
def verify_payment():
    config = current_app.config
    data = request.get_json() or {}
    reference = data.get("reference", "").strip().upper()
    phone = data.get("phone", "").strip()

    if not reference:
        return jsonify({"ok": False, "error": "Paystack reference is required."}), 400

    with global_db_read(config) as db:
        order = db.execute(
            """SELECT o.*, b.offer_slug, b.label as bundle_label
               FROM orders o
               LEFT JOIN data_bundles b ON b.id = o.bundle_id
               WHERE o.paystack_reference=?""",
            (reference,)
        ).fetchone()

        if not order:
            return jsonify({"ok": False, "error": "Reference not found. Check the reference in your payment email and try again."}), 404

        # Already delivered — just confirm
        if order["status"] == "dispatched":
            return jsonify({
                "ok": True,
                "already_done": True,
                "message": f"Your {order['bundle_label'] or order['network'].upper()} data was successfully delivered to {order['customer_phone']}.",
                "network": order["network"],
                "label": order["bundle_label"],
                "phone": order["customer_phone"],
            })

        # Webhook already picked this up and is processing — don't double-dispatch
        if order["status"] == "paid":
            return jsonify({
                "ok": True,
                "already_done": True,
                "message": f"Your payment is confirmed and your {order['bundle_label'] or order['network'].upper()} data is being processed. It will arrive shortly.",
                "network": order["network"],
                "label": order["bundle_label"],
                "phone": order["customer_phone"],
            })

        # Save values we need after closing the DB context
        offer_slug = order["offer_slug"] or ""
        order_dict = dict(order)

    # Check Paystack to confirm the payment is real
    try:
        ps_result = verify_transaction(config["PAYSTACK_SECRET_KEY"], reference)
        ps_data = ps_result.get("data", {})
        ps_status = ps_data.get("status", "")
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not reach Paystack to verify payment: {exc}"}), 502

    if ps_status != "success":
        return jsonify({
            "ok": False,
            "error": f"Your payment has not been confirmed by Paystack (status: {ps_status}). If you were charged, please contact support with reference {reference}.",
        })

    # Payment is confirmed — dispatch now
    gigzhub_id = ""
    gigzhub_error = ""
    status = "failed"
    try:
        result = dispatch_bundle(
            config["GIGZHUB_API_KEY"],
            order_dict["network"],
            order_dict["customer_phone"],
            offer_slug,
            order_dict["volume_mb"],
        )
        data_obj = result.get("data") or result
        gigzhub_id = (
            str(data_obj.get("id", ""))
            or str(data_obj.get("orderId", ""))
            or str(data_obj.get("order_id", ""))
            or str(data_obj.get("reference", ""))
        )
        status = "dispatched"
    except Exception as exc:
        gigzhub_error = str(exc)[:500]

    with global_db(config) as db:
        db.execute(
            "UPDATE orders SET status=?, gigzhub_order_id=?, gigzhub_error=? WHERE id=?",
            (status, gigzhub_id, gigzhub_error or None, order_dict["id"])
        )

        if status == "dispatched":
            try:
                credit_referral(config, db, order_dict)
            except Exception:
                pass

        if status == "dispatched" and order_dict["store_id"]:
            store = db.execute(
                "SELECT user_id FROM stores WHERE id=?", (order_dict["store_id"],)
            ).fetchone()
            if store:
                if order_dict["profit_pesewas"] > 0:
                    db.execute(
                        "UPDATE users SET wallet_pesewas = wallet_pesewas + ? WHERE id=?",
                        (order_dict["profit_pesewas"], store["user_id"])
                    )
                label = order_dict["bundle_label"] or order_dict["network"]
                _mirror_order(config, store["user_id"], order_dict, label)
                credit_agent_cut(config, db, order_dict, label)
                try:
                    broadcast_push(config, store["user_id"],
                                   "Order dispatched!",
                                   f"{label} sent to {order_dict['customer_phone']}",
                                   "/dashboard/orders")
                except Exception:
                    pass

    if status == "dispatched":
        return jsonify({
            "ok": True,
            "already_done": False,
            "message": f"Your payment is confirmed and {order_dict['bundle_label'] or order_dict['network'].upper()} data is being sent to {order_dict['customer_phone']}.",
            "network": order_dict["network"],
            "label": order_dict["bundle_label"],
            "phone": order_dict["customer_phone"],
        })

    return jsonify({
        "ok": False,
        "error": f"Your payment is confirmed but data delivery failed: {gigzhub_error}. Please contact support with reference {reference}.",
    })


def _mirror_order(config, user_id: str, order: dict, bundle_label: str):
    with user_db(config, user_id) as udb:
        udb.execute(
            """INSERT OR IGNORE INTO orders
               (id, bundle_label, network, customer_phone, amount_pesewas, profit_pesewas, status)
               VALUES (?,?,?,?,?,?,?)""",
            (order["id"], bundle_label, order["network"],
             order["customer_phone"], order["amount_pesewas"],
             order["profit_pesewas"], "dispatched")
        )
        udb.execute(
            "INSERT OR IGNORE INTO earnings (id, order_id, amount_pesewas) VALUES (?,?,?)",
            (str(uuid.uuid4()), order["id"], order["profit_pesewas"])
        )

