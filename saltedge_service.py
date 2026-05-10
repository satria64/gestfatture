"""Integrazione Salt Edge AIS (Account Information Service) PSD2.

Salt Edge v6 API: copre 5.000+ banche EU incluse tutte le italiane.
Pending mode (dev): 10 connessioni con fake banks, gratis.
Test mode: 100 connessioni con banche vere, gratis (richiede approvazione).
Live mode: produzione, ~€99/mese Pay as You Go.

Flow:
  1. Admin configura saltedge_app_id + saltedge_app_secret in AppSettings
  2. Per utente GestFatture: creiamo (o riusiamo) un Customer Salt Edge
  3. Creiamo una Connect Session → URL del Connect Widget
  4. Utente: redirect → sceglie banca → autorizza (SCA) → callback con connection_id
  5. Sync accounts/transactions per quel connection_id
  6. Refresh: PUT /connections/{id}/refresh (o interactive_refresh per SCA)
  7. Disconnect: DELETE /connections/{id}/remove

Documentazione: https://docs.saltedge.com/account_information/v6/

Espone la stessa interfaccia di bank_service.py (Tink) per backward compatibility.
"""
import json
import logging
import re
import unicodedata
from datetime import datetime, date, timedelta

import requests

log = logging.getLogger(__name__)

SALTEDGE_API_URL = "https://www.saltedge.com/api/v6"
HTTP_TIMEOUT = 25


# ─── Credenziali admin ────────────────────────────────────────────────────
def _get_credentials() -> tuple[str, str]:
    from models import AppSettings
    return (
        AppSettings.get("saltedge_app_id", "").strip(),
        AppSettings.get("saltedge_app_secret", "").strip(),
    )


