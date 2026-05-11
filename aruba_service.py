"""Aruba Fatturazione Elettronica API v2 client — modalità Premium/Multiseller.

Un account Aruba Premium ("Multiseller") invia fatture al SDI per N P.IVA
cedenti registrate via web panel. GestFatture come SaaS multi-tenant usa
un unico set di credenziali Aruba globali (admin settings, cifrate at-rest);
ogni utente emette fatture col proprio `senderPIVA`.

Doc: https://fatturazioneelettronica.aruba.it/apidoc/v2/docs.html

Flow:
  1. Admin configura aruba_username + aruba_api_password + aruba_environment
     in /admin/settings (le credenziali sono cifrate at-rest se SECRETS_ENCRYPTION_KEY).
  2. Cedenti registrati a mano nel pannello web Aruba (NON via API).
  3. emit fattura → send_invoice(xml, sender_piva) → uploadFileName + requestId.
  4. Scheduler polling 30 min → list_invoices_out(sender_piva, last_24h) →
     aggiorna Invoice.sdi_status in base a STATUS_MAP.

Vincoli:
  - IdTrasmittente nell'XML deve essere SEMPRE Aruba PEC (IT 01879020517).
    Aruba rifiuta XML con IdTrasmittente diverso (errorCode 0094).
  - Max 5MB per file.
  - Rate limit per IP: 30 upload/min, 12 search/min, 1 auth/min.
  - `creationStartDate`-`creationEndDate` su /invoices-out: range max 2 giorni.
"""
from __future__ import annotations
import base64
import logging
from datetime import datetime, timedelta

import requests

log = logging.getLogger(__name__)

# ─── Costanti ──────────────────────────────────────────────────────────────
ARUBA_PEC_PIVA = "01879020517"  # IdTrasmittente obbligatorio (Aruba PEC S.p.A.)

ENV_URLS = {
    "sandbox": {
        "auth": "https://demoauth.fatturazioneelettronica.aruba.it",
        "api":  "https://demows.fatturazioneelettronica.aruba.it",
    },
    "production": {
        "auth": "https://auth.fatturazioneelettronica.aruba.it",
        "api":  "https://ws.fatturazioneelettronica.aruba.it",
    },
}

HTTP_TIMEOUT = 30

# Mapping stati Aruba 1-10 → Invoice.sdi_status GestFatture
STATUS_MAP = {
    "1":  "pending",                # Presa in carico (in elaborazione SDI)
    "2":  "error",                  # Errore elaborazione lato Aruba/SDI
    "3":  "sent",                   # Inviata al SDI
    "4":  "rejected",               # Scartata (NS) — XML non conforme
    "5":  "non_consegnata",         # MC — destinatario non raggiungibile
    "6":  "recapito_impossibile",   # AT — impossibile recapitare
    "7":  "delivered",              # Consegnata (RC)
    "8":  "accepted",               # Accettata dal destinatario (NE EC01)
    "9":  "rejected_by_recipient",  # Rifiutata dal destinatario (NE EC02)
    "10": "decorsi_termini",        # DT — decorrenza 15gg
}

# Stati "finali" per cui non serve più polling
TERMINAL_STATUSES = {"delivered", "rejected", "rejected_by_recipient",
                     "decorsi_termini", "non_consegnata",
                     "recapito_impossibile", "error"}


# ─── Helpers config ────────────────────────────────────────────────────────
def _get_settings() -> dict:
    from models import AppSettings
    return {
        "username":    AppSettings.get("aruba_username", "").strip(),
        "password":    AppSettings.get("aruba_api_password", "").strip(),
        "environment": AppSettings.get("aruba_environment", "sandbox").strip(),
        "enabled":     AppSettings.get("aruba_enabled", "false").strip().lower() == "true",
    }


def is_enabled() -> bool:
    """True se Aruba è abilitato e configurato (master toggle + creds)."""
    s = _get_settings()
    return s["enabled"] and bool(s["username"]) and bool(s["password"])


def _env_urls() -> dict:
    env = _get_settings()["environment"]
    return ENV_URLS.get(env, ENV_URLS["sandbox"])


# ─── Auth (OAuth2 Password Grant) ──────────────────────────────────────────
def _login() -> dict:
    """OAuth2 Password Grant. Salva token + expiry in AppSettings."""
    s = _get_settings()
    if not s["username"] or not s["password"]:
        raise RuntimeError("Aruba: username/password non configurati in admin settings")
    url = f"{_env_urls()['auth']}/auth/signin"
    body = {
        "grant_type": "password",
        "username":   s["username"],
        "password":   s["password"],
    }
    r = requests.post(url, data=body, timeout=HTTP_TIMEOUT)
    log.info("Aruba auth login → %d", r.status_code)
    if r.status_code != 200:
        raise RuntimeError(f"Aruba auth fallito {r.status_code}: {r.text[:300]}")
    data = r.json()
    _save_token(data)
    return data


def _refresh(refresh_token: str) -> dict | None:
    """OAuth2 Refresh. Ritorna None se fallisce (chiamante ricade su _login)."""
    url = f"{_env_urls()['auth']}/auth/signin"
    body = {
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
    }
    r = requests.post(url, data=body, timeout=HTTP_TIMEOUT)
    log.info("Aruba auth refresh → %d", r.status_code)
    if r.status_code != 200:
        log.warning("Aruba refresh fallito: %s", r.text[:200])
        return None
    data = r.json()
    _save_token(data)
    return data


