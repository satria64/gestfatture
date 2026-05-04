"""
Notifiche al PROPRIETARIO della fattura (l'utente di GestFatture, non il cliente).

Canali:
- Email (via SMTP globale)
- WhatsApp via CallMeBot (gratis, https://www.callmebot.com/blog/free-api-whatsapp-messages/)

Setup CallMeBot (una volta sola, per ogni utente):
  1. Aggiungere ai contatti il numero: +34 644 51 95 23
  2. Da WhatsApp scrivere: "I allow callmebot to send me messages"
  3. Si riceve la propria API key personale via WhatsApp
  4. Inserirla nelle Impostazioni di GestFatture
"""

import logging
import smtplib
import requests
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _format_eur(v: float) -> str:
    return f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _invoice_summary(invoice) -> dict:
    return {
        "numero":   invoice.number,
        "cliente":  invoice.client.name,
        "importo":  _format_eur(invoice.amount),
        "scadenza": invoice.due_date.strftime("%d/%m/%Y"),
        "ritardo":  invoice.days_overdue,
    }


def _suggest_next_action(invoice) -> str:
    """Determina il prossimo sollecito logico in base ai precedenti inviati."""
    n = invoice.reminder_count or 0
    if n == 0: return "s1"
    if n == 1: return "s2"
    if n == 2: return "s3"
    return "diffida"


def _build_quick_links(invoice) -> dict:
    """Genera i link delle quick actions per email/WhatsApp."""
    from tokens import make_action_url
    next_act = _suggest_next_action(invoice)
    return {
        "next_action": next_act,
        "next_url":    make_action_url(invoice, next_act),
        "s1":          make_action_url(invoice, "s1"),
        "s2":          make_action_url(invoice, "s2"),
        "s3":          make_action_url(invoice, "s3"),
        "diffida":     make_action_url(invoice, "diffida"),
        "paid":        make_action_url(invoice, "paid"),
        "stop":        make_action_url(invoice, "stop"),
    }


_NEXT_LABELS = {
    "s1":      "📧 Manda 1° Sollecito",
    "s2":      "📧 Manda 2° Sollecito",
    "s3":      "📧 Manda 3° Sollecito",
    "diffida": "⚖️ Manda Diffida formale",
}


