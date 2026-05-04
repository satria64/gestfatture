"""
Scheduler – gira ogni giorno alle 08:00 e:
  1. Aggiorna lo stato delle fatture (pending → overdue se scadute)
  2. Invia promemoria pre-scadenza
  3. Invia solleciti progressivi post-scadenza
  4. Ricalcola il credit score di tutti i clienti
"""

import logging
from datetime import date, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def _reminder_type_for_days_after(days: int, count: int) -> str:
    """Determina il tipo di sollecito in base ai giorni di ritardo e al contatore."""
    if count == 0:
        return "sollecito_1"
    elif count == 1:
        return "sollecito_2"
    elif count == 2:
        return "sollecito_3"
    else:
        return "diffida"


def run_daily_job(app):
    """Eseguito dallo scheduler; richiede l'app context Flask."""
    with app.app_context():
        from models import db, Invoice, Reminder
        from email_service import send_reminder
        from credit_scoring import update_all_scores
        from config import config

        today = date.today()
        log.info("Scheduler – avvio job giornaliero: %s", today)

        # Itera tutte le fatture pending/overdue di tutti gli utenti.
        # Ogni reminder usa il nome azienda dell'utente proprietario via Invoice.user_id.
        invoices = Invoice.query.filter(Invoice.status.in_(["pending", "overdue"])).all()

        for inv in invoices:
            # Salta le note di credito: non sono da sollecitare
            if inv.is_credit_note:
                continue
            inv.update_status()

            days_until = inv.days_until_due
            days_over  = inv.days_overdue

            # ── Promemoria pre-scadenza ──────────────────────────────────
            if inv.status == "pending" and days_until in config.DAYS_BEFORE_DUE:
                already_sent = any(
                    r.reminder_type == "pre_scadenza"
                    and r.sent_at.date() == today
                    for r in inv.reminders
                )
                if not already_sent:
                    ok = send_reminder(inv, "pre_scadenza")
                    inv.reminder_count += 1
                    inv.last_reminder_date = datetime.utcnow()
                    db.session.add(Reminder(
                        invoice_id=inv.id,
                        reminder_type="pre_scadenza",
                        subject=f"Promemoria fattura {inv.number}",
                        recipient=inv.client.contact_email,
                        success=ok,
                    ))

            # ── Solleciti post-scadenza ──────────────────────────────────
            elif inv.status == "overdue" and days_over in config.DAYS_AFTER_DUE:
                already_sent = any(r.sent_at.date() == today for r in inv.reminders)
                if not already_sent:
                    r_type = _reminder_type_for_days_after(days_over, inv.reminder_count)
                    ok = send_reminder(inv, r_type)
                    inv.reminder_count += 1
                    inv.last_reminder_date = datetime.utcnow()
                    db.session.add(Reminder(
                        invoice_id=inv.id,
                        reminder_type=r_type,
                        subject=f"Sollecito fattura {inv.number}",
                        recipient=inv.client.contact_email,
                        success=ok,
                    ))

        db.session.commit()

        # ── Notifica al PROPRIETARIO le fatture appena diventate scadute ─────
        # (solo quelle non ancora notificate, evita duplicati)
        from models import User
        from notification_service import notify_owner_of_overdue

        newly_overdue = Invoice.query.filter(
            Invoice.status == "overdue",
            Invoice.user_notified_at.is_(None),
            db.or_(Invoice.document_type != "TD04", Invoice.document_type.is_(None)),
        ).all()
        log.info("Scheduler – %d fatture scadute da notificare ai titolari", len(newly_overdue))

        for inv in newly_overdue:
            if inv.is_credit_note:
                continue
            user = User.query.get(inv.user_id)
            if not user:
                continue
            try:
                notify_owner_of_overdue(user, inv)
            except Exception as e:
                log.error("Errore notifica titolare u=%s inv=%s: %s", user.username, inv.number, e)

        # Ricalcola credit score
        update_all_scores()
        log.info("Scheduler – job completato.")


def _wrap_integration_sync(app, module_name: str):
    """Wrapper sicuro che cattura le eccezioni dei job di integrazione."""
    def runner():
        try:
            mod = __import__(module_name)
            mod.sync(app)
        except Exception as e:
            log.error("Errore job %s: %s", module_name, e, exc_info=True)
    return runner


