import requests
from flask import current_app

_DEFAULT_BASE = "https://gigzhub.net/api/v1"

NETWORK_MAP = {
    "mtn": "MTN",
    "telecel": "Telecel",
    "airteltigo": "AirtelTigo",
}


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
