"""
Credit Scoring – calcola un punteggio 0–100 per ogni cliente.

Criteri:
  - 40 pt  → percentuale fatture pagate puntualmente
  - 30 pt  → ritardo medio ponderato (meno ritardo = punteggio più alto)
  - 20 pt  → numero di fatture con diffida (sollecito_3 / diffida)
  - 10 pt  → rapporto importo scaduto / importo totale fatturato
"""

from datetime import date


def compute_score(client) -> float:
    # Considera solo le fatture vere (TD01/TD05) — escludi note di credito
    invoices = [i for i in client.invoices if i.document_type != "TD04"]
    if not invoices:
        return 100.0

    total = len(invoices)
    paid  = [i for i in invoices if i.status == "paid"]
    overdue = [i for i in invoices if i.status == "overdue"]

    # --- Componente 1: tasso di pagamento puntuale (max 40 pt) ---
    on_time = 0
    for inv in paid:
        if inv.payment_date and inv.payment_date <= inv.due_date:
            on_time += 1
    score_paid = (on_time / total) * 40

    # --- Componente 2: ritardo medio (max 30 pt) ---
    delays = []
    for inv in paid:
        if inv.payment_date and inv.payment_date > inv.due_date:
            delays.append((inv.payment_date - inv.due_date).days)
    for inv in overdue:
        delays.append((date.today() - inv.due_date).days)

    if delays:
        avg_delay = sum(delays) / len(delays)
        # 0 giorni → 30 pt, ≥60 giorni → 0 pt
        score_delay = max(0, 30 - (avg_delay / 60) * 30)
    else:
        score_delay = 30

    # --- Componente 3: diffide ricevute (max 20 pt) ---
    diffide = 0
    for inv in invoices:
        for rem in inv.reminders:
            if rem.reminder_type in ("sollecito_3", "diffida") and rem.success:
                diffide += 1
                break
    # ogni diffida toglie 5 pt, min 0
    score_diffide = max(0, 20 - diffide * 5)

    # --- Componente 4: importo scaduto / totale fatturato (max 10 pt) ---
    tot_amount     = sum(i.amount for i in invoices) or 1
    overdue_amount = sum(i.amount for i in overdue)
    ratio          = overdue_amount / tot_amount
    score_ratio    = (1 - ratio) * 10

    final = round(score_paid + score_delay + score_diffide + score_ratio, 1)
    return max(0.0, min(100.0, final))


def update_all_scores(db_session_or_app=None):
    """Ricalcola il credit score per tutti i clienti. Da chiamare con app context Flask."""
    from models import Client, db
    clients = Client.query.all()
    for c in clients:
        c.credit_score = compute_score(c)
    db.session.commit()