def run_bandi_sync(app):
    """Job giornaliero: scraping bandi + matching per ogni utente reale.
    Chiamato dallo scheduler alle 06:00 (prima del job solleciti).
    """
    with app.app_context():
        from models import db, AppSettings, User
        from bandi_service import sync_all_sources, compute_matches_for_user
        from claude_service import DEFAULT_MODEL

        api_key = AppSettings.get("anthropic_api_key", "")
        if not api_key:
            log.info("Bandi sync saltato: API key Anthropic non configurata")
            return

        scrape_model = AppSettings.get("anthropic_model") or DEFAULT_MODEL
        try:
            stats = sync_all_sources(db, api_key, scrape_model)
            log.info("Bandi sync: nuovi=%d aggiornati=%d errori=%d",
                     stats["new"], stats["updated"], stats["errors"])
        except Exception as e:
            log.error("Bandi scraping globale fallito: %s", e, exc_info=True)
            return

        # Matching per ogni utente reale (non ospite) + notifica digest
        users = User.query.filter(~User.username.like("ospite_%")).all()
        from notification_service import notify_owner_of_new_bandi
        for u in users:
            try:
                n = compute_matches_for_user(
                    db, u, api_key, model="claude-haiku-4-5-20251001"
                )
                log.info("Bandi matching per u=%s: %d match aggiornati", u.username, n)
            except Exception as e:
                log.error("Matching bandi fallito per u=%s: %s", u.username, e)
                continue
            # Digest notifica (email + WhatsApp) per i nuovi match >= 75
            try:
                res = notify_owner_of_new_bandi(u, db, min_score=75)
                if res["count"]:
                    log.info("Bandi digest u=%s: %d nuovi notificati", u.username, res["count"])
            except Exception as e:
                log.error("Notifica digest bandi fallita per u=%s: %s", u.username, e)


def start_scheduler(app):
    global _scheduler
    _scheduler = BackgroundScheduler(timezone="Europe/Rome")

    # Job giornaliero solleciti (08:00)
    _scheduler.add_job(
        func=run_daily_job,
        args=[app],
        trigger=CronTrigger(hour=8, minute=0),
        id="daily_reminders",
        replace_existing=True,
    )

    # Folder watcher ogni 30 secondi
    _scheduler.add_job(
        func=_wrap_integration_sync(app, "integration_folder"),
        trigger=IntervalTrigger(seconds=30),
        id="integration_folder",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # PEC IMAP ogni 5 minuti
    _scheduler.add_job(
        func=_wrap_integration_sync(app, "integration_pec"),
        trigger=IntervalTrigger(minutes=5),
        id="integration_pec",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Fatture in Cloud ogni 30 minuti
    _scheduler.add_job(
        func=_wrap_integration_sync(app, "integration_fic"),
        trigger=IntervalTrigger(minutes=30),
        id="integration_fic",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Riconciliazione bancaria giornaliera alle 07:00
    def _bank_runner():
        try:
            with app.app_context():
                from models import db, User, BankAccount, BankTransaction, Invoice
                from bank_service import sync_all_accounts_for_user, auto_reconcile_user
                from notification_service import notify_owner_of_bank_reconciliation
                from datetime import datetime as _dt, timedelta as _td

                users = (User.query
                         .filter(~User.username.like("ospite_%"))
                         .join(BankAccount, BankAccount.user_id == User.id)
                         .distinct().all())
                for u in users:
                    try:
                        sync_stats = sync_all_accounts_for_user(db, u.id, days_back=14)
                        # Snapshot tx prima della reconciliation per il digest
                        cutoff = _dt.utcnow() - _td(hours=24)
                        recon_stats = auto_reconcile_user(db, u.id)
                        # Costruisci la lista (tx, invoice) per il digest
                        recently_matched = (BankTransaction.query
                                            .filter_by(user_id=u.id, status="auto_matched")
                                            .filter(BankTransaction.matched_at >= cutoff)
                                            .all())
                        auto_pairs = [(tx, tx.matched_invoice) for tx in recently_matched if tx.matched_invoice]
                        pending = (BankTransaction.query
                                   .filter_by(user_id=u.id, status="pending")
                                   .filter(BankTransaction.amount > 0).count())
                        notify_owner_of_bank_reconciliation(u, db, auto_pairs, pending)
                        log.info("Bank u=%s: sync=%s recon=%s pending=%d",
                                 u.username, sync_stats, recon_stats, pending)
                    except Exception as e:
                        log.error("Bank job u=%s fallito: %s", u.username, e)
        except Exception as e:
            log.error("Bank job globale fallito: %s", e, exc_info=True)
    _scheduler.add_job(
        func=_bank_runner,
        trigger=CronTrigger(hour=7, minute=0),
        id="bank_reconciliation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Bandi sync giornaliero alle 06:00 (prima del job solleciti)
    _scheduler.add_job(
        func=run_bandi_sync,
        args=[app],
        trigger=CronTrigger(hour=6, minute=0),
        id="bandi_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Backup S3 settimanale (lunedi' alle 03:00)
    def _backup_runner():
        try:
            from backup_service import run_backup
            r = run_backup(app)
            log.info("Backup settimanale: %s", r)
        except Exception as e:
            log.error("Backup settimanale fallito: %s", e, exc_info=True)
    _scheduler.add_job(
        func=_backup_runner,
        trigger=CronTrigger(day_of_week="mon", hour=3, minute=0),
        id="backup_weekly",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    _scheduler.start()
    log.info("Scheduler avviato – solleciti 08:00, bank 07:00, bandi 06:00, backup mon 03:00, integrazioni")
    return _scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
