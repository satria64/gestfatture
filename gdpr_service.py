"""GDPR data portability — Art. 20.

Costruisce uno ZIP con tutti i dati personali dell'utente: profilo,
impostazioni, clienti, fatture (con solleciti), ticket di assistenza,
messaggi PEC istituzionali e log di sicurezza. Le credenziali di servizi
terzi (token OAuth, password IMAP, API key) sono redatte per sicurezza.
"""
import io
import json
import os
import zipfile
from datetime import datetime, date

from models import (Client, Invoice, UserSetting, AuditLog,
                    SupportTicket, PecMessage)


SENSITIVE_USER_SETTING_KEYS = {
    "whatsapp_apikey",
    "integration_fic_access_token",
    "integration_fic_refresh_token",
    "integration_fic_client_secret",
    "integration_pec_password",
}

REDACTED = "[REDATTO — visibile in app, non incluso nell'export]"


def _iso(v):
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _user_dict(user):
    return {
        "id":         user.id,
        "username":   user.username,
        "email":      user.email,
        "phone":      user.phone,
        "is_admin":   user.is_admin,
        "created_at": _iso(user.created_at),
    }


def _client_dict(c):
    return {
        "id":           c.id,
        "name":         c.name,
        "email":        c.email,
        "pec":          c.pec,
        "phone":        c.phone,
        "address":      c.address,
        "vat_number":   c.vat_number,
        "credit_score": c.credit_score,
        "created_at":   _iso(c.created_at),
    }


def _reminder_dict(r):
    return {
        "id":            r.id,
        "sent_at":       _iso(r.sent_at),
        "reminder_type": r.reminder_type,
        "subject":       r.subject,
        "recipient":     r.recipient,
        "success":       r.success,
        "error_message": r.error_message,
    }


def _invoice_dict(i):
    return {
        "id":                 i.id,
        "client_id":          i.client_id,
        "client_name":        i.client.name if i.client else None,
        "number":             i.number,
        "amount":             i.amount,
        "issue_date":         _iso(i.issue_date),
        "due_date":           _iso(i.due_date),
        "document_type":      i.document_type,
        "linked_invoice_id":  i.linked_invoice_id,
        "status":             i.status,
        "payment_date":       _iso(i.payment_date),
        "reminder_count":     i.reminder_count,
        "last_reminder_date": _iso(i.last_reminder_date),
        "payment_link":       i.payment_link,
        "payment_ref":        i.payment_ref,
        "pdf_filename":       i.pdf_filename,
        "notes":              i.notes,
        "created_at":         _iso(i.created_at),
        "reminders":          [_reminder_dict(r) for r in (i.reminders or [])],
    }


def _ticket_dict(t):
    return {
        "id":         t.id,
        "subject":    t.subject,
        "status":     t.status,
        "priority":   t.priority,
        "category":   t.category,
        "created_at": _iso(t.created_at),
        "updated_at": _iso(t.updated_at),
        "messages": [
            {
                "id":              m.id,
                "author_id":       m.author_id,
                "author_username": m.author.username if m.author else None,
                "body":            m.body,
                "created_at":      _iso(m.created_at),
            }
            for m in (t.messages or []) if not m.is_internal
        ],
    }


def _pec_dict(p):
    return {
        "id":               p.id,
        "message_id":       p.message_id,
        "received_at":      _iso(p.received_at),
        "sender":           p.sender,
        "sender_label":     p.sender_label,
        "subject":          p.subject,
        "category":         p.category,
        "urgency":          p.urgency,
        "summary":          p.summary,
        "suggested_action": p.suggested_action,
        "deadline":         _iso(p.deadline),
        "key_facts":        p.key_facts_list,
        "body_excerpt":     p.body_excerpt,
        "attachments":      p.attachments_list,
        "is_read":          p.is_read,
        "is_archived":      p.is_archived,
    }


def _audit_dict(a):
    return {
        "timestamp":  _iso(a.timestamp),
        "action":     a.action,
        "target":     a.target,
        "details":    a.details,
        "ip_address": a.ip_address,
        "user_agent": a.user_agent,
    }


