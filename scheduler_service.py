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

    _scheduler.start()
    log.info("Scheduler avviato – solleciti 08:00 + integrazioni (folder 30s, PEC 5min, FiC 30min)")
    return _scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
