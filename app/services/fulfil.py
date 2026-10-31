"""Confirm a Paystack payment and dispatch the bundle — ONE code path for everything.

Orders now go through automatically, no matter which of these notices the payment first:

    1. Paystack webhook            (instant, verify=False — signature already checked)
    2. Customer's return page      (/checkout/verify, runs in the background)
    3. Background reconciler       (asks Paystack about recent 'Awaiting payment' orders)
    4. Manual: customer/admin pastes the reference into "Verify & Dispatch" (kept as a fallback)

Whichever gets there first "claims" the order with an atomic
UPDATE ... WHERE status IN (...), so a bundle can never be dispatched twice.
"""
import logging
import threading
import uuid

from .db import global_db, global_db_read, user_db
from .gigzhub import dispatch_bundle
from .paystack import verify_transaction
from .push import broadcast_push
from .referrals import credit_referral
from .team import credit_agent_cut

log = logging.getLogger(__name__)
_reconcile_lock = threading.Lock()


def fulfil_order(config, reference: str, verify: bool = True, retry_failed: bool = False) -> dict:
    """Verify payment (optional) and dispatch the order with this Paystack reference.

    Returns {"state": ..., "order": {...}} where state is one of:
      dispatched | failed   we handled it now (failed = paid, but GigzHub dispatch errored)
      already               someone else handled it / it is past 'pending'
      unpaid                Paystack says the payment is not successful (yet)
      error                 could not reach Paystack — nothing changed
      not_found             no such reference
    retry_failed=True (manual paste only) also lets a previously 'failed' order be re-dispatched.
    """
    reference = (reference or "").strip().upper()
    if not reference:
        return {"state": "not_found"}

    with global_db_read(config) as db:
        row = db.execute(
            """SELECT o.*, b.offer_slug, b.label AS bundle_label
               FROM orders o LEFT JOIN data_bundles b ON b.id = o.bundle_id
               WHERE o.paystack_reference=?""", (reference,)
        ).fetchone()
    if not row:
        return {"state": "not_found"}
    order = dict(row)

    eligible = ("pending", "failed") if retry_failed else ("pending",)
    if order["status"] not in eligible:
        return {"state": "already", "order": order}

    if verify:
        try:
            ps = verify_transaction(config["PAYSTACK_SECRET_KEY"], reference)
            ps_status = (ps.get("data") or {}).get("status", "")
        except Exception as exc:
            return {"state": "error", "error": str(exc)[:200], "order": order}
        if ps_status != "success":
            return {"state": "unpaid", "paystack_status": ps_status, "order": order}

    # Atomic claim — only one caller can flip the order to 'paid'
    placeholders = ",".join("?" * len(eligible))
    with global_db(config) as db:
        claimed = db.execute(
            f"UPDATE orders SET status='paid' WHERE id=? AND status IN ({placeholders})",
            (order["id"], *eligible),
        ).rowcount == 1
    if not claimed:
        return {"state": "already", "order": order}

    # Dispatch outside any DB block so a slow API call doesn't hold the write lock
    gigzhub_id, gigzhub_error, status = "", "", "failed"
    try:
        result = dispatch_bundle(
            config["GIGZHUB_API_KEY"], order["network"], order["customer_phone"],
            order.get("offer_slug") or "", order["volume_mb"],
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

    store_owner = None
    label = order.get("bundle_label") or order["network"]
    with global_db(config) as db:
        db.execute(
            "UPDATE orders SET status=?, gigzhub_order_id=?, gigzhub_error=? WHERE id=?",
            (status, gigzhub_id, gigzhub_error or None, order["id"]),
        )
        if status == "dispatched":
            try:
                credit_referral(config, db, order)
            except Exception:
                log.exception("credit_referral failed for %s", order["id"])
            if order.get("store_id"):
                store = db.execute("SELECT user_id FROM stores WHERE id=?", (order["store_id"],)).fetchone()
                if store:
                    store_owner = store["user_id"]
                    if (order["profit_pesewas"] or 0) > 0:
                        db.execute("UPDATE users SET wallet_pesewas = wallet_pesewas + ? WHERE id=?",
                                   (order["profit_pesewas"], store_owner))
                    try:
                        credit_agent_cut(config, db, order, label)
                    except Exception:
                        log.exception("credit_agent_cut failed for %s", order["id"])

    # Per-reseller mirror + notifications (after the global write is released)
    if status == "dispatched" and store_owner:
        try:
            _mirror_order(config, store_owner, order, label)
        except Exception:
            log.exception("mirror failed for %s", order["id"])
        try:
            profit = "GHS %.2f" % ((order["profit_pesewas"] or 0) / 100)
            broadcast_push(config, store_owner, "Order dispatched!",
                           f"{label} sent to {order['customer_phone']} — +{profit} profit",
                           "/dashboard/orders")
        except Exception:
            pass
    try:
        amt = "GHS %.2f" % (order["amount_pesewas"] / 100)
        if status == "dispatched":
            broadcast_push(config, "admin", "New order dispatched",
                           f"{order['network'].upper()} bundle to {order['customer_phone']} — {amt}",
                           "/admin/orders")
        else:
            broadcast_push(config, "admin", "Order failed — action needed",
                           f"Dispatch failed for {order['customer_phone']}: {gigzhub_error[:80]}"
                           if gigzhub_error else f"Bundle dispatch failed for {order['customer_phone']}",
                           "/admin/orders")
    except Exception:
        pass

    order["status"] = status
    return {"state": status, "order": order, "error": gigzhub_error}


def _mirror_order(config, user_id: str, order: dict, bundle_label: str):
    with user_db(config, user_id) as udb:
        udb.execute(
            """INSERT OR IGNORE INTO orders
               (id, bundle_label, network, customer_phone, amount_pesewas, profit_pesewas, status)
               VALUES (?,?,?,?,?,?,?)""",
            (order["id"], bundle_label, order["network"], order["customer_phone"],
             order["amount_pesewas"], order["profit_pesewas"], "dispatched"),
        )
        udb.execute(
            "INSERT OR IGNORE INTO earnings (id, order_id, amount_pesewas) VALUES (?,?,?)",
            (str(uuid.uuid4()), order["id"], order["profit_pesewas"]),
        )


def fulfil_in_background(app, reference: str):
    """Fire-and-forget fulfil (used by the customer's return page)."""
    def run():
        with app.app_context():
            try:
                fulfil_order(app.config, reference, verify=True)
            except Exception:
                log.exception("Background fulfil failed for %s", reference)
    threading.Thread(target=run, name="fulfil-order", daemon=True).start()


def reconcile_pending(config, limit: int = 15) -> dict:
    """Ask Paystack about recent 'Awaiting payment' orders and dispatch any that were paid.

    Safety net for a missed/late webhook. Newer orders are re-checked often, older ones
    rarely, so abandoned carts don't hammer Paystack.
    """
    result = {"checked": 0, "fulfilled": 0}
    if not _reconcile_lock.acquire(blocking=False):
        return result
    try:
        with global_db_read(config) as db:
            rows = db.execute(
                """SELECT id, paystack_reference FROM orders
                   WHERE status='pending'
                     AND COALESCE(paystack_reference,'') != ''
                     AND created_at > datetime('now','-48 hours')
                     AND created_at < datetime('now','-20 seconds')
                     AND (status_checked_at IS NULL OR status_checked_at < CASE
                            WHEN created_at > datetime('now','-15 minutes') THEN datetime('now','-45 seconds')
                            WHEN created_at > datetime('now','-3 hours')    THEN datetime('now','-10 minutes')
                            ELSE datetime('now','-60 minutes') END)
                   ORDER BY created_at DESC LIMIT ?""", (limit,)
            ).fetchall()
        stamp = []
        for r in rows:
            try:
                out = fulfil_order(config, r["paystack_reference"], verify=True)
            except Exception:
                log.exception("reconcile failed for %s", r["paystack_reference"])
                stamp.append(r["id"])
                continue
            result["checked"] += 1
            if out["state"] in ("dispatched", "failed"):
                result["fulfilled"] += 1
            elif out["state"] in ("unpaid", "error"):
                stamp.append(r["id"])
        if stamp:
            with global_db(config) as db:
                for oid in stamp:
                    db.execute("UPDATE orders SET status_checked_at=datetime('now') "
                               "WHERE id=? AND status='pending'", (oid,))
    finally:
        _reconcile_lock.release()
    return result
