"""
Estrazione dati fattura tramite Anthropic Claude API.

Documentazione: https://docs.anthropic.com/en/docs/build-with-claude/pdf-support
"""

import base64
import json
import logging
import re
from datetime import datetime, date

log = logging.getLogger(__name__)

# Modelli disponibili (default: Sonnet 4.6 = qualità alta, costo medio)
DEFAULT_MODEL = "claude-sonnet-4-6"
ALL_MODELS = [
    ("claude-haiku-4-5-20251001", "Haiku 4.5 — il più economico (~€0.005/PDF)"),
    ("claude-sonnet-4-6",          "Sonnet 4.6 — qualità alta (~€0.02/PDF) ★"),
    ("claude-opus-4-7",            "Opus 4.7 — massima qualità (~€0.08/PDF)"),
]

EXTRACTION_PROMPT = """Stai analizzando un PDF caricato da un utente italiano. Devi:
1. PRIMA decidere se il documento è davvero una FATTURA / parcella / nota di credito / nota di debito (TD01, TD04, TD05, TD06).
2. SOLO se lo è, estrarre i dati del CLIENTE/DESTINATARIO (cessionario/committente).

Restituisci ESCLUSIVAMENTE un oggetto JSON valido, senza markdown, senza testo extra.

Schema richiesto:
{
  "is_invoice":     true | false,
  "doc_type_guess": "fattura" | "nota_credito" | "nota_debito" | "parcella" | "ricevuta" | "scontrino" | "contratto" | "preventivo" | "ddt" | "estratto_conto" | "lettera" | "altro",
  "number":         "numero fattura come stringa (es. '100', '2024/0001'), null se non è una fattura",
  "amount":         numero decimale (importo TOTALE da pagare comprensivo di IVA — Totale documento, NON imponibile), null se non è una fattura,
  "issue_date":     "data emissione formato YYYY-MM-DD",
  "due_date":       "data scadenza pagamento formato YYYY-MM-DD",
  "client_name":    "ragione sociale o nome completo del CLIENTE (cessionario)",
  "vat_number":     "P.IVA del CLIENTE (11 cifre, NON dell'emittente)",
  "address":        "indirizzo completo del cliente: via, civico, CAP, città, provincia",
  "phone":          "telefono del cliente, se presente",
  "email":          "email del cliente, se presente",
  "pec":            "PEC del cliente, se presente"
}

QUANDO is_invoice = false:
- Il documento NON è una fattura/parcella/NC/ND italiana.
- Esempi: ricevute fiscali generiche, scontrini, contratti, preventivi, DDT, estratti conto, brochure, lettere commerciali, comunicazioni, certificati, fogli di calcolo, immagini.
- In questo caso compila SOLO `is_invoice: false` e `doc_type_guess`. Tutti gli altri campi a null.
- Non inventare dati. Non forzare l'estrazione se mancano gli elementi tipici (numero fattura, totale, P.IVA cedente E cessionario, dicitura "Fattura"/"Parcella"/"Nota di credito").

QUANDO is_invoice = true:
- Il CEDENTE/PRESTATORE è chi EMETTE la fattura → IGNORALO completamente.
- Il CESSIONARIO/COMMITTENTE/DESTINATARIO/SPETTABILE è il CLIENTE → estrai questi dati.
- L'importo è il "Totale" / "Totale documento" / "Totale da pagare", NON "Imponibile" né "IVA".
- Le date in formato ISO YYYY-MM-DD (es. "2025-09-04").
- Se un campo facoltativo (telefono, email, PEC) non è determinabile, usa null.

Risposta in JSON puro: nessun ```, nessuna spiegazione, solo l'oggetto JSON."""


def _strip_codefence(s: str) -> str:
    """Rimuove ```json … ``` se presente."""
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*\n", "", s)
        s = re.sub(r"\n```\s*$", "", s)
    return s.strip()


def extract_with_claude(pdf_bytes: bytes, api_key: str,
                        model: str = DEFAULT_MODEL,
                        my_vat_number: str = "",
                        my_company_name: str = "") -> dict:
    """
    Manda il PDF a Claude e restituisce un dict con i campi normalizzati.

    `my_vat_number` e `my_company_name`: se forniti, Claude sa chi è l'utente
    e può distinguere tra fatture ATTIVE (utente=emittente) e PASSIVE (utente=destinatario).
    Solleva eccezioni se l'API fallisce o il JSON non è parsabile.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    # ── Costruisci il prompt con eventuale contesto identità utente ─────────
    full_prompt = EXTRACTION_PROMPT
    if my_vat_number or my_company_name:
        identity_block = f"""

