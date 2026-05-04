"""Integrazione GoCardless Bank Account Data (PSD2) per riconciliazione bancaria.

Gratis fino a 50.000 transazioni/mese. Copre 2.300+ banche EU incluse italiane.
Documentazione: https://bankaccountdata.gocardless.com/api/docs

Flow:
  1. Admin imposta gocardless_secret_id + gocardless_secret_key in AppSettings
  2. Utente sceglie la propria banca dalla lista italiana
  3. App crea EUA + Requisition → ottiene URL OAuth
  4. Utente apre URL, autorizza login banca, viene rediretto al callback
  5. App fetch /requisitions/{id}/accounts → salva ogni conto come BankAccount
  6. Job giornaliero: per ogni BankAccount, scarica transazioni nuove
  7. Auto-match con fatture aperte; transazioni dubbie in coda manuale
"""
import json
import logging
import re
from datetime import datetime, date, timedelta

import requests

log = logging.getLogger(__name__)

GC_BASE_URL = "https://bankaccountdata.gocardless.com/api/v2"
HTTP_TIMEOUT = 25
ACCESS_VALID_DAYS = 90       # massimo PSD2
MAX_HISTORICAL_DAYS = 90     # quanto indietro possiamo scaricare


# ─── Token management (cached) ────────────────────────────────────────────
_TOKEN_CACHE = {"access": None, "expires_at": None}


def _get_credentials() -> tuple[str, str]:
    from models import AppSettings
    return (
        AppSettings.get("gocardless_secret_id", "").strip(),
        AppSettings.get("gocardless_secret_key", "").strip(),
    )


def _get_access_token() -> str:
    """Restituisce un access token valido, rifrescandolo se necessario."""
    now = datetime.utcnow()
    cached = _TOKEN_CACHE.get("access")
    expires = _TOKEN_CACHE.get("expires_at")
    if cached and expires and now < expires - timedelta(minutes=5):
        return cached

    sid, skey = _get_credentials()
    if not sid or not skey:
        raise RuntimeError("GoCardless: secret_id/secret_key non configurati in admin")

    r = requests.post(
        f"{GC_BASE_URL}/token/new/",
        json={"secret_id": sid, "secret_key": skey},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"GoCardless token error {r.status_code}: {r.text[:300]}")
    data = r.json()
    _TOKEN_CACHE["access"] = data["access"]
    _TOKEN_CACHE["expires_at"] = now + timedelta(seconds=int(data.get("access_expires", 86400)))
    return data["access"]


def _gc_get(path: str) -> dict | list:
    token = _get_access_token()
    r = requests.get(f"{GC_BASE_URL}{path}",
                     headers={"Authorization": f"Bearer {token}",
                              "Accept": "application/json"},
                     timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"GoCardless GET {path} → {r.status_code}: {r.text[:300]}")
    return r.json()


def _gc_post(path: str, body: dict) -> dict:
    token = _get_access_token()
    r = requests.post(f"{GC_BASE_URL}{path}",
                      json=body,
                      headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/json"},
                      timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"GoCardless POST {path} → {r.status_code}: {r.text[:300]}")
    return r.json()


def _gc_delete(path: str) -> bool:
    token = _get_access_token()
    r = requests.delete(f"{GC_BASE_URL}{path}",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=HTTP_TIMEOUT)
    return r.status_code < 400


# ─── Public API ───────────────────────────────────────────────────────────
def list_italian_banks() -> list[dict]:
    """Restituisce la lista banche italiane disponibili su GoCardless.
    Ogni dict ha: id, name, bic, logo, transaction_total_days."""
    return _gc_get("/institutions/?country=it")


def create_connection(user_id: int, institution_id: str, redirect_url: str) -> dict:
    """Crea EUA + Requisition. Restituisce dict con `link` (OAuth URL) e
    `id` (requisition_id da salvare per il callback)."""
    # Step 1: End User Agreement (specifica scope + durata)
    eua = _gc_post("/agreements/enduser/", {
        "institution_id": institution_id,
        "max_historical_days": MAX_HISTORICAL_DAYS,
        "access_valid_for_days": ACCESS_VALID_DAYS,
        "access_scope": ["balances", "details", "transactions"],
    })
    # Step 2: Requisition (genera link OAuth)
    req = _gc_post("/requisitions/", {
        "redirect": redirect_url,
        "institution_id": institution_id,
        "agreement": eua["id"],
        "reference": f"gestfatture-u{user_id}-{int(datetime.utcnow().timestamp())}",
        "user_language": "IT",
    })
    return {"id": req["id"], "link": req["link"]}


