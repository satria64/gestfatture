import os
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
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

    # Se c'è un PDF allegato, usiamo "mixed" come container e mettiamo il body
    # come sottoparte "alternative" — così l'email è valida con allegato.
    has_pdf = bool(invoice.pdf_filename)
    if has_pdf:
        msg = MIMEMultipart("mixed")
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(body_part)
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    msg["Subject"] = subject
    msg["From"]    = f"{company_name} <{cfg['user']}>"
    msg["To"]      = recipient

    # Allega il PDF della fattura se presente
    if has_pdf:
        pdf_path = os.path.join(os.getcwd(), "uploads", invoice.pdf_filename)
        if os.path.exists(pdf_path):
            with open(pdf_path, "rb") as fp:
                part = MIMEApplication(fp.read(), _subtype="pdf")
                part.add_header(
                    "Content-Disposition", "attachment",
                    filename=f"Fattura_{invoice.number.replace('/', '_')}.pdf"
                )
                msg.attach(part)
            log.info("Allegato PDF: %s", pdf_path)

    try:
        if cfg["use_tls"]:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=10)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=10)

        if cfg["user"] and cfg["password"]:
            server.login(cfg["user"], cfg["password"])

        server.sendmail(cfg["user"], recipient, msg.as_string())
        server.quit()
        log.info("Email inviata a %s – tipo: %s", recipient, reminder_type)
        return True

    except Exception as exc:
        log.error("Errore invio email: %s", exc)
        return False