⚠️ CONTESTO IMPORTANTE — IDENTITÀ DELL'UTENTE DI QUESTA APP:
- Azienda dell'utente: {my_company_name or '(non specificata)'}
- P.IVA dell'utente: {my_vat_number or '(non specificata)'}

REGOLE FONDAMENTALI:
- Se questa azienda è il CEDENTE/EMITTENTE della fattura → è una FATTURA ATTIVA.
  Estrai il CESSIONARIO/DESTINATARIO come client (procedi normalmente).
- Se questa azienda è il CESSIONARIO/DESTINATARIO → è una FATTURA PASSIVA.
  In questo caso ESTRAI IL CEDENTE/EMITTENTE (il fornitore) come "client_name",
  "vat_number", "address", ecc. NON l'utente stesso.

In entrambi i casi i campi "client_name", "vat_number", ecc. devono SEMPRE riferirsi
alla CONTROPARTE della fattura, MAI all'utente stesso.
"""
        full_prompt = EXTRACTION_PROMPT + identity_block

    log.info("Claude API: estrazione PDF con modello %s (identità: %s)",
             model, my_company_name or my_vat_number or "n/d")

    message = client.messages.create(
        model=model,
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type":       "base64",
                        "media_type": "application/pdf",
                        "data":       pdf_b64,
                    },
                },
                {"type": "text", "text": full_prompt},
            ],
        }],
    )

    raw    = message.content[0].text
    parsed = json.loads(_strip_codefence(raw))

    # ── Normalizzazione ──────────────────────────────────────────────────────
    # Date: stringa ISO → date object
    for k in ("issue_date", "due_date"):
        v = parsed.get(k)
        if isinstance(v, str):
            try:
                parsed[k] = datetime.strptime(v, "%Y-%m-%d").date()
            except ValueError:
                parsed[k] = None

    # Importo: forza float
    if parsed.get("amount") is not None:
        try:
            parsed["amount"] = float(parsed["amount"])
        except (ValueError, TypeError):
            parsed["amount"] = None

    # Numero fattura: forza stringa
    if parsed.get("number") is not None:
        parsed["number"] = str(parsed["number"]).strip()

    # P.IVA: pulisci da non-cifre
    if parsed.get("vat_number"):
        digits = re.sub(r"\D", "", str(parsed["vat_number"]))
        parsed["vat_number"] = digits[:11] if len(digits) >= 11 else None

    # Rimuovi None / stringhe vuote
    return {k: v for k, v in parsed.items() if v not in (None, "", "null")}


# ─── Analisi PEC istituzionali (AdE / INPS / INAIL) ───────────────────────────
PEC_ANALYSIS_PROMPT = """Sei un assistente che aiuta un'azienda italiana a gestire le PEC istituzionali.

Analizza questa email PEC ricevuta e restituisci SOLO un oggetto JSON valido.

MITTENTE: {sender}
OGGETTO: {subject}
ALLEGATI: {attachments}

CORPO EMAIL (estratto):
\"\"\"
{body}
\"\"\"

Schema JSON da restituire (rispondi SOLO con questo, senza markdown):
{{
  "category": "comunicazione" | "cartella_pagamento" | "avviso_bonario" | "richiesta_documenti" | "rimborso" | "scadenza" | "intimazione" | "ingiunzione" | "accertamento" | "DURC" | "esito_SDI" | "altro",
  "urgency": "alta" | "media" | "bassa",
  "summary": "Riassunto in 2-3 frasi cosa contiene questa comunicazione",
  "key_facts": ["fatto importante 1", "importi/numeri rilevanti", "riferimenti normativi se presenti"],
  "deadline": "YYYY-MM-DD" oppure null,
  "suggested_action": "Cosa dovrebbe fare l'azienda destinataria (1 frase concisa)"
}}

Regole:
- "alta" = cartella esattoriale, intimazione, diffida, ingiunzione, scadenze entro 30 giorni
- "media" = avviso bonario, richiesta documenti, comunicazione importante
- "bassa" = notifiche generiche, riscontri positivi, conferme
- key_facts contiene 2-4 fatti chiave: importi (€), date, numeri di pratica
- Risposta in JSON puro, in italiano."""


# ─── Chat assistente AI per gli utenti dell'app ────────────────────────────────
CHAT_SYSTEM_PROMPT = """Sei l'assistente AI di GestFatture, un'app italiana per la gestione di fatture, solleciti automatici e contabilità clienti. Rispondi sempre in italiano, in modo chiaro, conciso e amichevole.

CONOSCENZE COMPLETE DI GESTFATTURE:

