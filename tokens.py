"""
Token firmati per quick actions via email / WhatsApp.

Un token codifica (user_id, invoice_id, action, timestamp) firmato con SECRET_KEY.
Validità: 30 giorni. Stesso token usabile più volte (la conferma 2-step previene
double-clicks e link-preview attacks).
"""

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask import current_app

SALT = "gestfatture-quick-action-v1"
DEFAULT_MAX_AGE = 60 * 60 * 24 * 30   # 30 giorni

ACTIONS = {
    "pre":     ("Promemoria pre-scadenza", "pre_scadenza"),
    "s1":      ("1° Sollecito",            "sollecito_1"),
    "s2":      ("2° Sollecito",            "sollecito_2"),
    "s3":      ("3° Sollecito",            "sollecito_3"),
    "diffida": ("Diffida formale",         "diffida"),
    "paid":    ("Marca come pagata",       None),
    "stop":    ("Disabilita ulteriori notifiche", None),
}


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=SALT)


def make_token(user_id: int, invoice_id: int, action: str) -> str:
    return _serializer().dumps({"u": user_id, "i": invoice_id, "a": action})


def verify_token(token: str, max_age: int = DEFAULT_MAX_AGE) -> dict | None:
    try:
        payload = _serializer().loads(token, max_age=max_age)
        if (
            isinstance(payload, dict)
            and "u" in payload and "i" in payload and "a" in payload
            and payload["a"] in ACTIONS
        ):
            return payload
    except (BadSignature, SignatureExpired):
        return None
    return None


def make_action_url(invoice, action: str, base_url: str = "") -> str:
    """Genera l'URL completo per una quick action."""
    from models import AppSettings
    if not base_url:
        base_url = AppSettings.get("app_external_url", "http://127.0.0.1:5000")
    base_url = base_url.rstrip("/")
    token = make_token(invoice.user_id, invoice.id, action)
    return f"{base_url}/quick/{token}"


# ─── Customer portal ────────────────────────────────────────────────────────
PORTAL_SALT    = "gestfatture-client-portal-v1"
PORTAL_MAX_AGE = 60 * 60 * 24 * 365  # 1 anno


def _portal_serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=PORTAL_SALT)


def make_portal_token(client_id: int, user_id: int) -> str:
    """Token firmato per accesso pubblico del cliente alla sua pagina insoluti."""
    return _portal_serializer().dumps({"c": client_id, "u": user_id})


def verify_portal_token(token: str, max_age: int = PORTAL_MAX_AGE) -> dict | None:
    try:
        payload = _portal_serializer().loads(token, max_age=max_age)
        if isinstance(payload, dict) and "c" in payload and "u" in payload:
            return payload
    except (BadSignature, SignatureExpired):
        return None
    return None


def make_portal_url(client, base_url: str = "") -> str:
    from models import AppSettings
    if not base_url:
        base_url = AppSettings.get("app_external_url", "http://127.0.0.1:5000")
    base_url = base_url.rstrip("/")
    token = make_portal_token(client.id, client.user_id)
    return f"{base_url}/portal/{token}"
