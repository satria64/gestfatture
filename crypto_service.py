"""Crittografia at-rest dei secret salvati in DB (Fernet AES-128).

Master key opzionale via env var SECRETS_ENCRYPTION_KEY. Se non è presente,
encrypt/decrypt sono no-op: il sistema funziona in chiaro come prima.
Questo permette di attivare/disattivare la crittografia senza migrazioni
distruttive — i valori vecchi non cifrati restano leggibili, i nuovi
salvataggi vengono cifrati appena la key è in env.

Utility:
- generate_key() per produrre una nuova master key (eseguire una tantum)
- migrate_existing_secrets(db) per ri-cifrare tutti i valori già in DB
"""
import logging
import os

log = logging.getLogger(__name__)
PREFIX = "fernet:"

_FERNET = None  # cache


def _fernet():
    global _FERNET
    if _FERNET is not None:
        return _FERNET
    key = (os.environ.get("SECRETS_ENCRYPTION_KEY") or "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        _FERNET = Fernet(key.encode() if isinstance(key, str) else key)
        return _FERNET
    except Exception as e:
        log.warning("SECRETS_ENCRYPTION_KEY invalida: %s — encryption disabled", e)
        return None


def is_encryption_enabled() -> bool:
    return _fernet() is not None


def is_encrypted_value(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(plain) -> str:
    """Cifra un valore. Se non c'è master key o il valore è già cifrato, no-op."""
    if not plain:
        return plain
    plain = str(plain)
    if plain.startswith(PREFIX):
        return plain
    f = _fernet()
    if not f:
        return plain
    token = f.encrypt(plain.encode("utf-8")).decode("ascii")
    return PREFIX + token


def decrypt(value) -> str:
    """Decifra. Se il valore non è cifrato, lo restituisce così com'è (back-compat)."""
    if not value or not isinstance(value, str):
        return value
    if not value.startswith(PREFIX):
        return value
    f = _fernet()
    if not f:
        log.warning("decrypt: nessuna SECRETS_ENCRYPTION_KEY → ritorno valore cifrato")
        return value
    try:
        return f.decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except Exception as e:
        log.error("decrypt fallita: %s", e)
        return value


def generate_key() -> str:
    """Genera una nuova master key Fernet (base64 url-safe, 32 byte).
    Stampa il risultato in chiaro all'admin: è da copiare in SECRETS_ENCRYPTION_KEY env var."""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode("ascii")


def migrate_existing_secrets(db) -> dict:
    """Cifra tutti i valori delle chiavi sensibili attualmente non cifrati.
    Ritorna dict con conteggi per debug."""
    from models import AppSettings, UserSetting, SENSITIVE_APP_KEYS, SENSITIVE_USER_KEYS
    if not is_encryption_enabled():
        return {"error": "SECRETS_ENCRYPTION_KEY non configurata"}

    stats = {"app_encrypted": 0, "user_encrypted": 0, "skipped_already_enc": 0}

    for row in AppSettings.query.filter(AppSettings.key.in_(SENSITIVE_APP_KEYS)).all():
        if not row.value:
            continue
        if is_encrypted_value(row.value):
            stats["skipped_already_enc"] += 1
            continue
        row.value = encrypt(row.value)
        stats["app_encrypted"] += 1

    for row in UserSetting.query.filter(UserSetting.key.in_(SENSITIVE_USER_KEYS)).all():
        if not row.value:
            continue
        if is_encrypted_value(row.value):
            stats["skipped_already_enc"] += 1
            continue
        row.value = encrypt(row.value)
        stats["user_encrypted"] += 1

    db.session.commit()
    return stats
