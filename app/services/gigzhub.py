import hmac
import hashlib
import requests
from urllib.parse import quote
from flask import current_app

_DEFAULT_BASE = "https://gigzhub.net/api/v1"

NETWORK_MAP = {
    "mtn": "MTN",
    "telecel": "Telecel",
    "airteltigo": "AirtelTigo",
}


def webhook_token() -> str:
    """Secret that proves a callback really came from GigzHub (we put it in the URL we give them)."""
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, b"gigzhub-webhook", hashlib.sha256).hexdigest()[:40]


def webhook_url() -> str:
    try:
        app_url = (current_app.config.get("APP_URL") or "").rstrip("/")
        return f"{app_url}/webhook/gigzhub?token={webhook_token()}" if app_url else ""
    except RuntimeError:
        return ""


def _base() -> str:
    try:
        url = current_app.config.get("GIGZHUB_BASE_URL", _DEFAULT_BASE) or _DEFAULT_BASE
        url = url.rstrip("/").removesuffix("/offers")
        # Ensure /v1 is always present — guard against admin saving bare /api URL
        if url.endswith("/api"):
            url = url + "/v1"
        return url
    except RuntimeError:
        return _DEFAULT_BASE


def _headers(api_key: str) -> dict:
    return {"x-api-key": api_key, "Content-Type": "application/json"}


def get_balance(api_key: str) -> dict:
    """Return GigzHub wallet balance. Returns {"balance": float, "currency": str}."""
    resp = requests.get(f"{_base()}/balance",
                        headers=_headers(api_key), timeout=10)
    if not resp.ok:
        raise ValueError(f"GigzHub returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except Exception:
        raise ValueError(f"GigzHub returned non-JSON: {resp.text[:200]}")


def get_offers(api_key: str) -> list:
    resp = requests.get(f"{_base()}/offers",
                        headers=_headers(api_key), timeout=15)
    if not resp.ok:
        raise ValueError(f"GigzHub returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except Exception:
        raise ValueError(
            f"GigzHub returned non-JSON (HTTP {resp.status_code}). "
            f"Response: {resp.text[:200] or '(empty)'}"
        )


def dispatch_bundle(api_key: str, network: str, phone: str,
                    offer_slug: str, volume_mb: int = 0) -> dict:
    """
    POST /order/:network
    Payload: {"phone", "offerSlug", "volume" (GB units), "type": "single"}
    volume_mb is converted to GB (integer) before sending.
    Returns the full parsed JSON body. Raises ValueError on HTTP error.
    """
    network_key = NETWORK_MAP.get(network.lower())
    if not network_key:
        raise ValueError(f"Unsupported network: {network}")
    volume_gb = max(1, round(volume_mb / 1000)) if volume_mb else 0
    payload = {
        "phone": phone,
        "offerSlug": offer_slug,
        "type": "single",
    }
    if volume_gb:
        payload["volume"] = volume_gb
    hook = webhook_url()          # GigzHub POSTs every status change here
    if hook:
        payload["webhookUrl"] = hook
    resp = requests.post(
        f"{_base()}/order/{network_key}",
        json=payload, headers=_headers(api_key), timeout=30
    )
    try:
        body = resp.json()
    except Exception:
        raise ValueError(
            f"GigzHub returned non-JSON (HTTP {resp.status_code}): {resp.text[:400]}"
        )
    # 409 DUPLICATE_ORDER means GigzHub already has this order pending — treat as success
    if resp.status_code == 409 and body.get("type") == "DUPLICATE_ORDER":
        return {"orderId": "", "status": "pending", "_duplicate": True}
    if not resp.ok:
        raise ValueError(
            f"GigzHub order failed (HTTP {resp.status_code}): {resp.text[:400]}"
        )
    return body


def check_beneficiary(api_key: str, phone: str, offer_slug: str) -> dict:
    """
    POST /beneficiary-check  {"phone", "offerSlug"}
    Returns {"eligible": bool, "message": str}.
    Raises ValueError when the check itself could not be completed
    (network error, GigzHub 5xx, unparseable response) so callers can fail closed.
    """
    try:
        resp = requests.post(
            f"{_base()}/beneficiary-check",
            json={"phone": phone, "offerSlug": offer_slug},
            headers=_headers(api_key), timeout=15,
        )
    except requests.RequestException as exc:
        raise ValueError(f"Could not reach verification service: {exc}")
    try:
        body = resp.json()
    except Exception:
        raise ValueError(f"Verification service returned non-JSON (HTTP {resp.status_code})")
    if resp.status_code >= 500:
        raise ValueError(f"Verification service error (HTTP {resp.status_code})")

    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    eligible = body.get("eligible", data.get("eligible"))
    message = (body.get("message") or data.get("message")
               or body.get("error") or data.get("reason") or "")
    # Only an explicit eligible == true on a 2xx response counts as verified
    return {"eligible": bool(resp.ok and eligible is True), "message": str(message)[:200]}


# ── Order status lookup ──────────────────────────────────────────────────────
# GigzHub's raw status words -> this app's order status.
#   dispatched = accepted by GigzHub, waiting in their queue
#   processing = GigzHub is delivering it
#   completed  = delivered
#   failed     = GigzHub rejected / cancelled / could not deliver
# Anything not listed here returns None, so an unfamiliar word never changes an order.
_STATUS_WORDS = {
    "dispatched": {"pending", "queued", "queue", "waiting", "accepted", "submitted",
                   "created", "received", "new", "placed", "initiated", "dispatched"},
    "processing": {"processing", "in_progress", "inprogress", "running", "sending", "ongoing"},
    "completed":  {"completed", "complete", "delivered", "success", "successful",
                   "done", "fulfilled"},
    "failed":     {"failed", "failure", "error", "cancelled", "canceled", "rejected",
                   "refunded", "declined", "reversed"},
}


def normalize_status(raw) -> str | None:
    word = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    for ours, words in _STATUS_WORDS.items():
        if word in words:
            return ours
    return None


def _status_path() -> str:
    try:
        path = current_app.config.get("GIGZHUB_STATUS_PATH") or ""
    except RuntimeError:
        path = ""
    return path or "/order/status/{id}"


def get_order_status(api_key: str, gigzhub_order_id: str) -> dict:
    """
    GET <base><GIGZHUB_STATUS_PATH> with {id} replaced by the GigzHub order id.
    Returns {"raw": "<GigzHub's own status word>", "status": <our status or None>}.
    Raises ValueError if the lookup itself fails, so callers leave the order untouched.
    """
    url = _base() + _status_path().replace("{id}", quote(str(gigzhub_order_id), safe=""))
    try:
        resp = requests.get(url, headers=_headers(api_key), timeout=15)
    except requests.RequestException as exc:
        raise ValueError(f"Could not reach GigzHub: {exc}")
    if not resp.ok:
        raise ValueError(f"GigzHub status lookup HTTP {resp.status_code} for {url}: {resp.text[:150]}")
    try:
        body = resp.json()
    except Exception:
        raise ValueError(f"GigzHub status lookup returned non-JSON: {resp.text[:150]}")

    layers = [body] if isinstance(body, dict) else []
    if isinstance(body, dict):
        for key in ("data", "order"):
            if isinstance(body.get(key), dict):
                layers.append(body[key])
    raw = ""
    for layer in layers:
        for key in ("status", "orderStatus", "order_status", "state"):
            if layer.get(key):
                raw = str(layer[key])
                break
        if raw:
            break
    if not raw:
        raise ValueError(f"No status field in GigzHub response: {str(body)[:150]}")
    return {"raw": raw[:40], "status": normalize_status(raw)}