📊 Dashboard: KPI fatture (totali, pagate, in attesa, scadute) + stat NC/ND.

📄 Fatture (TipoDocumento):
  • TD01 = Fattura standard, sollecitabile
  • TD04 = Nota di Credito (importo negativo, NO solleciti, può compensare una fattura)
  • TD05 = Nota di Debito (sollecitabile come fattura)
  • TD06 = Parcella

✏️ Creazione/import fatture:
  • Manuale: /invoices/new — campi: tipo doc, cliente, numero, importo, date, PDF allegato
  • Import: /invoices/import — formati supportati: CSV, Excel, PDF (regex/Claude AI), XML FatturaPA, .xml.p7m firmato, ZIP
  • Anteprima estrazione PDF: /invoices/pdf-preview

📨 Solleciti automatici (job giornaliero alle 08:00):
  • Pre-scadenza: 7/3/1 giorni prima
  • Post-scadenza: 1/7/15/30 giorni dopo
  • Tipi: pre_scadenza, sollecito_1, sollecito_2, sollecito_3, diffida formale
  • Le NC (TD04) sono escluse automaticamente

🔌 Integrazioni (per ogni utente, in /my-integrations):
  • 📁 Cartella locale: monitora ogni 30 sec, importa file XML/p7m/PDF/ZIP
  • 📧 PEC IMAP: scarica allegati ogni 5 min (Aruba: imaps.pec.aruba.it:993, Legalmail: mbox.cert.legalmail.it:993)
  • ☁️ Fatture in Cloud: OAuth ogni 30 min (Client ID/Secret configurati dall'admin)
  • 🏛 Analisi PEC istituzionali: AdE/INPS/INAIL → riassunto AI + notifiche

🔔 Notifiche scadenza al titolare (in /settings sezione "Notifiche"):
  • Email: configurata profilo
  • WhatsApp via CallMeBot: numero +34 644 51 95 23, chiede 'I allow callmebot to send me messages', restituisce API key personale
  • Quick actions nei link: invio sollecito 1-click dalla notifica

💰 Pagamenti: link Stripe/PayPal nelle email; webhook in /webhook/stripe e /webhook/paypal segna automaticamente come pagato.

👥 Multi-tenant SaaS:
  • Ogni utente vede SOLO i suoi clienti/fatture
  • Admin gestisce utenti in /users e configurazione globale (SMTP, Claude, app FiC) in /integrations e /settings
  • Le impostazioni "personali" sono in UserSetting, quelle globali in AppSettings

🆘 Assistenza: /tickets per aprire un ticket all'admin.

REGOLE DI RISPOSTA:
- Brevi e dirette. Lista numerata per istruzioni step-by-step.
- Cita pagine come `/path` quando suggerisci dove andare.
- Se la domanda è tecnica oltre le tue conoscenze (bug specifici, problemi del database, errori non documentati), suggerisci: "Apri un ticket di assistenza in /tickets/new e l'admin ti risponderà."
- Non inventare feature. Se non sei sicuro, di' "non lo so, apri un ticket".
- Lingua: italiano (anche se l'utente scrive in inglese).
"""


def chat_response(history: list[dict], user_context: dict, api_key: str,
                  model: str = "claude-haiku-4-5-20251001") -> str:
    """
    Chat assistente. `history` = [{"role":"user/assistant","content":"..."}, ...].
    Restituisce la risposta come stringa.
    """
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    ctx_str = json.dumps(user_context, indent=2, ensure_ascii=False)
    system = CHAT_SYSTEM_PROMPT + "\n\nCONTESTO UTENTE:\n" + ctx_str

    log.info("Chat AI: %d msg in storia (model=%s)", len(history), model)

    # Anthropic richiede primo msg con role=user
    if not history or history[0]["role"] != "user":
        history = [{"role": "user", "content": "Ciao"}] + history

    msg = client.messages.create(
        model=model, max_tokens=800,
        system=system,
        messages=history,
    )
    return msg.content[0].text


def analyze_pec_email(subject: str, body: str, sender: str,
                      attachments: list[str], api_key: str,
                      model: str = DEFAULT_MODEL) -> dict:
    """Analizza una PEC istituzionale via Claude e restituisce dict strutturato."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    prompt = PEC_ANALYSIS_PROMPT.format(
        sender=sender,
        subject=subject[:300],
        attachments=", ".join(attachments[:10]) or "nessuno",
        body=(body or "")[:6000],
    )

    log.info("Claude PEC analysis: %s — %s", sender, subject[:80])

    msg = client.messages.create(
        model=model, max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _strip_codefence(msg.content[0].text)
    return json.loads(raw)
