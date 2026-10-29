import re
import uuid
import secrets
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, current_app, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash
from ..services.db import global_db, global_db_read
from ..services.paystack import initialize_transaction

_USERNAME_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{1,28}[a-z0-9]$|^[a-z0-9]{3}$')

auth_bp = Blueprint("auth", __name__)

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _ensure_referral_code(db, user_id):
    """Return the user's referral code, creating a unique one if missing."""
    row = db.execute("SELECT referral_code FROM users WHERE id=?", (user_id,)).fetchone()
    if row and row["referral_code"]:
        return row["referral_code"]
    while True:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        if not db.execute("SELECT 1 FROM users WHERE referral_code=?", (code,)).fetchone():
            break
    db.execute("UPDATE users SET referral_code=? WHERE id=?", (code, user_id))
    return code


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("auth/login.html")

    identifier = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    config = current_app.config

    # Admin is env-only
    if identifier == config["ADMIN_EMAIL"].lower() and password == config["ADMIN_PASSWORD"]:
        session.clear()
        session["user_id"] = "admin"
        session["role"] = "admin"
        session["email"] = identifier
        return redirect(url_for("admin.dashboard"))

    with global_db_read(config) as db:
        user = db.execute(
            "SELECT * FROM users WHERE (email=? OR username=?) AND is_active=1",
            (identifier, identifier)
        ).fetchone()

    if not user or not check_password_hash(user["password_hash"], password):
        flash("Invalid email/username or password.", "error")
        return render_template("auth/login.html")

    # Make sure this account has a referral code (older accounts won't yet)
    with global_db(config) as db:
        code = _ensure_referral_code(db, user["id"])

    session.clear()
    session["user_id"] = user["id"]
    session["role"] = "reseller"
    session["email"] = user["email"]
    session["full_name"] = user["full_name"]
    session["referral_popup"] = code   # shown once, right after sign-in
    return redirect(url_for("reseller.dashboard"))


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("public.home"))


@auth_bp.route("/apply")
def apply_page():
    """Redirect /apply to register for backward compat."""
    return redirect(url_for("auth.register"))


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        config = current_app.config
        ref = request.args.get("ref", "").strip().upper()
        if ref:
            session["ref"] = ref[:12]
        with global_db_read(config) as db:
            fee_row  = db.execute(
                "SELECT value FROM app_settings WHERE key='reseller_registration_fee_pesewas'"
            ).fetchone()
            free_row = db.execute(
                "SELECT value FROM app_settings WHERE key='registration_free'"
            ).fetchone()
        is_free = free_row and free_row["value"] == "1"
        return render_template("auth/register.html",
                               fee_pesewas=int(fee_row["value"]) if fee_row else 5000,
                               is_free=is_free)

    data = request.get_json()
    config = current_app.config
    email      = data.get("email", "").strip().lower()
    password   = data.get("password", "")
    full_name  = data.get("full_name", "").strip()
    phone      = data.get("phone", "").strip()
    username   = data.get("username", "").strip().lower()
    slug       = data.get("slug", "").strip().lower().replace(" ", "-")

    missing = [f for f, v in [("full name", full_name), ("phone", phone),
               ("email", email), ("username", username), ("store URL", slug),
               ("password", password)] if not v]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}."}), 400

    if not _USERNAME_RE.match(username) or re.search(r'[_-]{2,}', username):
        return jsonify({"error": "Username must be 3–30 characters, start and end with a letter or number, and cannot have consecutive hyphens or underscores."}), 400

    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    with global_db(config) as db:
        if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            return jsonify({"error": "Email already registered."}), 400

        if db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
            return jsonify({"error": "Username already taken."}), 400

        if db.execute("SELECT id FROM stores WHERE slug=?", (slug,)).fetchone():
            return jsonify({"error": "Store URL is already taken."}), 400

        fee_row  = db.execute(
            "SELECT value FROM app_settings WHERE key='reseller_registration_fee_pesewas'"
        ).fetchone()
        free_row = db.execute(
            "SELECT value FROM app_settings WHERE key='registration_free'"
        ).fetchone()
        is_free = free_row and free_row["value"] == "1"
        fee = int(fee_row["value"]) if fee_row else 5000

        user_id  = str(uuid.uuid4())
        store_id = str(uuid.uuid4())

        referred_by = None
        ref_code = session.get("ref")
        if ref_code:
            referrer = db.execute(
                "SELECT id FROM users WHERE referral_code=?", (ref_code,)
            ).fetchone()
            referred_by = referrer["id"] if referrer else None

        if is_free:
            # Activate immediately — no payment needed
            db.execute(
                """INSERT INTO users (id, email, password_hash, full_name, phone, username, role, is_active, referred_by)
                   VALUES (?,?,?,?,?,?,'reseller',1,?)""",
                (user_id, email, generate_password_hash(password), full_name, phone, username, referred_by)
            )
            db.execute(
                "INSERT INTO stores (id, user_id, slug, store_name) VALUES (?,?,?,?)",
                (store_id, user_id, slug, f"{full_name}'s Store")
            )
            return jsonify({"redirect": "/login?registered=1"})

        # Paid path — create pending user, charge via Paystack
        reg_id    = str(uuid.uuid4())
        reference = f"REG-{reg_id[:8].upper()}"
        db.execute(
            """INSERT INTO users (id, email, password_hash, full_name, phone, username, role, is_active, referred_by)
               VALUES (?,?,?,?,?,?,'reseller',0,?)""",
            (user_id, email, generate_password_hash(password), full_name, phone, username, referred_by)
        )
        db.execute(
            """INSERT INTO reseller_registrations
               (id, user_id, amount_pesewas, paystack_reference, status)
               VALUES (?,?,?,?,'pending')""",
            (reg_id, user_id, fee, reference)
        )
        db.execute(
            "INSERT INTO stores (id, user_id, slug, store_name) VALUES (?,?,?,?)",
            (store_id, user_id, slug, f"{full_name}'s Store")
        )

    callback_url = f"{config['APP_URL']}/register/verify?ref={reference}"
    result = initialize_transaction(
        config["PAYSTACK_SECRET_KEY"], email, fee, reference, callback_url,
        metadata={"type": "registration", "user_id": user_id}
    )
    return jsonify({
        "authorization_url": result["data"]["authorization_url"],
        "reference": reference,
    })


@auth_bp.route("/register/verify")
def register_verify():
    ref = request.args.get("ref", "")
    return render_template("auth/register_verify.html", reference=ref)
