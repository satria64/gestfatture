"""
PEC IMAP integration: scarica gli allegati XML/p7m/ZIP dalle email PEC ricevute
e li importa automaticamente.

Tipici provider PEC e loro server IMAP:
  Aruba PEC:     imaps.pec.aruba.it          (porta 993, SSL)
  Legalmail:     mbox.cert.legalmail.it      (porta 993, SSL)
  Register PEC:  pec.register.it             (porta 993, SSL)
  PEC.it:        imap.pec.it                 (porta 993, SSL)
"""

import os
import re
import json
import imaplib
import email
import logging
from datetime import datetime
from email.header import decode_header

log = logging.getLogger(__name__)

ACCEPTED_EXT = (".xml", ".p7m", ".zip")

# ─── Mittenti PEC istituzionali da monitorare ─────────────────────────────────
INSTITUTIONAL_DOMAINS = {
    "agenziaentrate.it":         "Agenzia delle Entrate",
    "pec.agenziaentrate.it":     "Agenzia delle Entrate",
    "pce.agenziaentrate.it":     "Agenzia delle Entrate",
    "agenziariscossione.gov.it": "Agenzia Riscossione",
    "pec.agenziariscossione.gov.it": "Agenzia Riscossione",
    "inps.gov.it":               "INPS",
    "postacert.inps.gov.it":     "INPS",
    "pec.inps.gov.it":           "INPS",
    "inail.it":                  "INAIL",
    "inail.gov.it":              "INAIL",
    "pec.inail.it":              "INAIL",
    "postacert.inail.gov.it":    "INAIL",
}


def identify_institutional_sender(email_addr: str) -> str | None:
    """Restituisce l'etichetta del mittente istituzionale o None."""
    if not email_addr:
        return None
    domain = email_addr.split("@", 1)[-1].lower().strip(">").strip()
    for d, label in INSTITUTIONAL_DOMAINS.items():
        if d == domain or domain.endswith("." + d):
            return label
    return None


def _get_upload_folder() -> str:
    folder = os.path.join(os.getcwd(), "uploads")
    os.makedirs(folder, exist_ok=True)
    return folder


def _decode_filename(raw: str) -> str:
    """Decodifica nomi file MIME-encoded (=?utf-8?B?...?=)."""
    parts = decode_header(raw)
    out = ""
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                out += chunk.decode(enc or "utf-8", errors="replace")
            except LookupError:
                out += chunk.decode("utf-8", errors="replace")
        else:
            out += chunk
    return out


def _connect(host: str, port: int, user: str, password: str, use_ssl: bool):
    if use_ssl:
        m = imaplib.IMAP4_SSL(host, port)
    else:
        m = imaplib.IMAP4(host, port)
    m.login(user, password)
    return m


def test_connection(host: str, port: int, user: str, password: str, use_ssl: bool,
                    folder: str = "INBOX") -> tuple[bool, str]:
    try:
        m = _connect(host, port, user, password, use_ssl)
        typ, _ = m.select(folder, readonly=True)
        if typ != "OK":
            m.logout()
            return False, f"Cartella '{folder}' non accessibile."
        typ, data = m.search(None, "ALL")
        count = len(data[0].split()) if typ == "OK" else 0
        m.logout()
        return True, f"Connessione OK — {count} messaggi totali in '{folder}'."
    except imaplib.IMAP4.error as e:
        return False, f"Errore IMAP: {e}"
    except Exception as e:
        return False, f"Errore connessione: {e}"


def fetch_new_attachments(host: str, port: int, user: str, password: str,
                          use_ssl: bool, folder: str = "INBOX"
                          ) -> list[tuple[str, bytes]]:
    """
    Scarica gli allegati dalle email NON LETTE che hanno file XML/p7m/ZIP.
    Marca i messaggi come letti dopo l'elaborazione.
    Restituisce lista di (filename, bytes).
    """
    m = _connect(host, port, user, password, use_ssl)
    typ, _ = m.select(folder)
    if typ != "OK":
        m.logout()
        raise RuntimeError(f"Cartella '{folder}' non accessibile.")

    typ, data = m.search(None, "UNSEEN")
    if typ != "OK":
        m.logout()
        return []

    attachments: list[tuple[str, bytes]] = []
    msg_ids = data[0].split()
    log.info("PEC: %d messaggi non letti da analizzare", len(msg_ids))

    for num in msg_ids:
        typ, payload = m.fetch(num, "(RFC822)")
        if typ != "OK" or not payload or not payload[0]:
            continue
        try:
            msg = email.message_from_bytes(payload[0][1])
        except Exception as e:
            log.warning("PEC: parsing email %s fallito: %s", num, e)
            continue

        for part in msg.walk():
            if part.get_content_disposition() != "attachment":
                continue
            raw_name = part.get_filename() or ""
            if not raw_name:
                continue
            fname = _decode_filename(raw_name)
            lower = fname.lower()
            if not (lower.endswith(ACCEPTED_EXT) or lower.endswith(".xml.p7m")):
                continue
            try:
                content = part.get_payload(decode=True)
            except Exception:
                continue
            if content:
                attachments.append((fname, content))

        # Marca come letto
        try:
            m.store(num, "+FLAGS", "\\Seen")
        except Exception:
            pass

    try:
        m.close()
    except Exception:
        pass
    m.logout()
    return attachments


