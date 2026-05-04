"""Helper TOTP / 2FA: generazione segreti, QR code, backup codes, verifica."""
import base64
import hashlib
import io
import json
import secrets

import pyotp
import qrcode

ISSUER = "GestFatture"
BACKUP_CODE_COUNT = 8


def generate_secret() -> str:
    """Genera un nuovo segreto TOTP base32 (160 bit)."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    """URI 'otpauth://...' da scansionare nelle app authenticator."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)


def qr_data_uri(uri: str) -> str:
    """Restituisce un PNG base64 (data: URI) per <img src=...>."""
    img = qrcode.make(uri, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def verify(secret: str, code: str, valid_window: int = 1) -> bool:
    """Verifica un codice a 6 cifre. valid_window=1 → ±30s di tolleranza."""
    if not secret or not code:
        return False
    code = "".join(c for c in code if c.isdigit())
    if len(code) != 6:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=valid_window)


def generate_backup_codes(n: int = BACKUP_CODE_COUNT) -> list[str]:
    """Codici 8 caratteri hex maiuscoli, mostrati all'utente UNA volta."""
    return [secrets.token_hex(4).upper() for _ in range(n)]


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def hash_codes_json(codes: list[str]) -> str:
    return json.dumps([hash_code(c) for c in codes])


def consume_backup_code(stored_json: str, candidate: str) -> tuple[bool, str]:
    """Verifica se candidate è in stored_json e lo rimuove (single-use).

    Restituisce (ok, new_stored_json).
    """
    try:
        stored = json.loads(stored_json or "[]")
    except Exception:
        stored = []
    h = hash_code(candidate)
    if h not in stored:
        return False, json.dumps(stored)
    stored.remove(h)
    return True, json.dumps(stored)