def _headers() -> dict:
    app_id, secret = _get_credentials()
    if not app_id or not secret:
        raise RuntimeError("Salt Edge: app_id/secret non configurati in admin")
    return {
        "App-id": app_id,
        "Secret": secret,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _post(path: str, payload: dict) -> dict:
    r = requests.post(f"{SALTEDGE_API_URL}{path}",
                      json=payload, headers=_headers(), timeout=HTTP_TIMEOUT)
    log.info("Salt Edge POST %s → %d", path, r.status_code)
    if r.status_code >= 400:
        log.error("Salt Edge POST %s body: %s", path, r.text[:1000])
        raise RuntimeError(f"Salt Edge POST {path} → {r.status_code}: {r.text[:500]}")
    return r.json()


def _get(path: str, params: dict | None = None) -> dict:
    r = requests.get(f"{SALTEDGE_API_URL}{path}",
                     params=params or {}, headers=_headers(), timeout=HTTP_TIMEOUT)
    log.info("Salt Edge GET %s → %d", path, r.status_code)
    if r.status_code >= 400:
        log.error("Salt Edge GET %s body: %s", path, r.text[:1000])
        raise RuntimeError(f"Salt Edge GET {path} → {r.status_code}: {r.text[:500]}")
    return r.json()


def _put(path: str, payload: dict | None = None) -> dict:
    r = requests.put(f"{SALTEDGE_API_URL}{path}",
                     json=payload or {}, headers=_headers(), timeout=HTTP_TIMEOUT)
    log.info("Salt Edge PUT %s → %d", path, r.status_code)
    if r.status_code >= 400:
        log.error("Salt Edge PUT %s body: %s", path, r.text[:1000])
        raise RuntimeError(f"Salt Edge PUT {path} → {r.status_code}: {r.text[:500]}")
    return r.json()


def _delete(path: str) -> dict:
    r = requests.delete(f"{SALTEDGE_API_URL}{path}",
                        headers=_headers(), timeout=HTTP_TIMEOUT)
    log.info("Salt Edge DELETE %s → %d", path, r.status_code)
    if r.status_code >= 400:
        log.error("Salt Edge DELETE %s body: %s", path, r.text[:1000])
        raise RuntimeError(f"Salt Edge DELETE {path} → {r.status_code}: {r.text[:500]}")
    return r.json() if r.text else {}


# ─── Customer management ──────────────────────────────────────────────────
def _is_valid_customer_id(v) -> bool:
    """Salt Edge V6 customer.id è sempre un integer positivo serializzato come stringa.
    Scarta None, "", "None"/"null" letterali e non-numerici (cache inquinata da
    tentativi precedenti)."""
    if v is None:
        return False
    s = str(v).strip()
    if not s or s.lower() in ("none", "null"):
        return False
    return s.isdigit()


def _find_customer_by_identifier(identifier: str) -> str | None:
    """Cerca un customer Salt Edge by identifier iterando la lista paginata.
    V6 NON supporta filtro server-side per identifier, quindi paginiamo.
    Importante: V6 ritorna il campo come `customer_id` (non `id`!) — vedi
    https://docs.saltedge.com/v6/api_reference#customers-list."""
    next_id = None
    for _ in range(20):  # max 20 pagine ~= 20.000 customers (sufficiente)
        params = {"per_page": 1000}
        if next_id:
            params["from_id"] = next_id
        try:
            data = _get("/customers", params=params)
        except Exception as e:
            log.warning("Salt Edge: errore search customers: %s", e)
            return None
        items = data.get("data", []) or []
        for c in items:
            if c.get("identifier") == identifier:
                # V6: campo è customer_id; fallback a id per future compat
                cid = c.get("customer_id") or c.get("id")
                if cid is not None:
                    return str(cid)
        meta = data.get("meta", {}) or {}
        next_id = meta.get("next_id")
        if not next_id:
            break
    return None


def get_or_create_customer(user_id: int) -> str:
    """Restituisce il customer_id Salt Edge per un utente GestFatture.
    Strategia (in ordine):
      1. Cache in UserSetting('saltedge_customer_id') (più veloce)
      2. Riuso da BankAccount.saltedge_customer_id esistente
      3. Search by identifier su Salt Edge (gestisce duplicates da tentativi falliti)
      4. POST /customers (creazione nuova, fallback a search se duplicate)
    Salva il customer_id in UserSetting per riusi futuri."""
    from models import UserSetting, BankAccount
    identifier = f"gestfatture-user-{user_id}"

    # 1. Cache UserSetting (scarta valori non-numerici da tentativi precedenti)
    cached = UserSetting.get(user_id, "saltedge_customer_id")
    if cached:
        if _is_valid_customer_id(cached):
            return str(cached).strip()
        log.warning("Salt Edge: cache UserSetting inquinata per user %d (valore=%r), la ignoro",
                    user_id, cached)
        UserSetting.set(user_id, "saltedge_customer_id", "")

    # 2. Riuso da BankAccount esistente
    existing_ba = (BankAccount.query
                   .filter_by(user_id=user_id)
                   .filter(BankAccount.saltedge_customer_id != "")
                   .first())
    if existing_ba and _is_valid_customer_id(existing_ba.saltedge_customer_id):
        cid = str(existing_ba.saltedge_customer_id).strip()
        log.info("Salt Edge: customer_id da BankAccount %s per user %d", cid, user_id)
        UserSetting.set(user_id, "saltedge_customer_id", cid)
        return cid

    # 3. Search by identifier (recupera customers creati in tentativi precedenti)
    found = _find_customer_by_identifier(identifier)
    if found and _is_valid_customer_id(found):
        log.info("Salt Edge: customer esistente %s recuperato via search per user %d",
                 found, user_id)
        UserSetting.set(user_id, "saltedge_customer_id", found)
        return found

    # 4. Crea nuovo
    try:
        data = _post("/customers", {"data": {"identifier": identifier}})
        # V6: response ha customer_id (non id); fallback a id per safety
        d = data.get("data", {}) or {}
        customer_id = d.get("customer_id") or d.get("id")
        if _is_valid_customer_id(customer_id):
            cid = str(customer_id).strip()
            log.info("Salt Edge: creato customer %s per user %d", cid, user_id)
            UserSetting.set(user_id, "saltedge_customer_id", cid)
            return cid
        raise RuntimeError(f"Salt Edge: response /customers senza id valido: {data}")
    except RuntimeError as e:
        msg = str(e).lower()
        # Race condition: customer creato tra search e POST
        if "duplicate" in msg or "already" in msg or "duplicatedcustomer" in msg:
            found = _find_customer_by_identifier(identifier)
            if found and _is_valid_customer_id(found):
                UserSetting.set(user_id, "saltedge_customer_id", found)
                return found
        raise RuntimeError(f"Salt Edge customers API failed: {e}")


# ─── Connessione: Connect Session ─────────────────────────────────────────
def build_link_url(redirect_url: str, state: str, market: str = "IT",
                   locale: str = "it_IT", user_id: int | None = None) -> str:
    """Genera l'URL Salt Edge Connect Widget per il flow PSD2.
    V6 schema: customer_id è integer; scopes sono accounts/holder_info/transactions."""
    if user_id is None:
        raise RuntimeError("Salt Edge build_link_url: user_id obbligatorio")
    customer_id = get_or_create_customer(user_id)
    if not customer_id:
        raise RuntimeError(f"Salt Edge: customer_id non disponibile per user {user_id}")
    # V6 richiede customer_id come integer
    try:
        customer_id_int = int(customer_id)
    except (TypeError, ValueError):
        raise RuntimeError(f"Salt Edge: customer_id non numerico: {customer_id!r}")

    payload = {
        "data": {
            "customer_id": customer_id_int,
            "consent": {
                # V6 scopes: accounts, holder_info, transactions (non i nomi V5)
                "scopes": ["accounts", "transactions"],
                "from_date": (date.today() - timedelta(days=90)).isoformat(),
            },
            "attempt": {
                "return_to": redirect_url,
                "fetch_scopes": ["accounts", "transactions"],
                "custom_fields": {"state": state},
                "locale": locale[:2],  # Salt Edge usa "it", "en"...
            },
            "country_code": (market or "IT").upper(),
        }
    }
    # V6: /connections/connect (rinominato da v5 /connect_sessions/create)
    data = _post("/connections/connect", payload)
    connect_url = data.get("data", {}).get("connect_url") or \
                  data.get("data", {}).get("redirect_url")
    if not connect_url:
        raise RuntimeError(f"Salt Edge: connect_url mancante in response: {data}")
    return connect_url


def exchange_code(code: str, redirect_url: str) -> dict:
    """Salt Edge NON usa exchange code: il callback include direttamente connection_id.
    Manteniamo questa funzione per compat. ma ritorniamo dict vuoto.
    Il flusso reale è gestito in bank_callback() che riceve connection_id da query."""
    return {}


def refresh_user_token(refresh_token: str) -> dict:
    """Salt Edge non usa refresh token utente lato app.
    Per refresh dei dati: PUT /connections/{id}/refresh.
    Manteniamo come no-op per compat."""
    return {}


# ─── Accounts & Transactions ──────────────────────────────────────────────
def list_user_accounts_for_connection(connection_id: str) -> list[dict]:
    """Lista accounts collegati a una connection Salt Edge."""
    data = _get("/accounts", params={"connection_id": connection_id})
    return data.get("data", []) or []


def list_user_accounts(user_token_or_connection_id: str) -> list[dict]:
    """Compat. con bank_service.py (Tink): primo arg era access_token.
    In Salt Edge è il connection_id."""
    return list_user_accounts_for_connection(user_token_or_connection_id)


def list_transactions(user_token_or_connection_id: str, account_id: str,
                      date_from: date | None = None) -> list[dict]:
    """Lista transactions di un account Salt Edge.
    Salt Edge paginate fino a 1000 per default."""
    params = {
        "connection_id": user_token_or_connection_id,
        "account_id": account_id,
    }
    if date_from:
        params["from_date"] = date_from.isoformat()
    all_tx = []
    next_id = None
    for _ in range(10):  # max 10 pagine = 10000 tx, abbondante per sync incrementale
        if next_id:
            params["from_id"] = next_id
        data = _get("/transactions", params=params)
        items = data.get("data", []) or []
        all_tx.extend(items)
        meta = data.get("meta", {}) or {}
        next_id = meta.get("next_id")
        if not next_id or not items:
            break
    return all_tx


def list_connections_for_customer(customer_id: str) -> list[dict]:
    data = _get("/connections", params={"customer_id": customer_id})
    return data.get("data", []) or []


def refresh_connection(connection_id: str) -> dict:
    """Triggera un refresh non-interattivo della connection (entro 90gg da SCA).
    V6: POST (non più PUT)."""
    return _post(f"/connections/{connection_id}/refresh", {})


# ─── Sync transactions in DB ─────────────────────────────────────────────
def _ensure_fresh_token(bank_account) -> str:
    """Compat. shim: in Salt Edge non c'è token utente, ritorniamo il connection_id
    salvato in `requisition_id` (riusato come connection_id per evitare migrazioni)."""
    conn_id = (bank_account.requisition_id or "").strip()
    if not conn_id:
        raise RuntimeError("Salt Edge: connection_id mancante (l'utente deve ricollegare la banca)")
    return conn_id


def _parse_date(s: str | None):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def upsert_transaction(db, bank_account, tx_data: dict):
    """Inserisce una transazione Salt Edge. Schema V6:
    - id / transaction_id (str)
    - account_id (str)
    - amount (float)
    - currency_code (str)
    - made_on (date booking)
    - description (str)
    - extra: { payee, payer_name, payer_iban, account_number, ... }
    - status: posted / pending
    """
    from models import BankTransaction
    ext_id = str(tx_data.get("id") or tx_data.get("transaction_id") or "")[:120]
    if not ext_id:
        return None
    existing = BankTransaction.query.filter_by(
        bank_account_id=bank_account.id, external_id=ext_id
    ).first()
    if existing:
        return existing

    try:
        amount = float(tx_data.get("amount", 0) or 0)
    except Exception:
        amount = 0.0
    currency = tx_data.get("currency_code", "EUR")

    description = tx_data.get("description", "") or ""

    extra = tx_data.get("extra", {}) or {}
    debtor_name = extra.get("payer_name") or extra.get("payee") or ""
    debtor_iban = extra.get("account_number") or extra.get("payer_iban") or ""

    booking = _parse_date(tx_data.get("made_on"))
    value_d = booking  # Salt Edge non distingue value date in modo standard

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
        connection_id = _ensure_fresh_token(bank_account)
        # Aggiorna saldo (usato dal cash flow forecast)
        try:
            accs = list_user_accounts_for_connection(connection_id)
            for acc in accs:
                acc_id = acc.get("id") or acc.get("account_id")
                if acc_id is not None and str(acc_id) == str(bank_account.external_account_id):
                    bal = acc.get("balance")
                    if bal is not None:
                        try:
                            bank_account.last_balance = float(bal)
                            bank_account.last_balance_at = datetime.utcnow()
                        except Exception:
                            pass
                    break
        except Exception as e:
            log.warning("Balance fetch fallito acc=%s: %s",
                        bank_account.external_account_id, e)

        date_from = (date.today() - timedelta(days=days_back))
        if bank_account.last_sync_at:
            date_from = max(date_from, bank_account.last_sync_at.date() - timedelta(days=2))
        txs = list_transactions(connection_id, bank_account.external_account_id, date_from)
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
        if "401" in str(e) or "403" in str(e) or "Inactive" in str(e) or "Expired" in str(e):
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


# ─── Auto-reconciliation: match tx ↔ fattura (logica invariata da Tink) ──
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
    """Cancella la connection lato Salt Edge + scarta token locali.
    V6: DELETE /connections/{id} (senza /remove suffix)."""
    conn_id = (bank_account.requisition_id or "").strip()
    if conn_id:
        try:
            _delete(f"/connections/{conn_id}")
        except Exception as e:
            log.warning("Salt Edge disconnect fallito (continuo lato locale): %s", e)
    bank_account.status = "disabled"
    bank_account.access_token = ""
    bank_account.refresh_token = ""
    bank_account.requisition_id = ""
    db.session.commit()
    return True