def fetch_full_messages(host, port, user, password, use_ssl, folder="INBOX"
                        ) -> list[dict]:
    """
    Scarica le email NON LETTE: per ognuna restituisce dict con
    {sender, subject, message_id, body, attachments:[(fname,bytes)]}.
    Marca le email come lette.
    """
    m = _connect(host, port, user, password, use_ssl)
    typ, _ = m.select(folder)
    if typ != "OK":
        m.logout()
        raise RuntimeError(f"Cartella '{folder}' non accessibile.")

    typ, data = m.search(None, "UNSEEN")
    if typ != "OK":
        m.logout()
        return []

    out = []
    for num in data[0].split():
        typ, payload = m.fetch(num, "(RFC822)")
        if typ != "OK" or not payload or not payload[0]:
            continue
        try:
            msg = email.message_from_bytes(payload[0][1])
        except Exception:
            continue

        # Sender (estrai indirizzo email)
        sender_raw = msg.get("From", "")
        emails = re.findall(r"[\w\.\-+]+@[\w\.\-]+", sender_raw)
        sender = emails[0] if emails else sender_raw

        subject = _decode_filename(msg.get("Subject", "")).strip()
        msg_id  = (msg.get("Message-ID", "") or "").strip()

        # Body: priorità text/plain, fallback text/html (strip tag)
        body = ""
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = part.get_content_disposition() or ""
            if "attachment" in disp:
                continue
            try:
                payload_bytes = part.get_payload(decode=True)
                if not payload_bytes:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload_bytes.decode(charset, errors="ignore")
                if ctype == "text/plain":
                    body += text + "\n"
                elif ctype == "text/html" and not body:
                    body += re.sub(r"<[^>]+>", " ", text)
            except Exception:
                continue
        body = re.sub(r"\s+", " ", body).strip()

        # Allegati
        attachments = []
        for part in msg.walk():
            disp = part.get_content_disposition() or ""
            if "attachment" not in disp:
                continue
            fname_raw = part.get_filename() or ""
            if not fname_raw:
                continue
            fname = _decode_filename(fname_raw)
            try:
                content = part.get_payload(decode=True)
                if content:
                    attachments.append((fname, content))
            except Exception:
                continue

        out.append({
            "sender":      sender,
            "subject":     subject,
            "message_id":  msg_id,
            "body":        body,
            "attachments": attachments,
        })

        try:
            m.store(num, "+FLAGS", "\\Seen")
        except Exception:
            pass

    try:
        m.close()
    except Exception:
        pass
    m.logout()
    return out


