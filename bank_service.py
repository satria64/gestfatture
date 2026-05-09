"""Bank service: wrapper agnostic provider per riconciliazione bancaria PSD2.

Sostituisce l'integrazione Tink con Salt Edge AIS (più economica per startup).
Tutte le funzioni delegano a saltedge_service.py per mantenere la backward
compatibility con le route esistenti in app.py (build_link_url, sync_account,
auto_reconcile_user, ecc.).

Backup del codice Tink originale: bank_service_tink_legacy.py
"""
from saltedge_service import (
    # Connessione
    build_link_url,
    exchange_code,           # no-op con Salt Edge
    refresh_user_token,      # no-op con Salt Edge
    get_or_create_customer,  # nuovo, specifico Salt Edge

    # Accounts & transactions
    list_user_accounts,
    list_transactions,
    list_user_accounts_for_connection,
    list_connections_for_customer,
    refresh_connection,

    # Sync
    upsert_transaction,
    sync_account,
    sync_all_accounts_for_user,

    # Reconciliation (logica pura, invariata da Tink)
    find_matches_for_transaction,
    auto_reconcile_user,

    # Disconnect
    disconnect_account,
)

# Compat: alcune route Tink usavano questi nomi diretti
__all__ = [
    "build_link_url", "exchange_code", "refresh_user_token",
    "get_or_create_customer",
    "list_user_accounts", "list_transactions",
    "list_user_accounts_for_connection", "list_connections_for_customer",
    "refresh_connection",
    "upsert_transaction", "sync_account", "sync_all_accounts_for_user",
    "find_matches_for_transaction", "auto_reconcile_user",
    "disconnect_account",
]
