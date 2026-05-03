"""
Import fatture da CSV o Excel (.xlsx).

Colonne accettate (case-insensitive, con alias):
  nome / cliente / ragione_sociale      → nome cliente
  email                                 → email cliente
  pec                                   → PEC cliente
  piva / p_iva / partita_iva            → P.IVA cliente
  telefono / phone                      → telefono
  indirizzo / address                   → indirizzo
  numero / n_fattura / numero_fattura   → numero fattura  ← OBBLIGATORIO
  importo / totale / importo_totale     → importo         ← OBBLIGATORIO
  data_emissione / data / emissione     → data emissione  ← OBBLIGATORIO
  data_scadenza / scadenza              → data scadenza   ← OBBLIGATORIO
  link_pagamento / payment_link         → link pagamento

Date: DD/MM/YYYY  oppure  YYYY-MM-DD
"""

import csv
import io
import logging
from datetime import datetime, date

log = logging.getLogger(__name__)

# ── Alias colonne ─────────────────────────────────────────────────────────────
_ALIASES = {
    "nome":               "client_name",
    "cliente":            "client_name",
    "ragione_sociale":    "client_name",
    "denominazione":      "client_name",
    "email":              "email",
    "pec":                "pec",
    "piva":               "vat_number",
    "p_iva":              "vat_number",
    "partita_iva":        "vat_number",
    "codice_fiscale":     "vat_number",
    "telefono":           "phone",
    "phone":              "phone",
    "indirizzo":          "address",
    "address":            "address",
    "numero":             "number",
    "n_fattura":          "number",
    "numero_fattura":     "number",
    "fattura":            "number",
    "importo":            "amount",
    "totale":             "amount",
    "importo_totale":     "amount",
    "valore":             "amount",
    "data_emissione":     "issue_date",
    "data":               "issue_date",
    "emissione":          "issue_date",
    "data_scadenza":      "due_date",
    "scadenza":           "due_date",
    "pagamento_entro":    "due_date",
    "link_pagamento":     "payment_link",
    "payment_link":       "payment_link",
    "note":               "notes",
    "notes":              "notes",
}
_REQUIRED = {"client_name", "number", "amount", "issue_date", "due_date"}


def _normalize_header(h: str) -> str:
    return _ALIASES.get(h.strip().lower().replace(" ", "_"), None)


def _parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Formato data non riconosciuto: '{s}'")


def _parse_amount(s: str) -> float:
    s = s.strip().replace("€", "").replace(" ", "")
    # gestisce sia 1.234,56 che 1234.56
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    return float(s)


def _rows_from_csv(file_bytes: bytes) -> list[dict]:
    text = file_bytes.decode("utf-8-sig", errors="replace")
    # auto-detect delimiter
    sample = text[:2048]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return list(reader)


def _rows_from_excel(file_bytes: bytes) -> list[dict]:
    import openpyxl, io as _io
    wb = openpyxl.load_workbook(_io.BytesIO(file_bytes), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(h) if h is not None else "" for h in rows[0]]
    result = []
    for row in rows[1:]:
        if all(v is None for v in row):
            continue
        result.append({headers[i]: (str(v) if v is not None else "") for i, v in enumerate(row)})
    return result