def _save_token(data: dict):
    from models import AppSettings
    AppSettings.set("aruba_access_token", data.get("access_token", ""))
    AppSettings.set("aruba_refresh_token", data.get("refresh_token", ""))
    # Margine 60s per evitare di chiamare con un token scaduto al margine
    exp_in = int(data.get("expires_in", 1800))
    exp_at = datetime.utcnow() + timedelta(seconds=max(60, exp_in - 60))
    AppSettings.set("aruba_token_expires_at", exp_at.isoformat())


def _get_valid_token() -> str:
    """Ritorna un access_token valido. Gestisce refresh + relogin trasparenti."""
    from models import AppSettings
    token = AppSettings.get("aruba_access_token", "").strip()
    exp_iso = AppSettings.get("aruba_token_expires_at", "").strip()
    if token and exp_iso:
        try:
            if datetime.fromisoformat(exp_iso) > datetime.utcnow():
                return token
        except ValueError:
            pass
    # Token scaduto o assente
    refresh = AppSettings.get("aruba_refresh_token", "").strip()
    if refresh:
        data = _refresh(refresh)
        if data:
            return data["access_token"]
    return _login()["access_token"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_valid_token()}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


# ─── Upload fattura outgoing ───────────────────────────────────────────────
def send_invoice(xml_content: str, sender_piva: str) -> dict:
    """Invia una fattura FatturaPA al SDI via Aruba.

    Args:
        xml_content: stringa XML FatturaPA non firmata (Aruba firma con sua chiave).
        sender_piva: P.IVA del cedente (11 cifre, prefisso IT aggiunto se mancante).
                     Deve essere registrato come cedente Premium nel pannello Aruba.

    Returns:
        dict con `aruba_filename` (es. IT01879020517_xxxxx.xml.p7m) +
        `request_id` (UUID per tracking) + `raw_response`.

    Raises:
        RuntimeError con messaggio diagnostico su errore HTTP o validazione.
    """
    if not xml_content:
        raise RuntimeError("Aruba send_invoice: xml_content vuoto")
    if not sender_piva:
        raise RuntimeError("Aruba send_invoice: sender_piva obbligatorio")
    piva = sender_piva.strip().upper()
    if not piva.startswith("IT"):
        piva = f"IT{piva}"
    url = f"{_env_urls()['api']}/services/invoice/upload"
    payload = {
        "dataFile":   base64.b64encode(xml_content.encode("utf-8")).decode("ascii"),
        "senderPIVA": piva,
    }
    r = requests.post(url, json=payload, headers=_headers(), timeout=HTTP_TIMEOUT)
    log.info("Aruba upload sender=%s → %d", piva, r.status_code)
    if r.status_code >= 400:
        log.error("Aruba upload body: %s", r.text[:500])
        raise RuntimeError(f"Aruba upload HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    err = str(data.get("errorCode", "")).strip()
    if err not in ("", "0000"):
        raise RuntimeError(f"Aruba upload errorCode={err}: "
                           f"{data.get('errorDescription', '')[:300]}")
    # errorDescription pattern: "Operazione effettuata - {requestId}"
    desc = data.get("errorDescription", "") or ""
    request_id = desc.split(" - ", 1)[1].strip() if " - " in desc else ""
    return {
        "aruba_filename": data.get("uploadFileName", ""),
        "request_id":     request_id,
        "raw_response":   data,
    }


# ─── Polling stato fatture inviate ──────────────────────────────────────────
def list_invoices_out(sender_piva: str, date_from: datetime, date_to: datetime,
                      page: int = 1, size: int = 100) -> list[dict]:
    """Recupera lista fatture inviate per un cedente nel range date.

    Vincolo Aruba: differenza max 2 giorni tra date_from e date_to.
    """
    if (date_to - date_from) > timedelta(days=2):
        raise RuntimeError("Aruba list_invoices_out: range date max 2 giorni")
    piva = sender_piva.strip().upper()
    if piva.startswith("IT"):
        piva = piva[2:]  # senderVatcode senza prefisso IT
    url = f"{_env_urls()['api']}/api/v2/invoices-out"
    params = {
        "creationStartDate": date_from.strftime("%Y-%m-%dT%H:%M:%S"),
        "creationEndDate":   date_to.strftime("%Y-%m-%dT%H:%M:%S"),
        "senderCountry":     "IT",
        "senderVatcode":     piva,
        "page":              page,
        "size":              size,
    }
    r = requests.get(url, params=params, headers=_headers(), timeout=HTTP_TIMEOUT)
    log.info("Aruba list_invoices_out sender=%s page=%d → %d", piva, page, r.status_code)
    if r.status_code >= 400:
        log.error("Aruba list body: %s", r.text[:500])
        raise RuntimeError(f"Aruba list HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    # V2 risposta: cerca array sotto chiavi note
    return (data.get("invoices") or data.get("data")
            or data.get("results") or [])


def get_invoice_detail(aruba_filename: str) -> dict:
    """Dettaglio di una fattura Aruba via filename (es. IT...._xxxxx.xml.p7m)."""
    if not aruba_filename:
        raise RuntimeError("Aruba get_invoice_detail: filename obbligatorio")
    url = f"{_env_urls()['api']}/api/v2/invoices-out/detail"
    params = {"filename": aruba_filename}
    r = requests.get(url, params=params, headers=_headers(), timeout=HTTP_TIMEOUT)
    log.info("Aruba get_detail filename=%s → %d", aruba_filename, r.status_code)
    if r.status_code >= 400:
        log.error("Aruba detail body: %s", r.text[:500])
        raise RuntimeError(f"Aruba detail HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def aruba_status_to_gestfatture(status_value) -> str:
    """Mappa lo stato numerico Aruba (1-10) al valore Invoice.sdi_status.
    Default: 'sent' se sconosciuto (comportamento conservativo)."""
    s = str(status_value).strip()
    return STATUS_MAP.get(s, "sent")