def fetch_requisition_accounts(requisition_id: str) -> dict:
    """Dopo il callback, recupera la lista accounts collegati alla requisition."""
    return _gc_get(f"/requisitions/{requisition_id}/")


def fetch_account_details(account_id: str) -> dict:
    return _gc_get(f"/accounts/{account_id}/details/")


def fetch_transactions(account_id: str, date_from: date | None = None) -> dict:
    """Restituisce dict con `booked` e `pending` lists di transazioni."""
    qs = ""
    if date_from:
        qs = f"?date_from={date_from.isoformat()}"
    data = _gc_get(f"/accounts/{account_id}/transactions/{qs}")
    return data.get("transactions", {"booked": [], "pending": []})


# ─── Sync transactions in DB ─────────────────────────────────────────────
def _parse_date(s: str | None):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def upsert_transaction(db, bank_account, tx_data: dict) -> "BankTransaction | None":
    """Inserisce o aggiorna una transazione. Restituisce (BankTransaction, was_new) o None."""
    from models import BankTransaction
    ext_id = tx_data.get("transactionId") or tx_data.get("internalTransactionId") or ""
    if not ext_id:
        # Fallback: hash dei campi principali
        ext_id = f"hash-{tx_data.get('bookingDate','')}-{tx_data.get('transactionAmount',{}).get('amount','')}-{(tx_data.get('remittanceInformationUnstructured','') or '')[:40]}"
    ext_id = ext_id[:120]

    existing = BankTransaction.query.filter_by(
        bank_account_id=bank_account.id, external_id=ext_id
    ).first()
    if existing:
        return existing

    amt_raw = tx_data.get("transactionAmount", {})
    try:
        amount = float(amt_raw.get("amount", 0))
    except Exception:
        amount = 0.0

    debtor = tx_data.get("debtorName") or tx_data.get("creditorName") or ""
    debtor_iban = (tx_data.get("debtorAccount", {}) or {}).get("iban") or \
                  (tx_data.get("creditorAccount", {}) or {}).get("iban") or ""
    desc = (tx_data.get("remittanceInformationUnstructured") or "")
    if not desc:
        # Concatenare structured se presente
        struct = tx_data.get("remittanceInformationStructured") or ""
        desc = struct or (tx_data.get("additionalInformation") or "")

    tx = BankTransaction(
        bank_account_id=bank_account.id,
        user_id=bank_account.user_id,
        external_id=ext_id,
        booking_date=_parse_date(tx_data.get("bookingDate")),
        value_date=_parse_date(tx_data.get("valueDate")),
        amount=amount,
        currency=amt_raw.get("currency", "EUR"),
        debtor_name=str(debtor)[:200],
        debtor_iban=str(debtor_iban)[:40],
        description=str(desc)[:5000],
        raw_data=json.dumps(tx_data, ensure_ascii=False)[:8000],
        status="non_invoice" if amount <= 0 else "pending",
    )
    db.session.add(tx)
    return tx


def sync_account(db, bank_account, days_back: int = 30) -> dict:
    """Scarica transazioni nuove da GoCardless per un singolo account.
    Restituisce dict con stats."""
    from models import BankAccount
    stats = {"new": 0, "errors": 0}
    try:
        # Sync incrementale: dal last_sync_at o dagli ultimi N giorni
        date_from = (date.today() - timedelta(days=days_back))
        if bank_account.last_sync_at:
            date_from = max(date_from, bank_account.last_sync_at.date() - timedelta(days=2))
        data = fetch_transactions(bank_account.external_account_id, date_from=date_from)
        booked = data.get("booked", []) or []
        for tx in booked:
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
        # Se 401/403 → expired/revoked
        if "401" in str(e) or "403" in str(e):
            bank_account.status = "expired"
        else:
            bank_account.status = "error"
        db.session.commit()
        stats["errors"] += 1
    return stats