def process_import(file_bytes: bytes, filename: str, db, user_id: int = None) -> tuple[int, int, list[str]]:
    """
    Importa fatture dal file.
    Restituisce (importate, saltate, lista_errori).
    """
    from models import Client, Invoice

    ext = filename.rsplit(".", 1)[-1].lower()
    if ext in ("xlsx", "xls"):
        raw_rows = _rows_from_excel(file_bytes)
    else:
        raw_rows = _rows_from_csv(file_bytes)

    if not raw_rows:
        return 0, 0, ["Il file è vuoto o non leggibile."]

    # Mappa le intestazioni
    sample_keys = list(raw_rows[0].keys())
    col_map = {}  # raw_key → field_name
    for rk in sample_keys:
        fn = _normalize_header(rk)
        if fn:
            col_map[rk] = fn

    missing = _REQUIRED - set(col_map.values())
    if missing:
        labels = {"client_name":"nome","number":"numero","amount":"importo",
                  "issue_date":"data_emissione","due_date":"data_scadenza"}
        return 0, 0, [f"Colonne obbligatorie mancanti: {', '.join(labels.get(m,m) for m in missing)}"]

    imported = skipped = 0
    errors: list[str] = []

    for i, raw in enumerate(raw_rows, start=2):
        row = {fn: raw.get(rk, "").strip() for rk, fn in col_map.items()}

        # Salta righe vuote
        if not any(row.values()):
            continue

        # Verifica campi obbligatori
        for field in _REQUIRED:
            if not row.get(field):
                errors.append(f"Riga {i}: campo '{field}' vuoto — riga saltata.")
                skipped += 1
                break
        else:
            try:
                # Fattura già esistente per QUESTO utente? (stesso numero)
                inv_q = Invoice.query.filter_by(number=row["number"])
                if user_id is not None:
                    inv_q = inv_q.filter_by(user_id=user_id)
                if inv_q.first():
                    skipped += 1
                    continue

                # Trova o crea il cliente DI QUESTO UTENTE
                client_q = Client.query.filter(Client.name.ilike(row["client_name"]))
                if user_id is not None:
                    client_q = client_q.filter_by(user_id=user_id)
                client = client_q.first()
                if not client:
                    client = Client(
                        user_id    = user_id,
                        name       = row["client_name"],
                        email      = row.get("email", ""),
                        pec        = row.get("pec", ""),
                        phone      = row.get("phone", ""),
                        address    = row.get("address", ""),
                        vat_number = row.get("vat_number", ""),
                    )
                    db.session.add(client)
                    db.session.flush()

                inv = Invoice(
                    user_id      = user_id,
                    client_id    = client.id,
                    number       = row["number"],
                    amount       = _parse_amount(row["amount"]),
                    issue_date   = _parse_date(row["issue_date"]),
                    due_date     = _parse_date(row["due_date"]),
                    payment_link = row.get("payment_link", ""),
                    notes        = row.get("notes", ""),
                )
                inv.update_status()
                db.session.add(inv)
                imported += 1

            except Exception as exc:
                errors.append(f"Riga {i}: {exc} — riga saltata.")
                skipped += 1

    db.session.commit()
    return imported, skipped, errors


# ─────────────────────────────────────────────────────────────────────────────
# IMPORT PDF — estrae i dati principali da una fattura PDF
# ─────────────────────────────────────────────────────────────────────────────
import re
import os
import shutil
from datetime import timedelta