# ─── EMAIL al proprietario ────────────────────────────────────────────────────
def send_email_to_owner(user, invoice) -> tuple[bool, str]:
    """Invia email al titolare quando una sua fattura è scaduta."""
    from models import AppSettings, UserSetting

    if not user.email:
        return False, "Email utente non configurata"

    cfg = {
        "host":     AppSettings.get("smtp_host", "smtp.gmail.com"),
        "port":     int(AppSettings.get("smtp_port", "587")),
        "user":     AppSettings.get("smtp_user", ""),
        "password": AppSettings.get("smtp_password", ""),
        "use_tls":  AppSettings.get("smtp_use_tls", "true") == "true",
    }
    if not cfg["user"]:
        return False, "SMTP globale non configurato"

    company_name = (UserSetting.get(user.id, "company_name")
                    or AppSettings.get("company_name", "GestFatture"))

    s = _invoice_summary(invoice)
    L = _build_quick_links(invoice)
    next_label = _NEXT_LABELS.get(L["next_action"], "📧 Manda sollecito")

    subject = f"⚠️ Fattura {s['numero']} scaduta — {s['cliente']}"
    html = f"""<!DOCTYPE html><html lang="it"><head><meta charset="utf-8"><style>
      body{{font-family:Arial,sans-serif;font-size:14px;color:#1f2937;line-height:1.6}}
      .wrap{{max-width:560px;margin:20px auto;padding:24px;border:1px solid #e5e7eb;border-radius:8px}}
      .header{{background:#dc2626;color:#fff;padding:12px 18px;border-radius:6px;margin-bottom:18px}}
      table{{width:100%;border-collapse:collapse;margin:12px 0}}
      td{{padding:8px;border-bottom:1px solid #f3f4f6}}
      .label{{color:#6b7280;width:35%}}
      .actions{{margin:24px 0;padding:18px;background:#f8fafc;border-radius:8px;text-align:center}}
      .btn{{display:inline-block;padding:11px 18px;border-radius:6px;text-decoration:none;
            font-weight:bold;margin:4px;font-size:13px}}
      .btn-next{{background:#2563eb;color:#fff !important;font-size:15px;padding:14px 24px}}
      .btn-paid{{background:#16a34a;color:#fff !important}}
      .btn-other{{background:#fff;color:#475569 !important;border:1px solid #cbd5e1}}
      .footer{{font-size:12px;color:#6b7280;margin-top:24px;text-align:center}}
    </style></head><body><div class="wrap">
      <div class="header"><h2 style="margin:0">⚠️ Fattura scaduta</h2></div>
      <p>Ciao <strong>{user.username}</strong>,</p>
      <p>una tua fattura è <strong>scaduta</strong>:</p>
      <table>
        <tr><td class="label">Numero</td><td><strong>{s['numero']}</strong></td></tr>
        <tr><td class="label">Cliente</td><td><strong>{s['cliente']}</strong></td></tr>
        <tr><td class="label">Importo</td><td><strong style="color:#dc2626">{s['importo']}</strong></td></tr>
        <tr><td class="label">Scadenza</td><td>{s['scadenza']}</td></tr>
        <tr><td class="label">Ritardo</td><td><strong>{s['ritardo']} giorni</strong></td></tr>
        <tr><td class="label">Solleciti già inviati</td><td>{invoice.reminder_count or 0}</td></tr>
      </table>

      <div class="actions">
        <p style="margin:0 0 12px 0;font-weight:bold;color:#1f2937">Cosa vuoi fare?</p>
        <a href="{L['next_url']}" class="btn btn-next">{next_label}</a>
        <br>
        <a href="{L['paid']}" class="btn btn-paid">✅ Marca come pagata</a>
        <br><br>
        <p style="margin:8px 0;font-size:12px;color:#64748b">Altri solleciti:</p>
        <a href="{L['s1']}" class="btn btn-other">1° Sollecito</a>
        <a href="{L['s2']}" class="btn btn-other">2° Sollecito</a>
        <a href="{L['s3']}" class="btn btn-other">3° Sollecito</a>
        <a href="{L['diffida']}" class="btn btn-other">Diffida</a>
      </div>

      <p style="font-size:12px;color:#64748b">
        Cliccando un pulsante andrai su una pagina di conferma — <strong>nessuna email
        viene inviata al cliente prima della tua conferma</strong>.
      </p>
      <div class="footer">Notifica automatica — {company_name} · I link sono validi 30 giorni</div>
    </div></body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{company_name} <{cfg['user']}>"
    msg["To"]      = user.email
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        if cfg["use_tls"]:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=10)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=10)
        if cfg["user"] and cfg["password"]:
            server.login(cfg["user"], cfg["password"])
        server.sendmail(cfg["user"], user.email, msg.as_string())
        server.quit()
        return True, f"Email inviata a {user.email}"
    except Exception as e:
        log.error("Errore email a %s: %s", user.email, e)
        return False, str(e)


# ─── WHATSAPP via CallMeBot ───────────────────────────────────────────────────
def send_whatsapp_to_owner(user, invoice) -> tuple[bool, str]:
    """Invia messaggio WhatsApp via CallMeBot."""
    from models import UserSetting

    phone  = (user.phone or "").strip()
    apikey = UserSetting.get(user.id, "whatsapp_apikey", "").strip()

    if not phone or not apikey:
        return False, "WhatsApp non configurato (telefono + API key richiesti)"

    # CallMeBot vuole il numero senza '+' iniziale
    phone_clean = phone.lstrip("+").replace(" ", "")

    s = _invoice_summary(invoice)
    L = _build_quick_links(invoice)
    next_label = {
        "s1": "1° Sollecito", "s2": "2° Sollecito", "s3": "3° Sollecito",
        "diffida": "Diffida formale",
    }.get(L["next_action"], "Sollecito")

    text = (
        f"⚠️ *Fattura {s['numero']} SCADUTA*\n"
        f"👤 {s['cliente']}\n"
        f"💰 *{s['importo']}*\n"
        f"📅 Scaduta il {s['scadenza']} ({s['ritardo']}gg ritardo)\n"
        f"\n"
        f"📲 *Azioni rapide* (tap per confermare):\n"
        f"📧 {next_label}: {L['next_url']}\n"
        f"✅ Marca pagata: {L['paid']}\n"
        f"🔕 Stop notifiche: {L['stop']}"
    )

    try:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": phone_clean, "text": text, "apikey": apikey},
            timeout=15,
        )
        body_low = r.text.lower()
        if r.status_code == 200 and ("queued" in body_low or "sent" in body_low or "ok" in body_low):
            return True, "Messaggio WhatsApp inviato"
        # Errore tipico: numero non registrato sul bot
        return False, f"CallMeBot HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        log.error("Errore WhatsApp a %s: %s", user.username, e)
        return False, str(e)


# ─── Notifica completa (email + WhatsApp se abilitati) ────────────────────────
def notify_owner_of_overdue(user, invoice) -> dict:
    """
    Invia notifica al titolare (email + WhatsApp se abilitati).
    Restituisce dict con esito di ogni canale.
    """
    from models import db, UserSetting

    result = {"email": None, "whatsapp": None}

    if UserSetting.get(user.id, "notify_email_enabled") == "true":
        result["email"] = send_email_to_owner(user, invoice)
    if UserSetting.get(user.id, "notify_whatsapp_enabled") == "true":
        result["whatsapp"] = send_whatsapp_to_owner(user, invoice)

    if any(r and r[0] for r in result.values() if r):
        invoice.user_notified_at = datetime.utcnow()
        db.session.commit()

    return result


# ─── Notifiche PEC istituzionali ──────────────────────────────────────────────
def _send_pec_email_to_owner(user, pec_msg) -> tuple[bool, str]:
    """Email al titolare con riassunto PEC istituzionale."""
    from models import AppSettings
    if not user.email:
        return False, "Email utente non configurata"

    cfg = {
        "host":     AppSettings.get("smtp_host", "smtp.gmail.com"),
        "port":     int(AppSettings.get("smtp_port", "587")),
        "user":     AppSettings.get("smtp_user", ""),
        "password": AppSettings.get("smtp_password", ""),
        "use_tls":  AppSettings.get("smtp_use_tls", "true") == "true",
    }
    if not cfg["user"]:
        return False, "SMTP non configurato"

    base_url = AppSettings.get("app_external_url", "http://127.0.0.1:5000").rstrip("/")
    pec_url  = f"{base_url}/pec/{pec_msg.id}"

    urgency_color = {"alta": "#dc2626", "media": "#f59e0b", "bassa": "#64748b"}.get(pec_msg.urgency, "#64748b")
    urgency_label = {"alta": "URGENTE", "media": "IMPORTANTE", "bassa": "INFORMATIVA"}.get(pec_msg.urgency, "PEC")

    deadline_html = ""
    if pec_msg.deadline:
        deadline_html = f"<tr><td class='label'>⏰ Scadenza</td><td><strong style='color:#dc2626'>{pec_msg.deadline.strftime('%d/%m/%Y')}</strong></td></tr>"

    facts_html = ""
    if pec_msg.key_facts_list:
        facts_html = "<ul style='margin:8px 0;padding-left:20px'>"
        for f in pec_msg.key_facts_list[:5]:
            facts_html += f"<li>{f}</li>"
        facts_html += "</ul>"

    subject = f"🏛 [{pec_msg.sender_label}] {pec_msg.subject[:120]}"
    html = f"""<!DOCTYPE html><html lang="it"><head><meta charset="utf-8"><style>
      body{{font-family:Arial,sans-serif;font-size:14px;color:#1f2937;line-height:1.6}}
      .wrap{{max-width:560px;margin:20px auto;padding:24px;border:1px solid #e5e7eb;border-radius:8px}}
      .header{{background:{urgency_color};color:#fff;padding:12px 18px;border-radius:6px;margin-bottom:18px}}
      table{{width:100%;border-collapse:collapse;margin:12px 0}}
      td{{padding:8px;border-bottom:1px solid #f3f4f6;vertical-align:top}}
      .label{{color:#6b7280;width:35%}}
      .summary-box{{background:#f8fafc;border-left:4px solid {urgency_color};padding:12px 16px;margin:12px 0;border-radius:4px}}
      .action-box{{background:#fef3c7;padding:12px 16px;margin:12px 0;border-radius:6px}}
      .btn{{display:inline-block;background:#2563eb;color:#fff !important;padding:10px 20px;
            border-radius:6px;text-decoration:none;font-weight:bold;margin:12px 0}}
    </style></head><body><div class="wrap">
      <div class="header">
        <small style="opacity:.85">{urgency_label}</small>
        <h2 style="margin:4px 0 0 0">🏛 {pec_msg.sender_label}</h2>
      </div>

      <p>Ciao <strong>{user.username}</strong>, hai ricevuto una nuova PEC istituzionale:</p>

      <table>
        <tr><td class="label">Mittente</td><td>{pec_msg.sender}</td></tr>
        <tr><td class="label">Oggetto</td><td><strong>{pec_msg.subject}</strong></td></tr>
        <tr><td class="label">Categoria</td><td>{pec_msg.category or '—'}</td></tr>
        {deadline_html}
      </table>

      <div class="summary-box">
        <strong>📋 Riassunto:</strong><br>
        {pec_msg.summary}
        {facts_html}
      </div>

      {f'''<div class="action-box">
        <strong>👉 Cosa fare:</strong><br>
        {pec_msg.suggested_action}
      </div>''' if pec_msg.suggested_action else ''}

      <div style="text-align:center"><a href="{pec_url}" class="btn">Apri in GestFatture</a></div>
    </div></body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"GestFatture PEC <{cfg['user']}>"
    msg["To"]      = user.email
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        if cfg["use_tls"]:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=10)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=10)
        if cfg["user"] and cfg["password"]:
            server.login(cfg["user"], cfg["password"])
        server.sendmail(cfg["user"], user.email, msg.as_string())
        server.quit()
        return True, "Email PEC inviata"
    except Exception as e:
        log.error("Errore email PEC: %s", e)
        return False, str(e)


def _send_pec_whatsapp_to_owner(user, pec_msg) -> tuple[bool, str]:
    """WhatsApp al titolare con riassunto PEC istituzionale."""
    from models import UserSetting, AppSettings
    phone  = (user.phone or "").strip()
    apikey = UserSetting.get(user.id, "whatsapp_apikey", "").strip()
    if not phone or not apikey:
        return False, "WhatsApp non configurato"

    phone_clean = phone.lstrip("+").replace(" ", "")
    base_url    = AppSettings.get("app_external_url", "http://127.0.0.1:5000").rstrip("/")
    pec_url     = f"{base_url}/pec/{pec_msg.id}"

    urg_emoji = {"alta": "🚨", "media": "⚠️", "bassa": "ℹ️"}.get(pec_msg.urgency, "📧")
    deadline_str = f"\n⏰ Scadenza: *{pec_msg.deadline.strftime('%d/%m/%Y')}*" if pec_msg.deadline else ""

    text = (
        f"{urg_emoji} *PEC {pec_msg.sender_label}*\n"
        f"📧 {pec_msg.subject[:120]}\n"
        f"\n"
        f"📋 {pec_msg.summary[:300] if pec_msg.summary else '(no AI summary)'}"
        f"{deadline_str}\n"
        f"\n"
        f"👉 Apri: {pec_url}"
    )

    try:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": phone_clean, "text": text, "apikey": apikey},
            timeout=15,
        )
        body_low = r.text.lower()
        if r.status_code == 200 and ("queued" in body_low or "sent" in body_low or "ok" in body_low):
            return True, "WhatsApp PEC inviato"
        return False, f"CallMeBot HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        log.error("Errore WhatsApp PEC: %s", e)
        return False, str(e)


def notify_pec_message(user, pec_msg) -> dict:
    """Notifica titolare di una nuova PEC istituzionale."""
    from models import db, UserSetting, AuditLog

    result = {"email": None, "whatsapp": None}

    email_on = UserSetting.get(user.id, "notify_email_enabled") == "true"
    wa_on    = UserSetting.get(user.id, "notify_whatsapp_enabled") == "true"

    if email_on and user.email:
        result["email"] = _send_pec_email_to_owner(user, pec_msg)
    if wa_on:
        result["whatsapp"] = _send_pec_whatsapp_to_owner(user, pec_msg)

    if any(r and r[0] for r in result.values() if r):
        pec_msg.notified_at = datetime.utcnow()
        db.session.commit()

    # Audit log: utile per diagnosticare notifiche silently failed.
    try:
        details_parts = []
        if email_on:
            ok_e, msg_e = result["email"] or (False, "skipped: no email")
            details_parts.append(f"email: {'OK' if ok_e else 'FAIL'} ({msg_e[:80]})")
        else:
            details_parts.append("email: disabled")
        if wa_on:
            ok_w, msg_w = result["whatsapp"] or (False, "skipped")
            details_parts.append(f"wa: {'OK' if ok_w else 'FAIL'} ({msg_w[:80]})")
        else:
            details_parts.append("wa: disabled")

        sender_label = pec_msg.sender_label or "?"
        details = f"sender={sender_label}; " + " | ".join(details_parts)
        ok_any = any(r and r[0] for r in result.values() if r)
        action = "pec_notify_ok" if ok_any else "pec_notify_failed"
        log_row = AuditLog(
            user_id  = user.id,
            username = user.username,
            action   = action[:60],
            target   = f"pec:{pec_msg.id}"[:200],
            details  = details[:2000],
        )
        db.session.add(log_row)
        db.session.commit()
    except Exception as e:
        log.warning("Audit log notify_pec fallito: %s", e)
        try: db.session.rollback()
        except Exception: pass

    return result
