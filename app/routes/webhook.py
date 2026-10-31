import uuid
from flask import Blueprint, request, jsonify, current_app
from ..services.db import global_db, user_db
from ..services.paystack import verify_webhook_signature
import hmac
from ..services.gigzhub import dispatch_bundle, webhook_token
from ..services.order_sync import apply_webhook
from ..services.push import broadcast_push
from ..services.referrals import credit_referral
from ..services.team import credit_agent_cut

webhook_bp = Blueprint("webhook", __name__)


@webhook_bp.route("/webhook/paystack", methods=["POST"])
def paystack_webhook():
    config = current_app.config
    signature = request.headers.get("x-paystack-signature", "")
    payload_bytes = request.get_data()

    if not verify_webhook_signature(config["PAYSTACK_SECRET_KEY"], payload_bytes, signature):
        return jsonify({"error": "Invalid signature"}), 401

    event = request.get_json()
    event_type = event.get("event", "")
    data = event["data"]

    # --- Transfer outcome (auto withdrawal) ---
    if event_type in ("transfer.success", "transfer.failed", "transfer.reversed"):
        _handle_transfer(config, event_type, data)
        return jsonify({"ok": True}), 200

    if event_type != "charge.success":
        return jsonify({"ok": True}), 200

    reference = data.get("reference", "")
    metadata = data.get("metadata", {})

    # --- Registration payment ---
    if reference.startswith("REG-"):
        _handle_registration(config, reference)
        return jsonify({"ok": True}), 200

    # --- Bundle order payment ---
    _handle_order(config, reference, metadata)
    return jsonify({"ok": True}), 200


@webhook_bp.route("/webhook/gigzhub", methods=["POST"])
def gigzhub_webhook():
    """GigzHub calls this on every order status change (pending/processing/delivered/failed/...).
    Authenticated by the secret token embedded in the webhookUrl we send with each order."""
    if not hmac.compare_digest(request.args.get("token", ""), webhook_token()):
        return jsonify({"error": "Invalid token"}), 401
    data = request.get_json(silent=True) or {}
    # Always answer 200 quickly once authenticated, even for orders we don't know,
    # so GigzHub doesn't keep retrying.
    try:
        apply_webhook(current_app.config,
                      [data.get("orderId"), data.get("reference")],
                      data.get("status"))
    except Exception:
        current_app.logger.exception("GigzHub webhook processing failed")
    return jsonify({"ok": True}), 200


def _handle_registration(config, reference: str):
    with global_db(config) as db:
        reg = db.execute(
            "SELECT * FROM reseller_registrations WHERE paystack_reference=? AND status='pending'",
            (reference,)
        ).fetchone()
        if not reg:
            return

        db.execute(
            "UPDATE reseller_registrations SET status='paid' WHERE id=?", (reg["id"],)
        )
        db.execute(
            "UPDATE users SET is_active=1 WHERE id=?", (reg["user_id"],)
        )


def _handle_order(config, reference: str, metadata: dict):
    # Signature already verified by the caller, so no extra Paystack lookup is needed.
    # fulfil_order claims the order atomically, so webhook replays can't double-dispatch.
    from ..services.fulfil import fulfil_order
    fulfil_order(config, reference, verify=False)


def _mirror_order_to_user_db(config, user_id: str, order, bundle_label: str = ""):
    with user_db(config, user_id) as udb:
        udb.execute(
            """INSERT OR IGNORE INTO orders
               (id, bundle_label, network, customer_phone, amount_pesewas, profit_pesewas, status)
               VALUES (?,?,?,?,?,?,?)""",
            (order["id"], bundle_label, order["network"],
             order["customer_phone"], order["amount_pesewas"],
             order["profit_pesewas"], "dispatched")
        )
        earning_id = str(uuid.uuid4())
        udb.execute(
            "INSERT OR IGNORE INTO earnings (id, order_id, amount_pesewas) VALUES (?,?,?)",
            (earning_id, order["id"], order["profit_pesewas"])
        )


def _handle_transfer(config, event_type: str, data: dict):
    transfer_code = data.get("transfer_code", "")
    if not transfer_code:
        return
    with global_db(config) as db:
        wd = db.execute(
            "SELECT * FROM wallet_withdrawals WHERE paystack_transfer_code=?",
            (transfer_code,)
        ).fetchone()
        if not wd:
            return

        if event_type == "transfer.success":
            db.execute(
                "UPDATE wallet_withdrawals SET status='paid' WHERE id=?", (wd["id"],)
            )
            try:
                broadcast_push(config, wd["user_id"],
                               "Withdrawal successful",
                               f"GHS {wd['amount_pesewas']/100:.2f} has been sent to {wd['mobile_number']}.",
                               "/dashboard/wallet")
            except Exception:
                pass

        elif event_type in ("transfer.failed", "transfer.reversed"):
            # Only act if still processing — if already "failed" (admin rejected) or "paid",
            # skip to avoid double-refunding the balance
            if wd["status"] == "processing":
                db.execute(
                    "UPDATE wallet_withdrawals SET status='failed' WHERE id=?", (wd["id"],)
                )
                # Refund the balance since transfer never completed
                db.execute(
                    "UPDATE users SET wallet_pesewas = wallet_pesewas + ? WHERE id=?",
                    (wd["amount_pesewas"], wd["user_id"])
                )
                try:
                    broadcast_push(config, wd["user_id"],
                                   "Withdrawal failed",
                                   f"Your GHS {wd['amount_pesewas']/100:.2f} withdrawal could not be completed. Your balance has been restored.",
                                   "/dashboard/wallet")
                except Exception:
                    pass


def _get_offer_slug(db, bundle_id: str) -> str:
    row = db.execute("SELECT offer_slug FROM data_bundles WHERE id=?", (bundle_id,)).fetchone()
    return row["offer_slug"] if row else ""


def _notify_admins(config, status: str, order, amt_ghs: str, error: str = ""):
    if status == "dispatched":
        title = "New order dispatched"
        body  = f"{order['network'].upper()} bundle to {order['customer_phone']} — {amt_ghs}"
    else:
        title = "Order failed — action needed"
        body  = f"Dispatch failed for {order['customer_phone']}: {error[:80]}" if error else \
                f"Bundle dispatch failed for {order['customer_phone']}"
    try:
        broadcast_push(config, "admin", title, body, "/admin/orders")
    except Exception:
        pass
