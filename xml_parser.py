"""
Parser per fatture elettroniche in formato FatturaPA (XML standard SDI).

Specifiche: https://www.fatturapa.gov.it/it/norme-e-regole/documentazione-fattura-elettronica/
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, date

log = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _strip_ns(tag: str) -> str:
    """Rimuove il prefisso namespace (es. '{http://...}Numero' → 'Numero')."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _find(elem, *names):
    """Primo discendente con local-name in `names`."""
    if elem is None:
        return None
    for e in elem.iter():
        if _strip_ns(e.tag) in names:
            return e
    return None


def _findall(elem, *names):
    if elem is None:
        return []
    return [e for e in elem.iter() if _strip_ns(e.tag) in names]


def _text(elem, default: str = "") -> str:
    if elem is None or elem.text is None:
        return default
    return elem.text.strip()


# ─── Parser principale FatturaPA ──────────────────────────────────────────────
def parse_fattura_pa(xml_bytes: bytes) -> list[dict]:
    """
    Parsa un XML FatturaPA. Restituisce una lista di dict (un file può aggregare
    più fatture: un solo header con `CessionarioCommittente`, più body).
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise ValueError(f"XML non valido: {e}")

    bodies = _findall(root, "FatturaElettronicaBody")
    if not bodies:
        raise ValueError("Nessun FatturaElettronicaBody trovato nel file XML")

    cessionario = _find(root, "CessionarioCommittente")
    client_data = _parse_anagrafica(cessionario) if cessionario is not None else {}

    results = []
    for body in bodies:
        inv = dict(client_data)

        # ── Dati documento ───────────────────────────────────────────────────
        dgd = _find(body, "DatiGeneraliDocumento")
        if dgd is not None:
            num = _text(_find(dgd, "Numero"))
            if num:
                inv["number"] = num

            dt = _text(_find(dgd, "Data"))
            if dt:
                try:
                    inv["issue_date"] = datetime.strptime(dt, "%Y-%m-%d").date()
                except ValueError:
                    pass

            tot = _text(_find(dgd, "ImportoTotaleDocumento"))
            if tot:
                try:
                    inv["amount"] = float(tot.replace(",", "."))
                except ValueError:
                    pass

            tipo = _text(_find(dgd, "TipoDocumento"))
            if tipo:
                inv["tipo_documento"] = tipo
                # TD04 = Nota di Credito → importo negativo
                if tipo == "TD04" and inv.get("amount", 0) > 0:
                    inv["amount"] = -inv["amount"]

        # ── Fattura collegata (per Note di Credito TD04) ─────────────────────
        # FatturaPA: <DatiFattureCollegate><IdDocumento>...</IdDocumento>
        coll = _find(body, "DatiFattureCollegate")
        if coll is not None:
            id_doc = _text(_find(coll, "IdDocumento"))
            if id_doc:
                inv["linked_invoice_number"] = id_doc.strip()

        # ── Scadenza pagamento ───────────────────────────────────────────────
        dp = _find(body, "DatiPagamento")
        if dp is not None:
            scad = _text(_find(dp, "DataScadenzaPagamento"))
            if scad:
                try:
                    inv["due_date"] = datetime.strptime(scad, "%Y-%m-%d").date()
                except ValueError:
                    pass

        results.append(inv)

    return results


def _parse_anagrafica(elem) -> dict:
    """Estrae nome, P.IVA, CF, indirizzo da CedentePrestatore o CessionarioCommittente."""
    data = {}

    # ── Denominazione / Nome+Cognome ─────────────────────────────────────────
    denom = _text(_find(elem, "Denominazione"))
    if denom:
        data["client_name"] = denom
    else:
        nome = _text(_find(elem, "Nome"))
        cogn = _text(_find(elem, "Cognome"))
        full = f"{nome} {cogn}".strip()
        if full:
            data["client_name"] = full

    # ── P.IVA ────────────────────────────────────────────────────────────────
    idfisc = _find(elem, "IdFiscaleIVA")
    if idfisc is not None:
        cod = _text(_find(idfisc, "IdCodice"))
        if cod:
            # rimuove eventuale prefisso paese tipo "IT"
            digits = "".join(c for c in cod if c.isdigit())
            data["vat_number"] = digits[:11] if len(digits) >= 11 else cod

    # ── Codice Fiscale come fallback per privati ─────────────────────────────
    if "vat_number" not in data:
        cf = _text(_find(elem, "CodiceFiscale"))
        if cf:
            data["vat_number"] = cf

    # ── Indirizzo ────────────────────────────────────────────────────────────
    sede = _find(elem, "Sede")
    if sede is not None:
        ind  = _text(_find(sede, "Indirizzo"))
        nciv = _text(_find(sede, "NumeroCivico"))
        cap  = _text(_find(sede, "CAP"))
        com  = _text(_find(sede, "Comune"))
        prov = _text(_find(sede, "Provincia"))

        line1 = (ind + (f" {nciv}" if nciv else "")).strip()
        line2 = " ".join(p for p in (cap, com) if p).strip()
        if prov:
            line2 += f" ({prov})" if line2 else f"({prov})"

        parts = [p for p in (line1, line2) if p]
        if parts:
            data["address"] = ", ".join(parts)[:200]

    # ── Email PEC del destinatario ───────────────────────────────────────────
    pec = _text(_find(elem, "PECDestinatario"))
    if pec:
        data["pec"] = pec

    return data


# ─── Estrazione XML da .p7m (PKCS#7 / CADES) ──────────────────────────────────
def extract_xml_from_p7m(p7m_bytes: bytes) -> bytes:
    """
    Estrae il payload XML da un file .xml.p7m firmato (formato CADES).
    Usa asn1crypto se possibile, altrimenti fallback con ricerca byte-level.
    """
    # ── Tentativo 1: asn1crypto (ASN.1 puro Python) ──────────────────────────
    try:
        from asn1crypto import cms
        info = cms.ContentInfo.load(p7m_bytes)
        if info["content_type"].native == "signed_data":
            signed = info["content"]
            content = signed["encap_content_info"]["content"].native
            if isinstance(content, bytes) and content.lstrip().startswith(b"<"):
                return content
    except Exception as e:
        log.debug("asn1crypto p7m extract fallita: %s", e)

    # ── Tentativo 2: ricerca diretta dei marker XML ──────────────────────────
    starts = [b"<?xml", b"<p:FatturaElettronica", b"<ns2:FatturaElettronica",
              b"<ns3:FatturaElettronica", b"<n1:FatturaElettronica",
              b"<FatturaElettronica"]
    ends   = [b"</p:FatturaElettronica>", b"</ns2:FatturaElettronica>",
              b"</ns3:FatturaElettronica>", b"</n1:FatturaElettronica>",
              b"</FatturaElettronica>"]

    start = -1
    for m in starts:
        idx = p7m_bytes.find(m)
        if idx >= 0:
            start = idx; break
    if start < 0:
        raise ValueError("XML FatturaPA non trovato nel file .p7m")

    end = -1
    for m in ends:
        idx = p7m_bytes.rfind(m)
        if idx >= 0:
            end = idx + len(m); break
    if end < 0:
        raise ValueError("Tag di chiusura FatturaElettronica non trovato")

    return p7m_bytes[start:end]
