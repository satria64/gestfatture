"""Integrazione Tink (Visa) per riconciliazione bancaria PSD2.

Tink Money Aggregation API: copre 6.000+ banche EU incluse tutte le italiane.
Free tier per sviluppatori, poi a consumo (~€0.10/conto/mese in produzione).

Flow OAuth (Tink Link):
  1. Admin imposta tink_client_id + tink_client_secret in AppSettings
  2. Utente: redirect a Tink Link → sceglie banca → autorizza (SCA)
  3. Callback con `?code=...` → exchange per access_token + refresh_token (90gg)
  4. Fetch accounts → salva BankAccount con tokens
  5. Sync transazioni con access_token (refresh on-demand)

Documentazione: https://docs.tink.com/api
"""
import json
import logging
import re
import secrets
import unicodedata
from datetime import datetime, date, timedelta

import requests

log = logging.getLogger(__name__)

TINK_API_URL  = "https://api.tink.com"
TINK_LINK_URL = "https://link.tink.com/1.0/transactions/connect-accounts"
HTTP_TIMEOUT  = 25
ACCESS_VALID_DAYS = 90  # massimo PSD2


# ─── Credenziali admin + token app-level ─────────────────────────────────
def _get_credentials() -> tuple[str, str]:
    from models import AppSettings
    return (
        AppSettings.get("tink_client_id", "").strip(),
        AppSettings.get("tink_client_secret", "").strip(),
    )


