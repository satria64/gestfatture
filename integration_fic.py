"""
Fatture in Cloud (TeamSystem) integration via API REST v2 + OAuth2.

Documentazione: https://developers.fattureincloud.it/
"""

import logging
import requests
from datetime import datetime, timedelta
from urllib.parse import urlencode

log = logging.getLogger(__name__)

AUTH_URL  = "https://api-v2.fattureincloud.it/oauth/authorize"
TOKEN_URL = "https://api-v2.fattureincloud.it/oauth/token"
API_BASE  = "https://api-v2.fattureincloud.it"

SCOPES = " ".join([
    "entity.clients:r",
    "entity.suppliers:r",
    "issued_documents.invoices:r",
    "received_documents:r",
])


# ─── OAuth2 flow ──────────────────────────────────────────────────────────────
def get_authorize_url(client_id: str, redirect_uri: str, state: str = "") -> str:
    params = {
        "response_type": "code",
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "scope":         SCOPES,
        "state":         state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str, client_id: str, client_secret: str) -> dict:
    r = requests.post(TOKEN_URL, json={
        "grant_type":    "authorization_code",
        "client_id":     client_id,
        "client_secret": client_secret,
        "code":          code,
        "redirect_uri":  redirect_uri,
    }, timeout=30)
    r.raise_for_status()
    return r.json()


def refresh_access_token(refresh_token: str, client_id: str, client_secret: str) -> dict:
    r = requests.post(TOKEN_URL, json={
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "client_id":     client_id,
        "client_secret": client_secret,
    }, timeout=30)
    r.raise_for_status()
    return r.json()


def _get_token_or_refresh(user_id: int) -> str | None:
    """Restituisce un access_token valido per l'utente specificato."""
    from models import UserSetting, AppSettings
    access  = UserSetting.get(user_id, "integration_fic_access_token", "")
    refresh = UserSetting.get(user_id, "integration_fic_refresh_token", "")
    expires = UserSetting.get(user_id, "integration_fic_token_expires_at", "")
    # Client ID/Secret sono globali (admin), non per-user
    cid     = AppSettings.get("integration_fic_client_id", "")
    csec    = AppSettings.get("integration_fic_client_secret", "")

    if not access:
        return None

    needs_refresh = False
    if expires:
        try:
            exp_dt = datetime.fromisoformat(expires)
            if exp_dt - datetime.utcnow() < timedelta(seconds=60):
                needs_refresh = True
        except Exception:
            pass

    if needs_refresh and refresh and cid and csec:
        try:
            tokens = refresh_access_token(refresh, cid, csec)
            access = tokens["access_token"]
            UserSetting.set(user_id, "integration_fic_access_token", access)
            if tokens.get("refresh_token"):
                UserSetting.set(user_id, "integration_fic_refresh_token", tokens["refresh_token"])
            if tokens.get("expires_in"):
                exp = datetime.utcnow() + timedelta(seconds=int(tokens["expires_in"]))
                UserSetting.set(user_id, "integration_fic_token_expires_at", exp.isoformat())
        except Exception as e:
            log.error("FiC u=%d: refresh token fallito: %s", user_id, e)
            return None

    return access


# ─── API calls ────────────────────────────────────────────────────────────────
def _api_get(path: str, access_token: str, params: dict | None = None) -> dict:
    r = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params or {},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_companies(access_token: str) -> list[dict]:
    data = _api_get("/user/companies", access_token)
    info = data.get("data", {}).get("companies", {})
    # FiC v2: companies sotto controlled/owned
    out = []
    for k in ("controlled", "owned"):
        out.extend(info.get(k, []) or [])
    return out


def get_issued_invoices(access_token: str, company_id: int,
                        since_iso: str | None = None) -> list[dict]:
    params = {"type": "invoice", "per_page": 100}
    if since_iso:
        params["q"] = f'last_update >= "{since_iso}"'
    data = _api_get(f"/c/{company_id}/issued_documents", access_token, params)
    return data.get("data", []) or []


def get_received_invoices(access_token: str, company_id: int,
                          since_iso: str | None = None) -> list[dict]:
    params = {"per_page": 100}
    if since_iso:
        params["q"] = f'last_update >= "{since_iso}"'
    data = _api_get(f"/c/{company_id}/received_documents", access_token, params)
    return data.get("data", []) or []


