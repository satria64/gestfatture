import os
import re
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.utils import formatdate, make_msgid, formataddr
from datetime import datetime

from models import AppSettings

log = logging.getLogger(__name__)


def _get_smtp_config():
    return {
        "host":     AppSettings.get("smtp_host",     "smtp.gmail.com"),
        "port":     int(AppSettings.get("smtp_port", "587")),
        "user":     AppSettings.get("smtp_user",     ""),
        "password": AppSettings.get("smtp_password", ""),
        "use_tls":  AppSettings.get("smtp_use_tls",  "true") == "true",
        "from_name":AppSettings.get("company_name",  "Gestione Fatture"),
    }


# ─── Provider dispatcher (SMTP / Resend) ────────────────────────────────────
def email_provider() -> str:
    """Restituisce 'resend' se è il provider preferito + API key configurata, altrimenti 'smtp'."""
    pref = (AppSettings.get("email_provider", "smtp") or "smtp").lower()
    if pref == "resend" and AppSettings.get("resend_api_key", "").strip():
        return "resend"
    return "smtp"


def _send_via_smtp(msg, sender_email: str, recipient: str) -> tuple[bool, str]:
    cfg = _get_smtp_config()
    if not cfg["host"] or not cfg["user"]:
        return False, "SMTP non configurato"
    try:
        if cfg["use_tls"]:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15)
        if cfg["user"] and cfg["password"]:
            server.login(cfg["user"], cfg["password"])
        server.sendmail(sender_email, recipient, msg.as_string())
        server.quit()
        return True, "OK (SMTP)"
    except Exception as e:
        return False, f"SMTP: {e}"


def _send_via_resend(subject: str, recipient: str, html: str, plain: str,
                     sender_name: str, sender_email: str,
                     reply_to: str | None = None,
                     attachments: list[dict] | None = None) -> tuple[bool, str]:
    """attachments: lista di {filename, content (bytes)}."""
    api_key  = AppSettings.get("resend_api_key", "").strip()
    from_addr = AppSettings.get("resend_from_email", "").strip() or sender_email
    if not api_key:
        return False, "Resend API key non configurata"
    if not from_addr:
        return False, "Resend from email non configurato"
    try:
        import resend
        resend.api_key = api_key
        from_field = f"{sender_name} <{from_addr}>" if sender_name else from_addr
        params = {
            "from": from_field,
            "to": [recipient],
            "subject": subject,
            "html": html,
            "text": plain or "",
        }
        if reply_to:
            params["reply_to"] = reply_to
        if attachments:
            import base64
            params["attachments"] = [
                {"filename": a["filename"],
                 "content":  base64.b64encode(a["content"]).decode()}
                for a in attachments
            ]
        result = resend.Emails.send(params)
        return True, f"OK (Resend id={result.get('id', '?')})"
    except Exception as e:
        return False, f"Resend: {e}"


def deliver_email(*, msg, subject: str, recipient: str, html: str, plain: str,
                  sender_name: str, sender_email: str,
                  reply_to: str | None = None,
                  attachments: list[dict] | None = None) -> tuple[bool, str]:
    """Invia un'email tramite Resend (se configurato come provider) o SMTP.

    Se Resend fallisce, fa fallback automatico su SMTP.
    `msg` è il MIMEMessage già pronto, usato dal path SMTP.
    Gli altri argomenti servono al path Resend.
    """
    if email_provider() == "resend":
        ok, info = _send_via_resend(
            subject=subject, recipient=recipient, html=html, plain=plain,
            sender_name=sender_name, sender_email=sender_email,
            reply_to=reply_to, attachments=attachments,
        )
        if ok:
            return ok, info
        log.warning("Resend ha fallito (%s) – fallback SMTP", info)
    return _send_via_smtp(msg, sender_email, recipient)