def _post_form(path: str, data: dict, auth_token: str | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    r = requests.post(f"{TINK_API_URL}{path}", data=data,
                      headers=headers, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"Tink POST {path} → {r.status_code}: {r.text[:300]}")
    return r.json()


def _get_json(path: str, token: str) -> dict:
    r = requests.get(f"{TINK_API_URL}{path}",
                     headers={"Authorization": f"Bearer {token}",
                              "Accept": "application/json"},
                     timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"Tink GET {path} → {r.status_code}: {r.text[:300]}")
    return r.json()


def _get_app_token(scope: str) -> str:
    """Token app-level (client_credentials) per chiamate amministrative."""
    cid, csec = _get_credentials()
    if not cid or not csec:
        raise RuntimeError("Tink: client_id/client_secret non configurati in admin")
    data = _post_form("/api/v1/oauth/token", {
        "client_id": cid,
        "client_secret": csec,
        "grant_type": "client_credentials",
        "scope": scope,
    })
    return data["access_token"]


# ─── Connessione: build URL Tink Link ────────────────────────────────────
def build_link_url(redirect_url: str, state: str, market: str = "IT",
                   locale: str = "it_IT") -> str:
    """Genera l'URL Tink Link a cui rediriggere l'utente per il flow PSD2.
    Lo state è anti-CSRF e identifica la sessione."""
    cid, _ = _get_credentials()
    if not cid:
        raise RuntimeError("Tink client_id mancante")
    from urllib.parse import urlencode
    params = {
        "client_id": cid,
        "redirect_uri": redirect_url,
        "market": market,
        "locale": locale,
        "state": state,
        "test": "false",  # set "true" per sandbox banche di test
    }
    return f"{TINK_LINK_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_url: str) -> dict:
    """Scambia il code restituito da Tink Link per access_token + refresh_token utente."""
    cid, csec = _get_credentials()
    return _post_form("/api/v1/oauth/token", {
        "code": code,
        "client_id": cid,
        "client_secret": csec,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_url,
    })


def refresh_user_token(refresh_token: str) -> dict:
    cid, csec = _get_credentials()
    return _post_form("/api/v1/oauth/token", {
        "refresh_token": refresh_token,
        "client_id": cid,
        "client_secret": csec,
        "grant_type": "refresh_token",
    })


def list_user_accounts(user_token: str) -> list[dict]:
    """GET /data/v2/accounts → lista conti collegati di questo utente."""
    data = _get_json("/data/v2/accounts", user_token)
    return data.get("accounts", []) or []


def list_transactions(user_token: str, account_id: str,
                      date_from: date | None = None) -> list[dict]:
    """GET /data/v2/transactions filtrate per account.
    Tink limita pageSize a 200. Se servono piu' transazioni si itera con
    nextPageToken — qui ci limitiamo alla prima pagina (sufficiente per
    sync incrementale di pochi giorni)."""
    qs = f"?accountIdIn={account_id}&pageSize=200"
    if date_from:
        qs += f"&bookedDateGte={date_from.isoformat()}"
    data = _get_json(f"/data/v2/transactions{qs}", user_token)
    return data.get("transactions", []) or []


# ─── Sync transactions in DB ─────────────────────────────────────────────
def _ensure_fresh_token(bank_account) -> str:
    """Restituisce un access_token valido per il bank_account, refreshandolo se necessario."""
    from models import db
    now = datetime.utcnow()
    if bank_account.access_token and bank_account.token_expires_at \
       and bank_account.token_expires_at > now + timedelta(minutes=2):
        return bank_account.access_token
    # Refresh
    if not bank_account.refresh_token:
        raise RuntimeError("Refresh token mancante: l'utente deve ri-autorizzare")
    data = refresh_user_token(bank_account.refresh_token)
    bank_account.access_token = data["access_token"]
    if data.get("refresh_token"):
        bank_account.refresh_token = data["refresh_token"]
    bank_account.token_expires_at = now + timedelta(seconds=int(data.get("expires_in", 3600)))
    db.session.commit()
    return bank_account.access_token


def _parse_date(s: str | None):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def upsert_transaction(db, bank_account, tx_data: dict):
    """Inserisce una transazione Tink. Tink schema:
    - id (str)
    - accountId (str)
    - amount: { currencyCode, value: { scale, unscaledValue } }
    - dates: { booked, value }
    - descriptions: { display, original }
    - counterparties: { payer/payee }
    - status: BOOKED / PENDING
    - reference (str opzionale, spesso contiene il numero fattura)
    """
    from models import BankTransaction
    ext_id = tx_data.get("id", "")[:120]
    if not ext_id:
        return None
    existing = BankTransaction.query.filter_by(
        bank_account_id=bank_account.id, external_id=ext_id
    ).first()
    if existing:
        return existing

    amt_obj = tx_data.get("amount", {})
    val_obj = amt_obj.get("value", {}) or {}
    try:
        unscaled = float(val_obj.get("unscaledValue", 0))
        scale = int(val_obj.get("scale", 2))
        amount = unscaled / (10 ** scale)
    except Exception:
        amount = 0.0
    currency = amt_obj.get("currencyCode", "EUR")

    descs = tx_data.get("descriptions", {}) or {}
    description = (descs.get("original") or descs.get("display") or
                   tx_data.get("reference") or "")

    counter = tx_data.get("counterparties", {}) or {}
    payer = counter.get("payer", {}) or {}
    payee = counter.get("payee", {}) or {}
    debtor_name = payer.get("name") or payee.get("name") or ""
    debtor_iban = ((payer.get("identifiers", {}) or {}).get("iban", {}) or {}).get("iban") or \
                  ((payee.get("identifiers", {}) or {}).get("iban", {}) or {}).get("iban") or ""

    dates = tx_data.get("dates", {}) or {}
    booking = _parse_date(dates.get("booked"))
    value_d = _parse_date(dates.get("value"))

    tx = BankTransaction(
        bank_account_id=bank_account.id,
        user_id=bank_account.user_id,
        external_id=ext_id,
        booking_date=booking,
        value_date=value_d,
        amount=amount,
        currency=currency,
        debtor_name=str(debtor_name)[:200],
        debtor_iban=str(debtor_iban)[:40],
        description=str(description)[:5000],
        raw_data=json.dumps(tx_data, ensure_ascii=False)[:8000],
        status="non_invoice" if amount <= 0 else "pending",
    )
    db.session.add(tx)
    return tx


def sync_account(db, bank_account, days_back: int = 30) -> dict:
    stats = {"new": 0, "errors": 0}
    try:
        token = _ensure_fresh_token(bank_account)
        date_from = (date.today() - timedelta(days=days_back))
        if bank_account.last_sync_at:
            date_from = max(date_from, bank_account.last_sync_at.date() - timedelta(days=2))
        txs = list_transactions(token, bank_account.external_account_id, date_from)
        for tx in txs:
            tx_obj = upsert_transaction(db, bank_account, tx)
            if tx_obj and tx_obj.id is None:
                stats["new"] += 1
        bank_account.last_sync_at = datetime.utcnow()
        bank_account.last_error = ""
        bank_account.status = "linked"
        db.session.commit()
    except Exception as e:
        log.error("Sync bank account u=%d acc=%s fallito: %s",
                  bank_account.user_id, bank_account.external_account_id, e)
        bank_account.last_error = str(e)[:500]
        if "401" in str(e) or "403" in str(e) or "invalid_grant" in str(e).lower():
            bank_account.status = "expired"
        else:
            bank_account.status = "error"
        db.session.commit()
        stats["errors"] += 1
    return stats


def sync_all_accounts_for_user(db, user_id: int, days_back: int = 30) -> dict:
    from models import BankAccount
    stats = {"new": 0, "errors": 0, "accounts": 0}
    accs = BankAccount.query.filter_by(user_id=user_id).filter(
        BankAccount.status.in_(["linked", "error"])
    ).all()
    for acc in accs:
        stats["accounts"] += 1
        s = sync_account(db, acc, days_back=days_back)
        stats["new"] += s["new"]
        stats["errors"] += s["errors"]
    return stats


# ─── Auto-reconciliation: match tx ↔ fattura (invariato vs versione precedente) ──
def _norm_text(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower())


def _amount_close(a: float, b: float, tolerance: float = 0.01) -> bool:
    return abs(a - b) <= tolerance


def find_matches_for_transaction(tx, candidates):
    desc_norm = _norm_text(tx.description or "")
    debtor_norm = _norm_text(tx.debtor_name or "")
    results = []
    for inv in candidates:
        if not _amount_close(tx.amount, inv.amount):
            continue
        score = 30
        reasons = ["importo combacia"]
        num_norm = _norm_text(inv.number or "")
        if num_norm and len(num_norm) >= 2 and num_norm in desc_norm:
            score += 50
            reasons.append(f"numero fattura '{inv.number}' nella causale")
        client_norm = _norm_text(inv.client.name) if inv.client else ""
        if client_norm and len(client_norm) >= 4:
            tokens = [t for t in client_norm.split() if len(t) >= 4]
            hits_debtor = sum(1 for t in tokens if t in debtor_norm)
            hits_desc = sum(1 for t in tokens if t in desc_norm)
            if hits_debtor >= 1 or hits_desc >= 2:
                score += 20
                reasons.append("nome cliente coincide")
        if inv.client and inv.client.vat_number:
            vat = re.sub(r"\D", "", inv.client.vat_number)
            if vat and vat in re.sub(r"\D", "", tx.description or ""):
                score += 30
                reasons.append("P.IVA cliente nella causale")
        score = min(100, score)
        results.append((inv, score, "; ".join(reasons)))
    results.sort(key=lambda x: -x[1])
    return results


def auto_reconcile_user(db, user_id: int, score_threshold: int = 80) -> dict:
    from models import BankTransaction, Invoice
    stats = {"auto_matched": 0, "left_pending": 0, "negative_or_outflow": 0}
    pending_tx = BankTransaction.query.filter(
        BankTransaction.user_id == user_id,
        BankTransaction.status == "pending",
        BankTransaction.amount > 0,
    ).all()
    if not pending_tx:
        return stats
    open_invoices = Invoice.query.filter(
        Invoice.user_id == user_id,
        Invoice.status.in_(["pending", "overdue"]),
        db.or_(Invoice.document_type != "TD04", Invoice.document_type.is_(None)),
    ).all()
    for tx in pending_tx:
        matches = find_matches_for_transaction(tx, open_invoices)
        if not matches:
            stats["left_pending"] += 1
            continue
        top_inv, top_score, reason = matches[0]
        is_unique = (len(matches) == 1) or (top_score - matches[1][1] >= 20)
        if top_score >= score_threshold and is_unique:
            tx.matched_invoice_id = top_inv.id
            tx.status = "auto_matched"
            tx.match_confidence = top_score
            tx.match_reason = reason
            tx.matched_at = datetime.utcnow()
            top_inv.status = "paid"
            top_inv.payment_date = tx.booking_date or date.today()
            top_inv.payment_ref = f"bank:{tx.external_id[:60]}"
            stats["auto_matched"] += 1
            try:
                open_invoices.remove(top_inv)
            except ValueError:
                pass
        else:
            stats["left_pending"] += 1
    db.session.commit()
    return stats


def disconnect_account(db, bank_account) -> bool:
    """Per Tink basta scartare i token lato nostro: GestFatture non può più
    chiamare le API. L'utente può anche revocare l'accesso direttamente
    sul portale della sua banca."""
    bank_account.status = "disabled"
    bank_account.access_token = ""
    bank_account.refresh_token = ""
    db.session.commit()
    return True