def sync_for_user(app, user_id: int):
    """Sincronizza la PEC per un singolo utente: import fatture + analisi PEC istituzionali."""
    from models import UserSetting, AppSettings, db, PecMessage
    with app.app_context():
        if UserSetting.get(user_id, "integration_pec_enabled") != "true":
            return

        host = UserSetting.get(user_id, "integration_pec_host", "")
        port = int(UserSetting.get(user_id, "integration_pec_port", "993") or "993")
        user = UserSetting.get(user_id, "integration_pec_user", "")
        pwd  = UserSetting.get(user_id, "integration_pec_password", "")
        ssl  = UserSetting.get(user_id, "integration_pec_use_ssl", "true") == "true"
        folder = UserSetting.get(user_id, "integration_pec_folder", "INBOX") or "INBOX"

        if not all([host, user, pwd]):
            return

        # PEC analysis è ON solo se l'utente l'ha attivata
        analyze_institutional = UserSetting.get(user_id, "pec_analysis_enabled") == "true"

        try:
            messages = fetch_full_messages(host, port, user, pwd, ssl, folder)
        except Exception as e:
            log.error("PEC sync u=%d error: %s", user_id, e)
            UserSetting.set(user_id, "integration_pec_last_error", str(e))
            return

        if not messages:
            UserSetting.set(user_id, "integration_pec_last_sync", datetime.utcnow().isoformat())
            return

        from import_service import (
            process_xml_import, process_p7m_import, process_zip_import,
        )
        upload_folder = _get_upload_folder()
        total_inv = 0
        total_pec = 0

        for msg in messages:
            sender_label = identify_institutional_sender(msg["sender"])

            # ── 1. Process attachments come fatture (sempre) ─────────────────
            for fname, content in msg["attachments"]:
                lower = fname.lower()
                try:
                    if lower.endswith(".xml.p7m") or lower.endswith(".p7m"):
                        n_ok, _, _ = process_p7m_import(content, fname, db, upload_folder, user_id=user_id)
                    elif lower.endswith(".xml"):
                        n_ok, _, _ = process_xml_import(content, fname, db, upload_folder, user_id=user_id)
                    elif lower.endswith(".zip"):
                        n_ok, _, _ = process_zip_import(content, fname, db, upload_folder, user_id=user_id)
                    else:
                        continue
                    total_inv += n_ok
                except Exception as e:
                    log.error("PEC u=%d: errore import allegato '%s': %s", user_id, fname, e)

            # ── 2. Se mittente istituzionale: salva PEC + analisi opzionale ──
            if sender_label and analyze_institutional:
                if msg["message_id"] and PecMessage.query.filter_by(
                    user_id=user_id, message_id=msg["message_id"]
                ).first():
                    continue  # già processato

                attach_names = [fname for fname, _ in msg["attachments"]]
                pec = PecMessage(
                    user_id=user_id,
                    message_id=msg["message_id"] or f"no-id-{datetime.utcnow().timestamp()}",
                    sender=msg["sender"],
                    sender_label=sender_label,
                    subject=msg["subject"][:500],
                    body_excerpt=msg["body"][:8000],
                    attachments=json.dumps(attach_names),
                )

                # Tentativo analisi via Claude API
                api_key = AppSettings.get("anthropic_api_key", "")
                if api_key:
                    try:
                        from claude_service import analyze_pec_email, DEFAULT_MODEL
                        model = AppSettings.get("anthropic_model", "") or DEFAULT_MODEL
                        analysis = analyze_pec_email(
                            msg["subject"], msg["body"], msg["sender"], attach_names,
                            api_key, model
                        )
                        pec.category = analysis.get("category", "altro")[:50]
                        pec.urgency  = analysis.get("urgency", "media")[:20]
                        pec.summary  = analysis.get("summary", "")
                        pec.suggested_action = analysis.get("suggested_action", "")
                        pec.key_facts = json.dumps(analysis.get("key_facts", []))
                        if analysis.get("deadline"):
                            try:
                                pec.deadline = datetime.strptime(analysis["deadline"], "%Y-%m-%d").date()
                            except Exception:
                                pass
                    except Exception as e:
                        log.warning("PEC u=%d: Claude analysis fallita: %s", user_id, e)
                        pec.summary = f"[Analisi AI fallita] {msg['subject']}"
                        pec.urgency = "media"
                else:
                    # Fallback senza AI: euristica basica
                    pec.summary = msg["body"][:300] or msg["subject"]
                    pec.urgency = "alta" if any(w in msg["body"].lower() for w in
                                ["intimazione", "diffida", "cartella", "ingiunzione"]) else "media"
                    pec.category = "comunicazione"

                db.session.add(pec); db.session.flush()
                total_pec += 1

                # Notifica titolare via email/WhatsApp
                from notification_service import notify_pec_message
                from models import User
                u = User.query.get(user_id)
                if u:
                    try:
                        notify_pec_message(u, pec)
                    except Exception as e:
                        log.error("Notifica PEC u=%d fallita: %s", user_id, e)

        db.session.commit()
        log.info("PEC sync u=%d: %d fatture importate, %d PEC istituzionali analizzate.",
                 user_id, total_inv, total_pec)

        UserSetting.set(user_id, "integration_pec_last_sync", datetime.utcnow().isoformat())
        UserSetting.set(user_id, "integration_pec_last_error", "")
        UserSetting.set(user_id, "integration_pec_last_count",
                        f"{total_inv} fatture, {total_pec} PEC")


def sync(app):
    """Job periodico: itera su tutti gli utenti con PEC abilitata."""
    from models import User
    with app.app_context():
        users = User.query.all()
    for u in users:
        try:
            sync_for_user(app, u.id)
        except Exception as e:
            log.error("PEC sync u=%d: %s", u.id, e)