def _html_to_text(html: str) -> str:
    """Converte l'HTML del sollecito in testo semplice leggibile."""
    # Sostituzione tag a-tag con testo + link tra parentesi
    text = re.sub(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                  r'\2 (\1)', html, flags=re.IGNORECASE | re.DOTALL)
    # br/p in newline
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
    # Rimuovi tutti i tag rimasti
    text = re.sub(r'<[^>]+>', '', text)
    # Decodifica entità HTML basiche
    text = (text.replace('&nbsp;', ' ').replace('&amp;', '&')
                .replace('&lt;', '<').replace('&gt;', '>')
                .replace('&quot;', '"').replace('&#39;', "'")
                .replace('&euro;', '€'))
    # Comprimi spazi multipli e righe vuote multiple
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _build_html(invoice, reminder_type: str, company_name: str, payment_link: str) -> tuple[str, str]:
    """Restituisce (subject, html_body) in base al tipo di sollecito."""
    client_name = invoice.client.name
    numero      = invoice.number
    importo     = f"€ {invoice.amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    scadenza    = invoice.due_date.strftime("%d/%m/%Y")
    giorni      = invoice.days_overdue

    pay_btn = ""
    if payment_link:
        pay_btn = f"""
        <p style="text-align:center;margin:24px 0">
          <a href="{payment_link}" style="background:#2563eb;color:#fff;padding:12px 28px;
             border-radius:6px;text-decoration:none;font-weight:bold">
            Paga ora
          </a>
        </p>"""

    templates = {
        "pre_scadenza": (
            f"Promemoria: fattura n. {numero} in scadenza il {scadenza}",
            f"""<p>Gentile <strong>{client_name}</strong>,</p>
            <p>le ricordiamo che la fattura n. <strong>{numero}</strong>
            dell'importo di <strong>{importo}</strong>
            è in scadenza il <strong>{scadenza}</strong>.</p>
            <p>Per facilitare il pagamento, può utilizzare il link dedicato:{pay_btn}</p>
            <p>La ringraziamo per la sua collaborazione.</p>"""
        ),
        "sollecito_1": (
            f"Primo sollecito – fattura n. {numero} scaduta",
            f"""<p>Gentile <strong>{client_name}</strong>,</p>
            <p>la fattura n. <strong>{numero}</strong> di <strong>{importo}</strong>
            risulta <strong>non saldata</strong> da {giorni} giorni (scadenza: {scadenza}).</p>
            <p>La invitiamo a procedere al pagamento entro i prossimi giorni.{pay_btn}</p>
            <p>Per qualsiasi chiarimento siamo a sua disposizione.</p>"""
        ),
        "sollecito_2": (
            f"Secondo sollecito – fattura n. {numero} in ritardo",
            f"""<p>Gentile <strong>{client_name}</strong>,</p>
            <p>nonostante il precedente sollecito, la fattura n. <strong>{numero}</strong>
            di <strong>{importo}</strong> risulta ancora <strong>insoluta</strong>
            ({giorni} giorni di ritardo).</p>
            <p>Le chiediamo di regolarizzare urgentemente la sua posizione.{pay_btn}</p>"""
        ),
        "sollecito_3": (
            f"Terzo sollecito – fattura n. {numero} – azione richiesta",
            f"""<p>Gentile <strong>{client_name}</strong>,</p>
            <p>siamo costretti a ricordarle per la terza volta che la fattura
            n. <strong>{numero}</strong> di <strong>{importo}</strong>
            è <strong>insoluta da {giorni} giorni</strong>.</p>
            <p>La preghiamo di provvedere immediatamente al pagamento,
            pena il ricorso alle procedure di recupero crediti.{pay_btn}</p>"""
        ),
        "diffida": (
            f"DIFFIDA – fattura n. {numero} – recupero crediti",
            f"""<p>Gentile <strong>{client_name}</strong>,</p>
            <p>con la presente siamo a diffiderla formalmente a provvedere al pagamento
            della fattura n. <strong>{numero}</strong> di <strong>{importo}</strong>,
            scaduta il {scadenza} e ancora <strong>insoluta dopo {giorni} giorni</strong>.</p>
            <p>In assenza di riscontro entro 5 giorni lavorativi, saremo costretti
            ad avviare le procedure legali di recupero crediti.{pay_btn}</p>"""
        ),
    }

    subject, body = templates.get(reminder_type, templates["sollecito_1"])

    html = f"""<!DOCTYPE html>
<html lang="it">
<head><meta charset="utf-8"><style>
  body{{font-family:Arial,sans-serif;font-size:14px;color:#1f2937;line-height:1.6}}
  .wrap{{max-width:600px;margin:0 auto;padding:24px}}
  .header{{background:#1e3a5f;color:#fff;padding:16px 24px;border-radius:6px 6px 0 0}}
  .footer{{margin-top:24px;font-size:12px;color:#6b7280;border-top:1px solid #e5e7eb;padding-top:12px}}
</style></head>
<body>
  <div class="wrap">
    <div class="header"><h2 style="margin:0">{company_name}</h2></div>
    <div style="padding:24px 0">{body}</div>
    <div class="footer">
      Questa è una comunicazione automatica. Non rispondere a questa email.<br>
      {company_name}
    </div>
  </div>
</body></html>"""

    return subject, html


