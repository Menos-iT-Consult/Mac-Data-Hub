"""Sub-agents: an agent can create sub-agents who get their own dashboard and
storefront and sell data bundles like any other reseller.

Rules
- Only a top-level agent (users.parent_id IS NULL) can have sub-agents; sub-agents
  cannot create their own (one level deep).
- An agent can only act on the sub-agents they created (checked on every action).
- Suspend  -> sub-agent is signed out, cannot log in, and their store is hidden.
- Block withdrawals -> sub-agent can still sell, but cannot request withdrawals.
- Reset password -> a new random password is generated and shown once to the agent.
- Remove -> soft delete (order history stays intact); only allowed once the
  sub-agent's wallet is empty and they have no withdrawal in progress. Their email,
  username and store URL are released so the same person can be added again.
- Prices: the agent sets what sub-agents pay per bundle (never below base). The agent
  earns (that price - base) on every sub-agent sale; the sub-agent earns the rest.
"""
import re
import uuid
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, current_app, jsonify)
from werkzeug.security import generate_password_hash
from ..services.db import global_db, global_db_read
from ..services.team import toggle_flag, reset_password as _reset_pw, remove_sub_agent
from .reseller import reseller_required, _ctx

sub_agents_bp = Blueprint("sub_agents", __name__, url_prefix="/dashboard/sub-agents")

_USERNAME_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{1,28}[a-z0-9]$|^[a-z0-9]{3}$')
_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$')

