"""Shared account actions for sub-agents/agents (used by the agent and admin dashboards)
plus the agent's cut on sub-agent sales."""
import secrets
import uuid
from werkzeug.security import generate_password_hash
from .db import user_db
from .push import broadcast_push

_PW_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def new_password(n: int = 10) -> str:
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(n))


def toggle_flag(db, user_id: str, column: str) -> int:
    """Flip is_suspended / withdrawals_blocked and return the new value."""
    assert column in ("is_suspended", "withdrawals_blocked")
    cur = db.execute(f"SELECT {column} FROM users WHERE id=?", (user_id,)).fetchone()
    new_state = 0 if cur[column] else 1
    db.execute(f"UPDATE users SET {column}=? WHERE id=?", (new_state, user_id))
    return new_state


def reset_password(db, user_id: str) -> str:
    pw = new_password()
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), user_id))
    return pw


def remove_sub_agent(db, child):
    """Soft-remove a sub-agent. Returns an error string, or None on success.

    The login email, username and store URL are released (renamed) so the same person
    can be added again later. Order history stays intact.
    """
    if child["wallet_pesewas"] > 0:
        return (f"{child['full_name']} still has GHS {child['wallet_pesewas']/100:.2f} in their wallet. "
                "Block withdrawals or suspend the account instead, or remove once the wallet is empty.")
    busy = db.execute(
        "SELECT 1 FROM wallet_withdrawals WHERE user_id=? AND status IN ('pending','processing')",
        (child["id"],)
    ).fetchone()
    if busy:
        return "This sub-agent has a withdrawal in progress. Try again once it is settled."

    tag = child["id"][:8]
    db.execute(
        "UPDATE users SET removed_at=datetime('now'), is_suspended=1, email=?, username=? WHERE id=?",
        (f"removed-{tag}+{child['email']}", f"removed-{tag}", child["id"])
    )
    db.execute("UPDATE stores SET slug=substr(?||slug, 1, 60) WHERE user_id=?",
               (f"removed-{tag}-", child["id"]))
    db.execute("DELETE FROM push_subscriptions WHERE user_id=?", (child["id"],))
    return None


def credit_agent_cut(config, db, order, label: str) -> int:
    """Pay the parent agent their cut of a dispatched sub-agent sale.

    Call inside an open global_db() block, once, when the order becomes dispatched.
    Mirrors the sale into the agent's own dashboard so it shows in their totals.
    """
    try:
        agent_id = order["agent_id"]
        cut = order["agent_profit_pesewas"] or 0
    except (KeyError, IndexError):
        return 0
    if not agent_id or cut <= 0:
        return 0

    db.execute("UPDATE users SET wallet_pesewas = wallet_pesewas + ? WHERE id=?", (cut, agent_id))

    sub_name = ""
    row = db.execute(
        "SELECT u.full_name FROM stores s JOIN users u ON u.id=s.user_id WHERE s.id=?",
        (order["store_id"],)
    ).fetchone()
    if row:
        sub_name = row["full_name"]

    tag = f"{label} · via {sub_name}" if sub_name else label
    with user_db(config, agent_id) as udb:
        udb.execute(
            """INSERT OR IGNORE INTO orders
               (id, bundle_label, network, customer_phone, amount_pesewas, profit_pesewas, status)
               VALUES (?,?,?,?,?,?,'dispatched')""",
            (order["id"], tag, order["network"], order["customer_phone"],
             order["amount_pesewas"], cut)
        )
        udb.execute("INSERT OR IGNORE INTO earnings (id, order_id, amount_pesewas) VALUES (?,?,?)",
                    (str(uuid.uuid4()), order["id"], cut))
    try:
        broadcast_push(config, agent_id, "Sub-agent sale!",
                       f"{sub_name or 'A sub-agent'} sold {label} — you earned GHS {cut/100:.2f}",
                       "/dashboard/sub-agents")
    except Exception:
        pass
    return cut