def sync_all_accounts_for_user(db, user_id: int, days_back: int = 30) -> dict:
    """Sync di tutti gli account collegati di un utente."""
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


# ─── Auto-reconciliation: tentative match transazione ↔ fattura ─────────
def _norm_text(s: str) -> str:
    """Lowercase + remove accents + strip punctuation."""
    if not s:
        return ""
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower())


def _amount_close(a: float, b: float, tolerance: float = 0.01) -> bool:
    return abs(a - b) <= tolerance


def find_matches_for_transaction(tx, candidates) -> list[tuple["Invoice", int, str]]:
    """Per una transazione e una lista di Invoice candidate, restituisce
    [(invoice, score, reason)] ordinata per score decrescente.
    Score 0-100. >=80 considerato match certo per auto-reconcile."""
    desc_norm = _norm_text(tx.description or "")
    debtor_norm = _norm_text(tx.debtor_name or "")
    results = []
    for inv in candidates:
        if not _amount_close(tx.amount, inv.amount):
            continue  # importo diverso → non considerare
        score = 30  # base: importo combacia
        reasons = ["importo combacia"]

        # Numero fattura nella descrizione (forte segnale)
        num_norm = _norm_text(inv.number or "")
        if num_norm and len(num_norm) >= 2 and num_norm in desc_norm:
            score += 50
            reasons.append(f"numero fattura '{inv.number}' nella causale")

        # Nome cliente nella descrizione o nel debtor_name
        client_norm = _norm_text(inv.client.name) if inv.client else ""
        if client_norm and len(client_norm) >= 4:
            # tokenize: se almeno 2 parole del client name compaiono in debtor o desc
            tokens = [t for t in client_norm.split() if len(t) >= 4]
            hits_debtor = sum(1 for t in tokens if t in debtor_norm)
            hits_desc   = sum(1 for t in tokens if t in desc_norm)
            if hits_debtor >= 1 or hits_desc >= 2:
                score += 20
                reasons.append("nome cliente coincide")

        # P.IVA cliente nella descrizione
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
    """Per ogni transazione pending dell'utente, prova match con le sue fatture
    pending/overdue. Se trova UN match con score >= threshold, riconcilia.
    Restituisce stats."""
    from models import BankTransaction, Invoice
    stats = {"auto_matched": 0, "left_pending": 0, "negative_or_outflow": 0}

    # Solo entrate (amount > 0) ancora pending
    pending_tx = BankTransaction.query.filter(
        BankTransaction.user_id == user_id,
        BankTransaction.status == "pending",
        BankTransaction.amount > 0,
    ).all()

    if not pending_tx:
        return stats

    # Fatture aperte dell'utente (non pagate, non NC, non annullate)
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
        # Auto-match solo se UN candidato chiaro (top score molto > 2°)
        is_unique = (len(matches) == 1) or (top_score - matches[1][1] >= 20)
        if top_score >= score_threshold and is_unique:
            tx.matched_invoice_id = top_inv.id
            tx.status = "auto_matched"
            tx.match_confidence = top_score
            tx.match_reason = reason
            tx.matched_at = datetime.utcnow()
            # Marca la fattura come pagata
            top_inv.status = "paid"
            top_inv.payment_date = tx.booking_date or date.today()
            top_inv.payment_ref = f"bank:{tx.external_id[:60]}"
            stats["auto_matched"] += 1
            # Rimuovi dall'elenco per non doppio-match
            try:
                open_invoices.remove(top_inv)
            except ValueError:
                pass
        else:
            stats["left_pending"] += 1

    db.session.commit()
    return stats


def disconnect_account(db, bank_account) -> bool:
    """Cancella la requisition su GoCardless e marca l'account disabilitato.
    Mantiene le transazioni storiche per audit/contabilità."""
    try:
        if bank_account.requisition_id:
            _gc_delete(f"/requisitions/{bank_account.requisition_id}/")
    except Exception as e:
        log.warning("Errore disconnect requisition: %s", e)
    bank_account.status = "disabled"
    db.session.commit()
    return True