def _settings_dict(uid):
    rows = UserSetting.query.filter_by(user_id=uid).all()
    out = {}
    for r in rows:
        if r.key in SENSITIVE_USER_SETTING_KEYS and r.value:
            out[r.key] = REDACTED
        else:
            out[r.key] = r.value
    return out


README_TEMPLATE = """\
GestFatture — Esportazione dati personali (GDPR Art. 20)
==========================================================

Generato il: {generated_at}
Per l'utente: {username} (id: {user_id})

Contenuto dell'archivio
-----------------------
- account.json        Dati del tuo profilo utente
- settings.json       Impostazioni personali e integrazioni
- clients.json        Tuoi clienti (anagrafica completa)
- invoices.json       Tue fatture, con i solleciti inviati
- tickets.json        Ticket di assistenza (incluse risposte pubbliche)
- pec_messages.json   Email PEC istituzionali (AdE/INPS/INAIL) ricevute e analizzate
- audit_log.json      Eventi di sicurezza del tuo account (login, modifiche, ecc.)
- pdfs/               Allegati PDF delle tue fatture (se presenti)

Dati ESCLUSI per sicurezza
---------------------------
Le credenziali di accesso a servizi terzi (password PEC IMAP, token OAuth
Fatture in Cloud, API key WhatsApp/CallMeBot) NON sono esportate. Trovi i
valori in chiaro dentro l'app, in Impostazioni e Mie integrazioni.

Note tecniche
-------------
- I dati sono in formato JSON, leggibili da qualsiasi programma o linguaggio.
- Le date sono in formato ISO 8601 (es. 2026-05-04T10:00:00).
- Gli importi sono in euro come numeri decimali.

Cosa puoi fare con questo archivio
-----------------------------------
- Conservarlo come backup personale dei tuoi dati.
- Migrarlo verso un altro software gestionale (i JSON sono universali).
- Verificare che i dati salvati corrispondano a ciò che ti aspetti.

Se vuoi cancellare definitivamente i tuoi dati dalla nostra app, vai su
Impostazioni -> "Cancella account" (richiede conferma con password).
"""


def build_export_zip(user, upload_folder: str) -> io.BytesIO:
    """Genera in memoria uno ZIP con tutti i dati personali dell'utente."""
    uid = user.id
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", README_TEMPLATE.format(
            generated_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            username=user.username,
            user_id=uid,
        ))
        zf.writestr("account.json",
                    json.dumps(_user_dict(user), ensure_ascii=False, indent=2))
        zf.writestr("settings.json",
                    json.dumps(_settings_dict(uid), ensure_ascii=False, indent=2))

        clients = Client.query.filter_by(user_id=uid).order_by(Client.id).all()
        zf.writestr("clients.json",
                    json.dumps([_client_dict(c) for c in clients],
                               ensure_ascii=False, indent=2))

        invoices = Invoice.query.filter_by(user_id=uid).order_by(Invoice.id).all()
        zf.writestr("invoices.json",
                    json.dumps([_invoice_dict(i) for i in invoices],
                               ensure_ascii=False, indent=2))

        tickets = SupportTicket.query.filter_by(user_id=uid).order_by(SupportTicket.id).all()
        zf.writestr("tickets.json",
                    json.dumps([_ticket_dict(t) for t in tickets],
                               ensure_ascii=False, indent=2))

        pec_msgs = PecMessage.query.filter_by(user_id=uid).order_by(PecMessage.id).all()
        zf.writestr("pec_messages.json",
                    json.dumps([_pec_dict(p) for p in pec_msgs],
                               ensure_ascii=False, indent=2))

        audit_rows = (AuditLog.query.filter_by(user_id=uid)
                      .order_by(AuditLog.timestamp.desc()).limit(5000).all())
        zf.writestr("audit_log.json",
                    json.dumps([_audit_dict(a) for a in audit_rows],
                               ensure_ascii=False, indent=2))

        if upload_folder and os.path.isdir(upload_folder):
            for inv in invoices:
                if not inv.pdf_filename:
                    continue
                src = os.path.join(upload_folder, inv.pdf_filename)
                if os.path.isfile(src):
                    try:
                        zf.write(src, arcname=f"pdfs/{inv.pdf_filename}")
                    except OSError:
                        pass
    buf.seek(0)
    return buf