def send_reminder(invoice, reminder_type: str) -> bool:
    """Invia il sollecito via email/PEC. Restituisce True se l'invio è riuscito."""
    from models import UserSetting
    # Difensivo: non si invia mai un sollecito per una nota di credito
    if invoice.is_credit_note:
        log.warning("Sollecito ignorato: la fattura n.%s è una nota di credito", invoice.number)
        return False
    cfg          = _get_smtp_config()
    recipient    = invoice.client.contact_email
    # Nome azienda: prima il personale dell'utente proprietario, poi il globale
    user_company = UserSetting.get(invoice.user_id, "company_name") if invoice.user_id else ""
    company_name = user_company or cfg["from_name"]
    payment_link = invoice.payment_link or ""

    if not recipient:
        log.warning("Nessun indirizzo email per il cliente %s", invoice.client.name)
        return False

    subject, html_body = _build_html(invoice, reminder_type, company_name, payment_link)

    # ─── Plain text alternativo (richiesto dagli antispam) ──────────────────
    plain_body = _html_to_text(html_body)

    # Container "mixed" se c'è PDF, "alternative" altrimenti.
    # Importante: SEMPRE attaccare PRIMA il plain text, POI l'HTML
    # (lo standard MIME vuole l'ultima parte come "preferita").
    has_pdf = bool(invoice.pdf_filename)
    if has_pdf:
        msg = MIMEMultipart("mixed")
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(plain_body, "plain", "utf-8"))
        body_part.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(body_part)
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(plain_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    # ─── Header completi per anti-spam ──────────────────────────────────────
    sender_email = cfg["user"]
    msg["Subject"]    = subject
    msg["From"]       = formataddr((company_name, sender_email))
    msg["To"]         = recipient
    msg["Reply-To"]   = sender_email
    msg["Date"]       = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender_email.split("@")[-1])
    msg["X-Mailer"]   = "GestFatture Invoice Manager"
    # Header importante: dichiara che è una notifica auto, non spam
    msg["X-Auto-Response-Suppress"] = "OOF, AutoReply"
    msg["Auto-Submitted"] = "auto-generated"

    # Allega il PDF della fattura se presente
    pdf_attachment = None
    if has_pdf:
        from app import get_upload_folder
        pdf_path = os.path.join(get_upload_folder(), invoice.pdf_filename)
        if os.path.exists(pdf_path):
            with open(pdf_path, "rb") as fp:
                pdf_bytes = fp.read()
            pdf_filename = f"Fattura_{invoice.number.replace('/', '_')}.pdf"
            part = MIMEApplication(pdf_bytes, _subtype="pdf")
            part.add_header("Content-Disposition", "attachment", filename=pdf_filename)
            msg.attach(part)
            pdf_attachment = {"filename": pdf_filename, "content": pdf_bytes}
            log.info("Allegato PDF: %s", pdf_path)

    ok, info = deliver_email(
        msg=msg, subject=subject, recipient=recipient,
        html=html_body, plain=plain_body,
        sender_name=company_name, sender_email=sender_email,
        reply_to=sender_email,
        attachments=[pdf_attachment] if pdf_attachment else None,
    )
    if ok:
        log.info("Email inviata a %s – tipo: %s [%s]", recipient, reminder_type, info)
    else:
        log.error("Errore invio email: %s", info)
    return ok
