"""Keeps order statuses in step with GigzHub automatically.

Flow: webhook/checkout dispatches an order (status 'dispatched'). From then on this
module asks GigzHub how the order is doing and moves it forward:

    dispatched -> processing -> completed        (or -> failed at any point)

Nobody has to mark anything by hand. Statuses only ever move forward, except to
'failed'. A failed lookup (network down, wrong path) never changes an order.

Money rule: profit / agent cut / referral bonus are credited when an order is
dispatched. If GigzHub later reports it as failed, those credits are reversed so
the invariant "credits exist only for dispatched/processing/completed orders" holds.
That also keeps the admin Retry button correct, because it re-credits on success.
"""
import threading
import time
import uuid
import logging

from .db import global_db, global_db_read, user_db
from .gigzhub import get_order_status
from .push import broadcast_push

log = logging.getLogger(__name__)

ACTIVE = ("dispatched", "processing")
_RANK = {"dispatched": 1, "processing": 2, "completed": 3}
_sync_lock = threading.Lock()


# ── Reading: live status for dashboards ──────────────────────────────────────

def overlay_status(config, rows: list) -> list:
    """Replace the (possibly stale) status on rows from a reseller's own DB with the
    live status from the global orders table. Rows are dicts with an 'id'."""
    ids = [r["id"] for r in rows]
    if not ids:
        return rows
    live = {}
    with global_db_read(config) as db:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in db.execute(f"SELECT id, status FROM orders WHERE id IN ({q})", chunk):
                live[r["id"]] = r["status"]
    for r in rows:
        if r["id"] in live:
            r["status"] = live[r["id"]]
    return rows


# ── Writing: one sync pass ───────────────────────────────────────────────────

def sync_once(config, limit: int = 40) -> dict:
    """Check up to `limit` in-flight orders against GigzHub. Safe to call concurrently."""
    result = {"checked": 0, "changed": 0, "failed": 0, "unmapped": 0, "errors": []}
    if not _sync_lock.acquire(blocking=False):
        result["errors"].append("A sync is already running.")
        return result
    try:
        with global_db_read(config) as db:
            rows = db.execute(
                """SELECT id, status, gigzhub_order_id FROM orders
                   WHERE status IN ('dispatched','processing')
                     AND COALESCE(gigzhub_order_id,'') != ''
                     AND created_at > datetime('now','-30 days')
                   ORDER BY COALESCE(status_checked_at,'') ASC
                   LIMIT ?""", (limit,)
            ).fetchall()

        lookups = []   # (order_id, new_status | None, raw_status)
        for r in rows:
            try:
                info = get_order_status(config["GIGZHUB_API_KEY"], r["gigzhub_order_id"])
            except Exception as exc:
                if len(result["errors"]) < 3:
                    result["errors"].append(str(exc)[:240])
                lookups.append((r["id"], None, None))   # still stamp "checked" so we rotate
                continue
            result["checked"] += 1
            if info["status"] is None:
                result["unmapped"] += 1
            lookups.append((r["id"], info["status"], info["raw"]))

        if lookups:
            _apply(config, lookups, result)
    finally:
        _sync_lock.release()
    return result


def apply_webhook(config, refs: list, raw_status: str) -> dict:
    """Handle a GigzHub status callback. `refs` = the orderId/reference values in the payload.
    Safe against duplicate deliveries (status only moves forward / to failed once)."""
    from .gigzhub import normalize_status
    refs = [str(r) for r in refs if r]
    result = {"checked": 1, "changed": 0, "failed": 0, "unmapped": 0, "errors": []}
    if not refs or not raw_status:
        return result
    new = normalize_status(raw_status)
    with global_db_read(config) as db:
        q = ",".join("?" * len(refs))
        row = db.execute(f"SELECT id FROM orders WHERE gigzhub_order_id IN ({q}) LIMIT 1", refs).fetchone()
    if not row:
        return result
    if new is None:
        result["unmapped"] = 1
    _apply(config, [(row["id"], new, str(raw_status)[:40])], result)
    return result