def _err(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def _is_sub_agent(user) -> bool:
    return bool(user.get("parent_id"))


def _get_child(db, parent_id: str, child_id: str):
    """Return the sub-agent row only if it belongs to this agent and isn't removed."""
    return db.execute(
        "SELECT * FROM users WHERE id=? AND parent_id=? AND removed_at IS NULL",
        (child_id, parent_id)
    ).fetchone()


@sub_agents_bp.route("")
@sub_agents_bp.route("/")
@reseller_required
def index():
    config = current_app.config
    uid, user, store = _ctx(config)
    if _is_sub_agent(user):
        return redirect(url_for("reseller.dashboard"))

    with global_db_read(config) as db:
        rows = db.execute(
            """SELECT u.id, u.full_name, u.email, u.phone, u.username, u.wallet_pesewas,
                      u.is_suspended, u.withdrawals_blocked, u.created_at,
                      s.store_name, s.slug,
                      (SELECT COUNT(*) FROM orders o
                        WHERE o.store_id = s.id AND o.status='dispatched') AS sales,
                      (SELECT COALESCE(SUM(o.profit_pesewas),0) FROM orders o
                        WHERE o.store_id = s.id AND o.status='dispatched') AS profit,
                      (SELECT COALESCE(SUM(o.agent_profit_pesewas),0) FROM orders o
                        WHERE o.store_id = s.id AND o.status='dispatched' AND o.agent_id = ?) AS my_cut
               FROM users u LEFT JOIN stores s ON s.user_id = u.id
               WHERE u.parent_id=? AND u.removed_at IS NULL
               ORDER BY u.created_at DESC""",
            (uid, uid)
        ).fetchall()
    subs = [dict(r) for r in rows]
    stats = {
        "total": len(subs),
        "active": sum(1 for s in subs if not s["is_suspended"]),
        "suspended": sum(1 for s in subs if s["is_suspended"]),
        "sales": sum(s["sales"] for s in subs),
        "profit": sum(s["profit"] for s in subs),
        "my_cut": sum(s["my_cut"] for s in subs),
    }
    return render_template("reseller/sub_agents.html", user=user, store=store,
                           subs=subs, stats=stats)


@sub_agents_bp.route("/create", methods=["POST"])
@reseller_required
def create():
    config = current_app.config
    uid, user, _ = _ctx(config)
    if _is_sub_agent(user):
        return _err("Sub-agents cannot create their own sub-agents.", 403)
    if not user["is_active"]:
        return _err("Your account must be active to add sub-agents.", 403)

    data = request.get_json() or {}
    full_name = (data.get("full_name") or "").strip()
    phone     = (data.get("phone") or "").strip()
    email     = (data.get("email") or "").strip().lower()
    username  = (data.get("username") or "").strip().lower()
    slug      = (data.get("slug") or "").strip().lower().replace(" ", "-")
    password  = data.get("password") or ""

    missing = [n for n, v in [("full name", full_name), ("phone", phone), ("email", email),
                              ("username", username), ("store URL", slug), ("password", password)] if not v]
    if missing:
        return _err(f"Missing: {', '.join(missing)}.")
    if "@" not in email or "." not in email.split("@")[-1]:
        return _err("Enter a valid email address.")
    if not _USERNAME_RE.match(username) or re.search(r'[_-]{2,}', username):
        return _err("Username must be 3–30 characters, start and end with a letter or number, with no consecutive - or _.")
    if not _SLUG_RE.match(slug) or "--" in slug:
        return _err("Store URL must be 3–40 characters: letters, numbers and single hyphens only.")
    if len(password) < 8:
        return _err("Password must be at least 8 characters.")

    with global_db(config) as db:
        if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            return _err("That email is already registered.")
        if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return _err("That username is already taken.")
        if db.execute("SELECT 1 FROM stores WHERE slug=?", (slug,)).fetchone():
            return _err("That store URL is already taken.")

        sub_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO users (id, email, password_hash, full_name, phone, username,
                                  role, is_active, parent_id)
               VALUES (?,?,?,?,?,?,'reseller',1,?)""",
            (sub_id, email, generate_password_hash(password), full_name, phone, username, uid)
        )
        db.execute(
            "INSERT INTO stores (id, user_id, slug, store_name) VALUES (?,?,?,?)",
            (str(uuid.uuid4()), sub_id, slug, f"{full_name}'s Store")
        )

    login_url = f"{config['APP_URL'].rstrip('/')}/login"
    return jsonify({"ok": True, "login_url": login_url, "username": username,
                    "email": email, "password": password})


@sub_agents_bp.route("/<child_id>/suspend", methods=["POST"])
@reseller_required
def toggle_suspend(child_id):
    with global_db(current_app.config) as db:
        if not _get_child(db, session["user_id"], child_id):
            return _err("Sub-agent not found.", 404)
        return jsonify({"ok": True, "is_suspended": toggle_flag(db, child_id, "is_suspended")})


@sub_agents_bp.route("/<child_id>/block-withdrawals", methods=["POST"])
@reseller_required
def toggle_block_withdrawals(child_id):
    with global_db(current_app.config) as db:
        if not _get_child(db, session["user_id"], child_id):
            return _err("Sub-agent not found.", 404)
        return jsonify({"ok": True, "withdrawals_blocked": toggle_flag(db, child_id, "withdrawals_blocked")})


@sub_agents_bp.route("/<child_id>/reset-password", methods=["POST"])
@reseller_required
def reset_password(child_id):
    with global_db(current_app.config) as db:
        child = _get_child(db, session["user_id"], child_id)
        if not child:
            return _err("Sub-agent not found.", 404)
        pw = _reset_pw(db, child_id)
    return jsonify({"ok": True, "password": pw, "username": child["username"],
                    "email": child["email"], "name": child["full_name"]})


@sub_agents_bp.route("/<child_id>/remove", methods=["POST"])
@reseller_required
def remove(child_id):
    with global_db(current_app.config) as db:
        child = _get_child(db, session["user_id"], child_id)
        if not child:
            return _err("Sub-agent not found.", 404)
        error = remove_sub_agent(db, child)
        if error:
            return _err(error)
    return jsonify({"ok": True})


@sub_agents_bp.route("/prices", methods=["GET", "POST"])
@reseller_required
def prices():
    """What this agent charges their sub-agents per bundle. Agent earns (price - base) per sale."""
    config = current_app.config
    uid, user, store = _ctx(config)
    if _is_sub_agent(user):
        return redirect(url_for("reseller.dashboard"))

    with global_db_read(config) as db:
        bundles = [dict(b) for b in db.execute(
            "SELECT * FROM data_bundles WHERE is_active=1 ORDER BY network, volume_mb"
        ).fetchall()]
        existing = {r["bundle_id"]: r["price_pesewas"] for r in db.execute(
            "SELECT bundle_id, price_pesewas FROM sub_agent_pricing WHERE parent_id=?", (uid,)
        ).fetchall()}

    if request.method == "POST":
        data = request.get_json() or {}
        base_of = {b["id"]: b["base_price_pesewas"] for b in bundles}
        with global_db(config) as db:
            for bundle_id, price_str in data.items():
                if bundle_id not in base_of:
                    continue
                try:
                    price = round(float(price_str) * 100)
                except (ValueError, TypeError):
                    continue
                if price < base_of[bundle_id]:
                    continue
                db.execute(
                    """INSERT INTO sub_agent_pricing (id, parent_id, bundle_id, price_pesewas)
                       VALUES (?,?,?,?)
                       ON CONFLICT(parent_id, bundle_id) DO UPDATE SET price_pesewas=excluded.price_pesewas""",
                    (str(uuid.uuid4()), uid, bundle_id, price)
                )
        return jsonify({"ok": True})

    return render_template("reseller/sub_agent_prices.html", user=user, store=store,
                           bundles=bundles, existing=existing)