def _extract_pdf_text(file_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
        import io as _io
        reader = PdfReader(_io.BytesIO(file_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        log.warning("Errore estrazione PDF: %s", e)
        return ""


def _find(patterns: list[str], text: str, flags=re.IGNORECASE) -> str | None:
    for p in patterns:
        m = re.search(p, text, flags)
        if m:
            return m.group(1).strip()
    return None


def _safe_date(s: str):
    """Tenta il parse della data; ritorna None se fuori range plausibile."""
    try:
        d = _parse_date(s.replace(".", "/").replace("-", "/"))
        # Range plausibile per fatture: 2000–2050
        if 2000 <= d.year <= 2050:
            return d
    except Exception:
        pass
    return None


def _find_client_block(text: str) -> tuple[str, int, int, str]:
    """
    Localizza il blocco testuale del CLIENTE.
    Gestisce 2 layout: dati DOPO il marker (formato classico) e dati PRIMA del
    marker (formato FatturaPA — pypdf estrae il blocco al contrario).

    Restituisce (testo_blocco, inizio, fine, side) dove side ∈ {"after","before"}.
    Se il marker non esiste: ("", -1, -1, "").
    """
    start_markers = [
        r"\bcessionario\s*/?\s*committente",
        r"\bspett(?:\.le|abile)\.?",
        r"\bdestinatario\s*[:\.]?",
        r"\bintestatario\s*[:\.]?",
        r"\bfattura(?:re)?\s+a\s*[:\.]?",
        r"\bcliente\s*[:\.]",
    ]
    end_markers = [
        r"\b(?:cedente|prestatore)\b",
        r"\bnumero\s+fattura\b",
        r"\bdettaglio\b",
        r"\bdescrizione\b",
        r"\bquantit[àa]\b",
        r"\briepilogo\b",
        r"\baliquot[ea]\b",
        r"\bimponibile\b",
        r"\bmodalit[àa]\s+pagamento\b",
        r"\bcondizioni\s+(?:di\s+)?pagamento\b",
        r"\btrattiamo\s+i\s+vostri\s+dati\b",
    ]

    marker = None
    for m_pat in start_markers:
        m = re.search(m_pat, text, re.IGNORECASE)
        if m:
            marker = m
            break
    if not marker:
        return "", -1, -1, ""

    m_start, m_end = marker.start(), marker.end()

    # ── BLOCCO "AFTER" (dati DOPO il marker) ─────────────────────────────────
    after_end = min(len(text), m_end + 800)
    after_block = text[m_end:after_end]
    for m_pat in end_markers:
        em = re.search(m_pat, after_block, re.IGNORECASE)
        if em:
            after_block = after_block[:em.start()]
            break
    after_start = m_end

    # ── BLOCCO "BEFORE" (dati PRIMA del marker) ──────────────────────────────
    before_start = max(0, m_start - 700)
    before_block = text[before_start:m_start]
    # Se il "before" contiene marker emittente, taglia: prendi solo dopo l'ultimo
    issuer_pats = [r"\bcedente\b", r"\bprestatore\b"]
    for m_pat in issuer_pats:
        ms = list(re.finditer(m_pat, before_block, re.IGNORECASE))
        if ms:
            cut = ms[-1].end()
            before_block  = before_block[cut:]
            before_start += cut

    # ── PUNTEGGIO: scegli il blocco con più indizi cliente ───────────────────
    def score(block: str) -> int:
        s = 0
        s += len(re.findall(r"\b\d{11}\b", block)) * 3              # P.IVA
        s += len(re.findall(r"\b\d{5}\b", block))                   # CAP
        ll = block.lower()
        for p in ("via ", "viale ", "strada ", "piazza ", "corso "):
            if p in ll: s += 2
        return s

    if score(before_block) > score(after_block):
        return before_block, before_start, m_start, "before"
    return after_block, after_start, after_start + len(after_block), "after"


def _find_client_marker(text: str) -> int:
    _, pos, _, _ = _find_client_block(text)
    return pos


def extract_invoice_data(text: str) -> dict:
    """Estrae con regex i campi principali da una fattura italiana."""
    data = {}

    # Blocco cliente (prima o dopo il marker, scelto per punteggio)
    client_block, client_start, client_end, client_side = _find_client_block(text)
    client_pos = client_start  # retrocompatibilità con il resto della funzione

    # ─── NUMERO FATTURA ──────────────────────────────────────────────────────
    num = _find([
        r"fattura\s+n\.?\s*[°º]?\s*[:\-]?\s*([A-Z0-9][\w/\-\.]{0,30})\s+del\b",
        r"fattura\s+n\.?\s*[°º]?\s*[:\-]?\s*([A-Z0-9][\w/\-\.]{0,30})",
        r"n[°º]\s*fattura\s*[:\-]?\s*([A-Z0-9][\w/\-\.]{0,30})",
        r"numero\s+(?:fattura|documento)\s*[:\-]?\s*([A-Z0-9][\w/\-\.]{0,30})",
        r"documento\s+n\.?\s*[°º]?\s*[:\-]?\s*([A-Z0-9][\w/\-\.]{0,30})",
        r"\bfattura\s+(\d+[/\-]\d+)",
    ], text)
    if num:
        data["number"] = num.rstrip(".,;:- ")

    # ─── IMPORTO TOTALE (l'ULTIMA occorrenza, è il totale finale) ────────────
    amt_patterns = [
        r"totale\s+(?:documento|da\s+pagare|fattura|generale)\s*[:\-]?\s*€?\s*([\d\.\,]+)",
        r"importo\s+totale\s*[:\-]?\s*€?\s*([\d\.\,]+)",
        r"\btotale\s*[:\-]?\s*€?\s*([\d\.\,]+)",
    ]
    for p in amt_patterns:
        matches = list(re.finditer(p, text, re.IGNORECASE))
        if matches:
            try:
                data["amount"] = _parse_amount(matches[-1].group(1))
                break
            except Exception:
                continue

    # ─── DATA EMISSIONE ──────────────────────────────────────────────────────
    issue_patterns = [
        r"\bdel\s+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})",  # "fattura n. 1/24 del 15/01/2024"
        r"data\s+(?:fattura|emissione|documento)\s*[:\-]?\s*(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})",
        r"emessa\s+il\s*[:\-]?\s*(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})",
        r"data\s*[:\-]?\s*(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})",
    ]
    for p in issue_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            d = _safe_date(m.group(1))
            if d:
                data["issue_date"] = d
                break

    # ─── DATA SCADENZA ───────────────────────────────────────────────────────
    due_patterns = [
        r"data\s+scadenza\s*[:\-]?\s*(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})",
        # FatturaPA: "Scadenze:\n2.330,00 € il 04/09/2025 - Bonifico"
        r"scadenz[ea]\s*[:\.]?[\s\S]{0,80}?\bil\s+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})",
        r"\bscadenz[ea]\s*[:\-\.]?\s*(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})",
        r"pagamento\s+entro\s*(?:il\s+)?(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})",
        r"da\s+pagare\s+entro\s*(?:il\s+)?(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})",
        r"termine\s+(?:di\s+)?pagamento\s*[:\-]?\s*(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})",
    ]
    for p in due_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            d = _safe_date(m.group(1))
            if d:
                data["due_date"] = d
                break

    # ─── ESTRAZIONE DAL BLOCCO CLIENTE ───────────────────────────────────────
    block_lines = [l.strip() for l in client_block.split("\n") if l.strip()]

    addr_prefixes = ("via ", "viale ", "v.le ", "piazza ", "p.zza ",
                     "corso ", "c.so ", "vicolo ", "largo ", "loc. ",
                     "borgo ", "strada ")
    info_prefixes = (
        "p.iva", "p iva", "piva", "partita iva", "p. iva", "vat",
        "c.f.", "c.f", "codice fiscale", "cod. fisc",
        "tel", "fax", "cell", "email", "e-mail", "pec", "telefono",
        "italia", "italy", "spett", "cliente",
        "destinatario", "intestatario", "cessionario", "committente",
        "cciaa", "rea", "iban", "banca", "trattiamo", "termini",
    )
    cap_re = re.compile(r"^\d{5}\b")  # CAP italiano

    def _is_info_line(line: str) -> bool:
        ll = line.lower().strip()
        return (
            any(ll.startswith(sp) for sp in info_prefixes) or
            bool(re.match(r"^[\d\s\-\.\/€,]+$", line))
        )

    def _is_addr_line(line: str) -> bool:
        ll = line.lower().strip()
        return any(ll.startswith(sp) for sp in addr_prefixes) or bool(cap_re.match(line))

    def _is_name_line(line: str) -> bool:
        return (
            not _is_info_line(line) and not _is_addr_line(line) and
            len(line) >= 2 and bool(re.search(r"[A-Za-zÀ-ÿ]{2,}", line)) and
            len(line) <= 100
        )

    # ─── NOME CLIENTE: gruppo più lungo di righe valide consecutive ──────────
    # (gestisce nomi su più righe come "cooperativa sociale PG Frassati scs"+"onlus")
    groups: list[list[str]] = []
    current: list[str] = []
    for line in block_lines:
        if _is_name_line(line):
            current.append(line.strip(".,;:- "))
        else:
            if current:
                groups.append(current); current = []
    if current:
        groups.append(current)

    if groups:
        # Preferisci il gruppo più grande; tie-break: lunghezza totale
        best = max(groups, key=lambda g: (len(g), sum(len(l) for l in g)))
        name = " ".join(best).strip()
        if 2 <= len(name) <= 200:
            data["client_name"] = name

    # ─── INDIRIZZO ───────────────────────────────────────────────────────────
    addr_lines = [l.strip(".,;:- ") for l in block_lines if _is_addr_line(l)]
    if addr_lines:
        # Limita a 2 righe (via + CAP/città)
        data["address"] = ", ".join(addr_lines[:2])[:200]

    # ─── P.IVA DEL CLIENTE ───────────────────────────────────────────────────
    pivas_in_block = re.findall(r"\b(\d{11})\b", client_block)
    if pivas_in_block:
        # La P.IVA cliente è quella più vicina al marker:
        #   side="before" → marker DOPO il blocco → prendi l'ULTIMA del blocco
        #   side="after"  → marker PRIMA del blocco → prendi la PRIMA del blocco
        data["vat_number"] = pivas_in_block[-1] if client_side == "before" else pivas_in_block[0]
    else:
        # Fallback: scansione globale evitando l'emittente
        all_pivas = [(m.start(), m.group(1)) for m in re.finditer(r"\b(\d{11})\b", text)]
        if all_pivas and client_start >= 0:
            distinct = []
            for _, p in all_pivas:
                if p not in distinct:
                    distinct.append(p)
            if len(distinct) >= 2:
                # Seconda P.IVA distinta = di solito il cliente
                data["vat_number"] = distinct[1]
            elif distinct:
                data["vat_number"] = distinct[0]
        elif all_pivas:
            data["vat_number"] = all_pivas[-1][1]

    return data


def process_pdf_import(file_bytes: bytes, filename: str, db, upload_folder: str,
                       user_id: int = None) -> tuple[int, int, list[str]]:
    """
    Importa una singola fattura PDF.
    Strategia:
      1. Se Anthropic API key è configurata → usa Claude (alta accuratezza).
      2. Altrimenti (o se Claude fallisce) → fallback su pypdf + regex.
    Salva il PDF in upload_folder come invoice_<id>.pdf.
    """
    from models import Client, Invoice, AppSettings, UserSetting

    data = {}
    extraction_method = "regex"

    # Identità utente (serve a distinguere fatture attive/passive)
    my_vat, my_company = "", ""
    if user_id:
        my_vat     = UserSetting.get(user_id, "my_vat_number", "")
        my_company = UserSetting.get(user_id, "company_name", "")

    # ── Tentativo 1: Claude API ──────────────────────────────────────────────
    api_key = AppSettings.get("anthropic_api_key", "")
    if api_key:
        try:
            from claude_service import extract_with_claude, DEFAULT_MODEL
            model = AppSettings.get("anthropic_model", "") or DEFAULT_MODEL
            data  = extract_with_claude(
                file_bytes, api_key, model=model,
                my_vat_number=my_vat, my_company_name=my_company,
            )
            extraction_method = "claude"
            log.info("PDF '%s' estratto via Claude API", filename)
        except Exception as e:
            log.warning("Claude API fallita per '%s': %s — uso regex", filename, e)

    # ── Tentativo 2 (fallback): pypdf + regex ────────────────────────────────
    if not data:
        text = _extract_pdf_text(file_bytes)
        if not text.strip():
            return 0, 1, [
                f"{filename}: PDF illeggibile (forse scansione). "
                "Configura Anthropic API key per leggere anche scansioni."
            ]
        data = extract_invoice_data(text)

        # Se la regex ha estratto l'UTENTE come cliente → fattura passiva,
        # cerchiamo l'altra P.IVA nel testo
        if my_vat:
            extracted = "".join(c for c in (data.get("vat_number") or "") if c.isdigit())
            my_clean = "".join(c for c in my_vat if c.isdigit())
            if extracted and extracted == my_clean:
                # Trova la prima P.IVA diversa dalla mia nel testo
                import re as _re
                others = [m.group(1) for m in _re.finditer(r"\b(\d{11})\b", text)
                          if m.group(1) != my_clean]
                if others:
                    data["vat_number"] = others[0]
                    data["client_name"] = "(VERIFICA: probabile fornitore - rilevata fattura passiva)"
                    data["_passive_warning"] = True

    # Defaults se mancano
    base = filename.rsplit(".", 1)[0]
    number = data.get("number") or f"PDF-{base[:20]}"

    # Duplicato per QUESTO utente?
    inv_q = Invoice.query.filter_by(number=number)
    if user_id is not None:
        inv_q = inv_q.filter_by(user_id=user_id)
    if inv_q.first():
        return 0, 1, [f"{filename}: numero fattura '{number}' già presente — saltata."]

    amount     = data.get("amount", 0.0)
    issue_date = data.get("issue_date") or date.today()
    due_date   = data.get("due_date")   or (issue_date + timedelta(days=30))

    client_name = data.get("client_name", f"Cliente PDF ({base[:30]})")
    vat_clean   = "".join(c for c in (data.get("vat_number") or "") if c.isdigit())

    # ── Matching: prima per P.IVA, poi fallback per nome ────────────────────
    client = None
    if vat_clean:
        client_q = Client.query.filter_by(vat_number=vat_clean)
        if user_id is not None:
            client_q = client_q.filter_by(user_id=user_id)
        client = client_q.first()
    if not client:
        client_q = Client.query.filter(Client.name.ilike(client_name))
        if user_id is not None:
            client_q = client_q.filter_by(user_id=user_id)
        client = client_q.first()

    if not client:
        client = Client(
            user_id    = user_id,
            name       = client_name,
            vat_number = vat_clean,
            address    = data.get("address", ""),
            email      = data.get("email", ""),
            pec        = data.get("pec", ""),
            phone      = data.get("phone", ""),
        )
        db.session.add(client); db.session.flush()
    else:
        # Se l'esistente ha un nome che sembra essere quello dell'UTENTE stesso
        # (vecchio bug pre-fix), aggiornalo con il nome estratto
        bad_name = (
            my_company and client.name.lower().startswith(my_company.lower()[:8])
            and not client_name.lower().startswith(my_company.lower()[:8])
        )
        if bad_name and client_name and len(client_name) > 3:
            log.info("Auto-fix nome cliente: '%s' → '%s'", client.name, client_name)
            client.name = client_name[:200]
        if vat_clean and not client.vat_number:           client.vat_number = vat_clean
        if data.get("address") and not client.address:    client.address    = data["address"]
        if data.get("email")   and not client.email:      client.email      = data["email"]
        if data.get("pec")     and not client.pec:        client.pec        = data["pec"]
        if data.get("phone")   and not client.phone:      client.phone      = data["phone"]

    inv = Invoice(
        user_id=user_id, client_id=client.id, number=number,
        amount=amount, issue_date=issue_date, due_date=due_date,
        notes=f"Importata da PDF: {filename}",
    )
    inv.update_status()
    db.session.add(inv); db.session.flush()

    # Salva il PDF nella cartella uploads
    os.makedirs(upload_folder, exist_ok=True)
    pdf_name = f"invoice_{inv.id}.pdf"
    with open(os.path.join(upload_folder, pdf_name), "wb") as fp:
        fp.write(file_bytes)
    inv.pdf_filename = pdf_name

    db.session.commit()

    method_lbl = "Claude API" if extraction_method == "claude" else "regex"
    warnings = [f"{filename}: importata come fattura n. '{number}' (via {method_lbl})."]
    missing = []
    if "amount"      not in data: missing.append("importo")
    if "client_name" not in data: missing.append("cliente")
    if "vat_number"  not in data: missing.append("P.IVA")
    if "address"     not in data: missing.append("indirizzo")
    if "issue_date"  not in data: missing.append("data emissione (usato oggi)")
    if "due_date"    not in data: missing.append("data scadenza (usato +30gg)")
    if missing:
        warnings.append(f"  ↳ campi non rilevati o stimati: {', '.join(missing)}")

    return 1, 0, warnings


# ─────────────────────────────────────────────────────────────────────────────
# IMPORT XML / P7M / ZIP — formato FatturaPA dei gestionali italiani
# ─────────────────────────────────────────────────────────────────────────────

def process_xml_import(xml_bytes: bytes, filename: str, db, upload_folder: str,
                       user_id: int = None) -> tuple[int, int, list[str]]:
    """Importa fatture da file XML FatturaPA (può contenere più fatture)."""
    from models import Client, Invoice, UserSetting
    from xml_parser import parse_fattura_pa

    # Recupera la P.IVA dell'utente per riconoscere fatture passive
    my_vat = UserSetting.get(user_id, "my_vat_number", "") if user_id else ""

    try:
        invoices_data = parse_fattura_pa(xml_bytes, my_vat_number=my_vat)
    except ValueError as e:
        return 0, 1, [f"{filename}: {e}"]

    imported = skipped = 0
    warnings: list[str] = []

    for data in invoices_data:
        if not data.get("number"):
            warnings.append(f"{filename}: numero fattura mancante — saltata.")
            skipped += 1; continue

        inv_q = Invoice.query.filter_by(number=data["number"])
        if user_id is not None:
            inv_q = inv_q.filter_by(user_id=user_id)
        if inv_q.first():
            warnings.append(f"{filename}: fattura n. '{data['number']}' già presente.")
            skipped += 1; continue

        client_name = data.get("client_name", f"Cliente XML ({filename[:30]})")
        vat_clean   = "".join(c for c in (data.get("vat_number") or "") if c.isdigit())

        # ── Matching: prima per P.IVA, poi fallback per nome ────────────────
        client = None
        if vat_clean:
            client_q = Client.query.filter_by(vat_number=vat_clean)
            if user_id is not None:
                client_q = client_q.filter_by(user_id=user_id)
            client = client_q.first()
        if not client:
            client_q = Client.query.filter(Client.name.ilike(client_name))
            if user_id is not None:
                client_q = client_q.filter_by(user_id=user_id)
            client = client_q.first()

        if not client:
            client = Client(
                user_id    = user_id,
                name       = client_name,
                vat_number = vat_clean,
                address    = data.get("address", ""),
                pec        = data.get("pec", ""),
            )
            db.session.add(client); db.session.flush()
        else:
            # Auto-fix se il vecchio nome inizia col nome dell'utente (bug pre-fix)
            my_company_xml = UserSetting.get(user_id, "company_name", "") if user_id else ""
            bad_name = (
                my_company_xml and client.name.lower().startswith(my_company_xml.lower()[:8])
                and not client_name.lower().startswith(my_company_xml.lower()[:8])
            )
            if bad_name and client_name and len(client_name) > 3:
                log.info("Auto-fix nome cliente XML: '%s' → '%s'", client.name, client_name)
                client.name = client_name[:200]
            if vat_clean and not client.vat_number:           client.vat_number = vat_clean
            if data.get("address") and not client.address:    client.address    = data["address"]
            if data.get("pec")     and not client.pec:        client.pec        = data["pec"]

        from datetime import timedelta
        issue_date = data.get("issue_date") or date.today()
        due_date   = data.get("due_date")   or (issue_date + timedelta(days=30))

        tipo_doc = data.get("tipo_documento") or "TD01"

        # Risolvi la fattura collegata (per TD04 = nota di credito)
        linked_id = None
        linked_inv = None
        linked_num = data.get("linked_invoice_number")
        if tipo_doc == "TD04" and linked_num:
            linked_inv = Invoice.query.filter_by(
                user_id=user_id, number=linked_num
            ).first() if user_id else Invoice.query.filter_by(number=linked_num).first()
            if linked_inv:
                linked_id = linked_inv.id

        inv = Invoice(
            user_id           = user_id,
            client_id         = client.id,
            number            = data["number"],
            amount            = data.get("amount", 0.0),
            issue_date        = issue_date,
            due_date          = due_date,
            document_type     = tipo_doc,
            linked_invoice_id = linked_id,
            notes             = f"Importata da XML FatturaPA: {filename}",
        )
        inv.update_status()
        db.session.add(inv); db.session.flush()

        # Se è una NC con fattura collegata, marca la fattura come compensata
        if tipo_doc == "TD04" and linked_inv and linked_inv.status not in ("paid", "compensated"):
            linked_inv.status = "compensated"
            warnings.append(
                f"  ↳ fattura n.{linked_inv.number} marcata come COMPENSATA dalla NC"
            )
        elif tipo_doc == "TD04" and linked_num and not linked_inv:
            warnings.append(
                f"  ↳ ATTENZIONE: fattura collegata n.{linked_num} non trovata nel sistema"
            )

        # Salva l'XML originale come pdf_filename (.xml accanto al PDF)
        try:
            os.makedirs(upload_folder, exist_ok=True)
            xml_name = f"invoice_{inv.id}.xml"
            with open(os.path.join(upload_folder, xml_name), "wb") as fp:
                fp.write(xml_bytes)
        except Exception:
            pass

        db.session.commit()
        imported += 1

        tipo = data.get("tipo_documento", "")
        tipo_lbl = f" [{tipo}]" if tipo and tipo != "TD01" else ""
        warnings.append(f"{filename}: importata fattura n. '{data['number']}'{tipo_lbl} via XML FatturaPA.")

    return imported, skipped, warnings


def process_p7m_import(p7m_bytes: bytes, filename: str, db, upload_folder: str,
                       user_id: int = None) -> tuple[int, int, list[str]]:
    """Estrae l'XML dal file firmato .xml.p7m e lo importa."""
    from xml_parser import extract_xml_from_p7m
    try:
        xml_bytes = extract_xml_from_p7m(p7m_bytes)
    except ValueError as e:
        return 0, 1, [f"{filename}: errore .p7m → {e}"]
    return process_xml_import(xml_bytes, filename, db, upload_folder, user_id=user_id)


def process_zip_import(zip_bytes: bytes, filename: str, db, upload_folder: str,
                       user_id: int = None) -> tuple[int, int, list[str]]:
    """Importa più fatture da archivio ZIP (XML, p7m e/o PDF mescolati)."""
    import zipfile, io as _io

    imported = skipped = 0
    errors: list[str] = []

    try:
        zf = zipfile.ZipFile(_io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return 0, 1, [f"{filename}: archivio ZIP non valido."]

    for name in zf.namelist():
        if name.endswith("/") or name.startswith("__MACOSX"):
            continue
        ext = name.rsplit(".", 1)[-1].lower()
        try:
            content = zf.read(name)
        except KeyError:
            continue

        if name.lower().endswith(".xml.p7m") or ext == "p7m":
            n_ok, n_skip, errs = process_p7m_import(content, name, db, upload_folder, user_id=user_id)
        elif ext == "xml":
            n_ok, n_skip, errs = process_xml_import(content, name, db, upload_folder, user_id=user_id)
        elif ext == "pdf":
            n_ok, n_skip, errs = process_pdf_import(content, name, db, upload_folder, user_id=user_id)
        else:
            continue

        imported += n_ok; skipped += n_skip; errors.extend(errs)

    if not imported and not errors:
        errors.append(f"{filename}: nessun file XML/p7m/PDF trovato nello ZIP.")

    return imported, skipped, errors


# Testo del CSV template da scaricare
CSV_TEMPLATE = (
    "nome;email;pec;piva;numero;importo;data_emissione;data_scadenza;link_pagamento;note\n"
    "Rossi Srl;info@rossi.it;rossi@pec.it;01234567890;2024/001;1500,00;01/01/2024;31/01/2024;;Prima fattura\n"
    "Bianchi SpA;info@bianchi.it;;09876543210;2024/002;2300,50;15/01/2024;15/02/2024;https://pay.stripe.com/xxx;\n"
)