# ─── Sync ─────────────────────────────────────────────────────────────────────
def _convert_fic_to_invoice(fic_doc: dict) -> dict:
    """Converte un documento FiC in dict compatibile con la nostra import pipeline."""
    entity = fic_doc.get("entity") or {}
    number = str(fic_doc.get("number", "")).strip()
    numer  = str(fic_doc.get("numeration", "")).strip()
    full_number = f"{number}{numer}" if numer else number

    issue_date = None
    if fic_doc.get("date"):
        try:
            issue_date = datetime.strptime(fic_doc["date"], "%Y-%m-%d").date()
        except ValueError:
            pass

    due_date = None
    if fic_doc.get("next_due_date"):
        try:
            due_date = datetime.strptime(fic_doc["next_due_date"], "%Y-%m-%d").date()
        except ValueError:
            pass

    addr_parts = []
    if entity.get("address_street"):  addr_parts.append(entity["address_street"])
    cap_city = " ".join(p for p in [entity.get("address_postal_code"),
                                     entity.get("address_city")] if p).strip()
    if cap_city: addr_parts.append(cap_city)
    if entity.get("address_province"): addr_parts.append(f"({entity['address_province']})")
    address = ", ".join(addr_parts)[:200] if addr_parts else ""

    return {
        "number":      full_number or None,
        "amount":      fic_doc.get("amount_gross") or fic_doc.get("amount_net") or 0.0,
        "issue_date":  issue_date,
        "due_date":    due_date,
        "client_name": entity.get("name", "").strip() or None,
        "vat_number":  entity.get("vat_number", "").strip() or None,
        "address":     address or None,
        "email":       entity.get("email", "").strip() or None,
    }


def sync_for_user(app, user_id: int):
    """Sincronizza Fatture in Cloud per un singolo utente."""
    from models import UserSetting, db, Client, Invoice

    with app.app_context():
        if UserSetting.get(user_id, "integration_fic_enabled") != "true":
            return

        access = _get_token_or_refresh(user_id)
        if not access:
            return

        company_id = UserSetting.get(user_id, "integration_fic_company_id", "")
        if not company_id:
            return

        last_sync = UserSetting.get(user_id, "integration_fic_last_sync", "")

        try:
            issued = get_issued_invoices(access, int(company_id), since_iso=last_sync or None)
        except Exception as e:
            log.error("FiC u=%d: get_issued_invoices fallita: %s", user_id, e)
            UserSetting.set(user_id, "integration_fic_last_error", str(e))
            return

        try:
            received = get_received_invoices(access, int(company_id), since_iso=last_sync or None)
        except Exception as e:
            log.warning("FiC u=%d: get_received_invoices fallita: %s", user_id, e)
            received = []

        all_docs = list(issued) + list(received)
        log.info("FiC sync u=%d: %d documenti da elaborare", user_id, len(all_docs))

        from datetime import timedelta as _td
        from datetime import date as _date
        imported = skipped = 0

        for doc in all_docs:
            data = _convert_fic_to_invoice(doc)
            if not data.get("number"):
                skipped += 1; continue
            if Invoice.query.filter_by(number=data["number"], user_id=user_id).first():
                skipped += 1; continue

            client_name = data.get("client_name") or f"Cliente FiC #{doc.get('id')}"
            client = Client.query.filter(Client.name.ilike(client_name), Client.user_id == user_id).first()
            if not client:
                client = Client(
                    user_id    = user_id,
                    name       = client_name,
                    vat_number = data.get("vat_number") or "",
                    address    = data.get("address") or "",
                    email      = data.get("email") or "",
                )
                db.session.add(client); db.session.flush()
            else:
                if data.get("vat_number") and not client.vat_number: client.vat_number = data["vat_number"]
                if data.get("address")    and not client.address:    client.address    = data["address"]
                if data.get("email")      and not client.email:      client.email      = data["email"]

            issue_date = data.get("issue_date") or _date.today()
            due_date   = data.get("due_date")   or (issue_date + _td(days=30))

            inv = Invoice(
                user_id    = user_id,
                client_id  = client.id,
                number     = data["number"],
                amount     = float(data.get("amount") or 0.0),
                issue_date = issue_date,
                due_date   = due_date,
                notes      = f"Importata da Fatture in Cloud (id={doc.get('id')})",
            )
            inv.update_status()
            db.session.add(inv)
            imported += 1

        db.session.commit()

        log.info("FiC sync u=%d: %d importate, %d saltate.", user_id, imported, skipped)
        UserSetting.set(user_id, "integration_fic_last_sync", datetime.utcnow().isoformat())
        UserSetting.set(user_id, "integration_fic_last_error", "")
        UserSetting.set(user_id, "integration_fic_last_count", str(imported))


def sync(app):
    """Job periodico: itera su tutti gli utenti con FiC abilitata."""
    from models import User
    with app.app_context():
        users = User.query.all()
    for u in users:
        try:
            sync_for_user(app, u.id)
        except Exception as e:
            log.error("FiC sync u=%d: %s", u.id, e)