def _apply(config, lookups, result):
    failed_orders = []      # (order row dict, users whose mirrored DB needs cleanup)
    with global_db(config) as db:        # one write + one R2 upload for the whole batch
        for oid, new, raw in lookups:
            cur = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if not cur or cur["status"] not in ACTIVE:
                continue                  # changed meanwhile (admin retry, etc.)
            if raw is None:               # lookup error: just stamp the time
                db.execute("UPDATE orders SET status_checked_at=datetime('now') WHERE id=?", (oid,))
                continue
            move = new == "failed" or (new in _RANK and _RANK[new] > _RANK[cur["status"]])
            if not move:
                db.execute(
                    "UPDATE orders SET gigzhub_status=?, status_checked_at=datetime('now') WHERE id=?",
                    (raw, oid))
                continue
            err = f"GigzHub reported this order as '{raw}'." if new == "failed" else None
            c = db.execute(
                """UPDATE orders SET status=?, gigzhub_status=?, status_checked_at=datetime('now'),
                          gigzhub_error=COALESCE(?, gigzhub_error)
                   WHERE id=? AND status=?""",
                (new, raw, err, oid, cur["status"]))
            if c.rowcount != 1:
                continue
            result["changed"] += 1
            if new == "failed":
                result["failed"] += 1
                failed_orders.append((dict(cur), _reverse_credits(db, cur)))

    # Per-reseller mirrored DBs: drop the earnings rows we just reversed (best effort)
    for order, user_ids in failed_orders:
        for uid in user_ids:
            try:
                with user_db(config, uid) as udb:
                    udb.execute("DELETE FROM earnings WHERE order_id=?", (order["id"],))
                    udb.execute("UPDATE orders SET status='failed' WHERE id=?", (order["id"],))
            except Exception:
                log.exception("Could not clean mirrored order %s for %s", order["id"], uid)
        try:
            broadcast_push(config, "admin", "Order failed at GigzHub",
                           f"{order['network'].upper()} to {order['customer_phone']} — retry from Orders",
                           "/admin/orders")
        except Exception:
            pass


def _reverse_credits(db, order) -> list:
    """Undo everything credited when this order was dispatched. Runs inside the open
    global_db() block. Returns the user ids whose personal DB mirrors the order."""
    mirrored = []
    if order["store_id"]:
        store = db.execute("SELECT user_id FROM stores WHERE id=?", (order["store_id"],)).fetchone()
        if store:
            mirrored.append(store["user_id"])
            if (order["profit_pesewas"] or 0) > 0:
                db.execute("UPDATE users SET wallet_pesewas = wallet_pesewas - ? WHERE id=?",
                           (order["profit_pesewas"], store["user_id"]))
    agent_id = order["agent_id"] if "agent_id" in order.keys() else None
    cut = (order["agent_profit_pesewas"] or 0) if "agent_profit_pesewas" in order.keys() else 0
    if agent_id and cut > 0:
        mirrored.append(agent_id)
        db.execute("UPDATE users SET wallet_pesewas = wallet_pesewas - ? WHERE id=?", (cut, agent_id))
    ref = db.execute("SELECT referrer_id, amount_pesewas FROM referral_earnings WHERE order_id=?",
                     (order["id"],)).fetchone()
    if ref:
        db.execute("UPDATE users SET wallet_pesewas = wallet_pesewas - ? WHERE id=?",
                   (ref["amount_pesewas"], ref["referrer_id"]))
        db.execute("DELETE FROM referral_earnings WHERE order_id=?", (order["id"],))
    return mirrored


def ensure_mirror(config, user_id: str, order, label: str):
    """Make sure a reseller's personal DB shows the order and exactly one earnings row.
    Used when a failed/stuck order is retried successfully."""
    with user_db(config, user_id) as udb:
        udb.execute(
            """INSERT OR IGNORE INTO orders
               (id, bundle_label, network, customer_phone, amount_pesewas, profit_pesewas, status)
               VALUES (?,?,?,?,?,?,'dispatched')""",
            (order["id"], label, order["network"], order["customer_phone"],
             order["amount_pesewas"], order["profit_pesewas"]))
        udb.execute("UPDATE orders SET status='dispatched' WHERE id=?", (order["id"],))
        if not udb.execute("SELECT 1 FROM earnings WHERE order_id=?", (order["id"],)).fetchone():
            udb.execute("INSERT INTO earnings (id, order_id, amount_pesewas) VALUES (?,?,?)",
                        (str(uuid.uuid4()), order["id"], order["profit_pesewas"]))


# ── Background loop ──────────────────────────────────────────────────────────

_started = False


def start_background_sync(app):
    """Start one daemon thread per process that runs sync_once() on an interval.
    Several gunicorn workers each run one; that is safe because every status change
    is a compare-and-set (UPDATE ... WHERE status=?) so credits are reversed once."""
    global _started
    interval = int(app.config.get("ORDER_SYNC_INTERVAL", 60) or 0)
    if _started or interval <= 0:
        return
    _started = True

    def loop():
        time.sleep(15)   # let the app finish booting
        while True:
            try:
                with app.app_context():
                    sync_once(app.config)
            except Exception:
                log.exception("Order status sync failed")
            time.sleep(interval)

    threading.Thread(target=loop, name="order-status-sync", daemon=True).start()
