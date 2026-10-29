"""Referral earnings: referrer gets a % of every dispatched order made by the
people they referred, for a fixed window after those people sign up."""
import uuid
from .push import broadcast_push

DEFAULT_RATE_BPS = 100   # 100 basis points = 1%
DEFAULT_DAYS = 60


def _setting(db, key, default):
    row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    try:
        return int(row["value"]) if row else default
    except (TypeError, ValueError):
        return default


def credit_referral(config, db, order):
    """Call inside an open global_db() block, only for a dispatched order.

    An order counts toward a referral when it was either
      (a) placed through the referred reseller's own store, or
      (b) bought by the referred reseller (customer_email = their account email).
    Safe to call twice for the same order: referral_earnings.order_id is UNIQUE.
    Returns the pesewas credited (0 if nothing).
    """
    order = dict(order)
    rate_bps = _setting(db, "referral_rate_bps", DEFAULT_RATE_BPS)
    days = _setting(db, "referral_days", DEFAULT_DAYS)
    amount = (order["amount_pesewas"] * rate_bps) // 10000
    if amount <= 0:
        return 0

    referee = None
    if order.get("store_id"):
        referee = db.execute(
            """SELECT u.id, u.referred_by, u.created_at FROM users u
               JOIN stores s ON s.user_id = u.id
               WHERE s.id=? AND u.referred_by IS NOT NULL""",
            (order["store_id"],)
        ).fetchone()
    if not referee and order.get("customer_email"):
        referee = db.execute(
            "SELECT id, referred_by, created_at FROM users WHERE email=? AND referred_by IS NOT NULL",
            (order["customer_email"].strip().lower(),)
        ).fetchone()
    if not referee:
        return 0

    # Still inside the window? (window starts when the referred person signed up)
    in_window = db.execute(
        "SELECT datetime('now') <= datetime(?, ?)", (referee["created_at"], f"+{days} days")
    ).fetchone()[0]
    if not in_window:
        return 0

    cur = db.execute(
        """INSERT OR IGNORE INTO referral_earnings
           (id, order_id, referrer_id, referred_id, amount_pesewas)
           VALUES (?,?,?,?,?)""",
        (str(uuid.uuid4()), order["id"], referee["referred_by"], referee["id"], amount)
    )
    if cur.rowcount == 0:
        return 0   # already credited for this order

    db.execute(
        "UPDATE users SET wallet_pesewas = wallet_pesewas + ? WHERE id=?",
        (amount, referee["referred_by"])
    )
    try:
        broadcast_push(config, referee["referred_by"], "Referral bonus!",
                       f"You earned GHS {amount/100:.2f} from a friend's order.",
                       "/dashboard/referrals")
    except Exception:
        pass
    return amount
