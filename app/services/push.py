"""Web Push notification service using VAPID + pywebpush."""
import json
from py_vapid import Vapid
from pywebpush import webpush, WebPushException
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from .db import global_db


def _generate_vapid_keypair() -> tuple[str, str]:
    """Generate a VAPID key pair using cryptography directly.
    
    Returns (private_pem_str, public_b64_str).
    This bypasses the deprecated py_vapid generate_keys() which
    fails with newer cryptography versions.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    # Export private key in PEM format
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    # Export public key in PEM format
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    # py_vapid expects a base64-encoded uncompressed EC point for browser subscription
    import base64
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return private_pem, public_b64


def _get_or_create_vapid(config) -> tuple[Vapid, str]:
    """Return (Vapid instance, public_key_b64), generating and persisting if needed."""
    with global_db(config) as db:
        priv_row = db.execute(
            "SELECT value FROM app_settings WHERE key='vapid_private_key'"
        ).fetchone()
        pub_row = db.execute(
            "SELECT value FROM app_settings WHERE key='vapid_public_key'"
        ).fetchone()

        priv_val = (priv_row["value"] or "") if priv_row else ""
        pub_val  = (pub_row["value"]  or "") if pub_row  else ""
        if priv_val.startswith("-----BEGIN"):
            vapid = Vapid.from_pem(priv_val.encode())
            return vapid, pub_val

        # Generate fresh VAPID key pair — purge stale subscriptions since they used the old key
        private_pem, public_b64 = _generate_vapid_keypair()
        vapid = Vapid.from_pem(private_pem.encode())

        db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('vapid_private_key', ?)",
            (private_pem,)
        )
        db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('vapid_public_key', ?)",
            (public_b64,)
        )
        db.execute("DELETE FROM push_subscriptions")
        return vapid, public_b64


def get_vapid_public_key(config) -> str:
    """Return the public key in the format the browser needs (uncompressed EC point, base64url)."""
    vapid, _ = _get_or_create_vapid(config)
    # Application server key for PushManager.subscribe must be the raw public key bytes
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    import base64
    raw = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _build_claims(config) -> dict:
    app_url = config.get("APP_URL", "http://localhost:5000")
    domain = app_url.replace("https://", "").replace("http://", "").split("/")[0] or "localhost"
    return {"sub": f"mailto:admin@{domain}"}


def _send_push_raw(vapid: Vapid, claims: dict, subscription: dict,
                   title: str, body: str, url: str, icon: str,
                   notif_type: str = "order") -> bool:
    """Low-level send — no DB access. Returns True on success, raises on 410."""
    payload = json.dumps({"title": title, "body": body, "url": url, "icon": icon, "type": notif_type})
    try:
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=vapid,
            vapid_claims=claims,
        )
        return True
    except WebPushException as e:
        if hasattr(e, "response") and e.response is not None and e.response.status_code == 410:
            raise
        return False


def send_push(config, subscription: dict, title: str, body: str,
              url: str = "/", icon: str = "/static/icons/icon-192.png") -> bool:
    """Send a push notification to one subscription. Returns True on success."""
    vapid, _ = _get_or_create_vapid(config)
    return _send_push_raw(vapid, _build_claims(config), subscription, title, body, url, icon)


def broadcast_push(config, user_id: str | None, title: str, body: str,
                   url: str = "/") -> None:
    """Send push to all subscriptions for a user_id (or all guests if None)."""
    vapid, _ = _get_or_create_vapid(config)
    claims = _build_claims(config)
    icon = "/static/icons/icon-192.png"

    with global_db(config) as db:
        if user_id:
            rows = db.execute(
                "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id=?",
                (user_id,)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id IS NULL"
            ).fetchall()
        rows = [dict(r) for r in rows]

    stale_ids = []
    for row in rows:
        sub = {"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}}
        try:
            _send_push_raw(vapid, claims, sub, title, body, url, icon)
        except WebPushException:
            stale_ids.append(row["id"])

    if stale_ids:
        with global_db(config) as db:
            for sid in stale_ids:
                db.execute("DELETE FROM push_subscriptions WHERE id=?", (sid,))


def broadcast_push_to_resellers(config, title: str, body: str, url: str = "/dashboard") -> int:
    """Send a push to every device subscribed by a reseller account.

    Never raises: one bad subscription (or push-service outage) must not break
    the admin broadcast. Returns the number of devices successfully notified.
    """
    try:
        vapid, _ = _get_or_create_vapid(config)
    except Exception:
        return 0
    claims = _build_claims(config)
    icon = "/static/icons/icon-192.png"

    with global_db(config) as db:
        rows = db.execute(
            "SELECT ps.id, ps.endpoint, ps.p256dh, ps.auth "
            "FROM push_subscriptions ps JOIN users u ON u.id = ps.user_id "
            "WHERE u.role = 'reseller'"
        ).fetchall()
        rows = [dict(r) for r in rows]

    sent, stale_ids = 0, []
    for row in rows:
        sub = {"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}}
        try:
            if _send_push_raw(vapid, claims, sub, title, body, url, icon, "broadcast"):
                sent += 1
        except WebPushException:
            stale_ids.append(row["id"])      # 410 Gone — device unsubscribed
        except Exception:
            continue                          # network hiccup etc. — skip this device

    if stale_ids:
        with global_db(config) as db:
            for sid in stale_ids:
                db.execute("DELETE FROM push_subscriptions WHERE id=?", (sid,))
    return sent
