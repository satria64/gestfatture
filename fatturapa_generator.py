"""
Generatore XML FatturaPA versione 1.2.x conforme alle specifiche AdE.

Riferimento: https://www.fatturapa.gov.it/it/norme-e-regole/DocumentiTecnici/

MVP: supporta TD01 (fattura) B2B con singola aliquota IVA per riga, una sola
aliquota di riepilogo (es. tutto al 22%), pagamento bonifico (MP05).
Estensioni future:
- TD04 Nota di credito (con riferimento a fattura originale)
- TD06 Parcella professionista
- Multi-aliquota IVA
- Ritenuta d'acconto / cassa previdenziale
- Esenzioni IVA (Natura N1-N7)
- Persona fisica (Nome+Cognome invece di Denominazione)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from xml.etree import ElementTree as ET


# ─── Schema namespace ──────────────────────────────────────────────────────
NS_P = "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"

ET.register_namespace("p", NS_P)
ET.register_namespace("ds", NS_DS)
ET.register_namespace("xsi", NS_XSI)


# ─── Strutture dati ────────────────────────────────────────────────────────
@dataclass
class Cedente:
    """Chi emette la fattura (l'utente GestFatture)."""
    piva: str                        # 11 cifre, senza prefisso paese
    codice_fiscale: str = ""         # 16 char persona fisica o uguale a P.IVA
    denominazione: str = ""          # ragione sociale (società)
    nome: str = ""                   # solo persona fisica
    cognome: str = ""                # solo persona fisica
    is_persona_fisica: bool = False  # True = usa Nome+Cognome, False = Denominazione
    indirizzo: str = ""              # via e numero civico
    cap: str = ""                    # 5 cifre
    comune: str = ""                 # città
    provincia: str = ""              # 2 char
    nazione: str = "IT"              # ISO 3166-1 alpha-2
    regime_fiscale: str = "RF01"     # RF01-RF19


@dataclass
class Cessionario:
    """Chi riceve la fattura (il cliente)."""
    piva: str = ""                   # se presente, in IdFiscaleIVA
    codice_fiscale: str = ""         # opzionale
    denominazione: str = ""          # ragione sociale
    nome: str = ""
    cognome: str = ""
    is_persona_fisica: bool = False
    indirizzo: str = ""
    cap: str = ""
    comune: str = ""
    provincia: str = ""
    nazione: str = "IT"
    codice_destinatario: str = "0000000"  # 7 char SDI o "0000000"
    pec_destinatario: str = ""            # obbligatoria se codice_destinatario="0000000"


@dataclass
class Riga:
    """Singola riga di dettaglio fattura."""
    descrizione: str
    quantita: float = 1.0
    prezzo_unitario: float = 0.0      # in euro, 2 decimali
    aliquota_iva: float = 22.0        # in %, es. 22.0
    unita_misura: str = ""            # opzionale, es. "ore", "pz"

    @property
    def prezzo_totale(self) -> float:
        return round(self.quantita * self.prezzo_unitario, 2)


@dataclass
class Fattura:
    """Una fattura emessa."""
    numero: str                       # es. "1/2026"
    data: date                        # data emissione
    cedente: Cedente
    cessionario: Cessionario
    righe: list[Riga] = field(default_factory=list)
    tipo_documento: str = "TD01"      # TD01=fattura
    divisa: str = "EUR"
    progressivo_invio: str = "00001"  # progressivo per anno
    causale: str = ""                 # opzionale
    data_scadenza: date | None = None
    modalita_pagamento: str = "MP05"  # MP05 = bonifico


# ─── Helpers ───────────────────────────────────────────────────────────────
def _fmt_amount(value: float) -> str:
    """Formatta importo a 2 decimali con punto come separatore."""
    return f"{value:.2f}"


def _fmt_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _add(parent: ET.Element, tag: str, text=None) -> ET.Element:
    """Crea un sub-elemento con testo. Se text è None, crea elemento vuoto."""
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def _validate(fattura: Fattura) -> list[str]:
    """Validazione di base. Restituisce lista di errori o lista vuota."""
    errors = []
    c = fattura.cedente
    if not (c.piva and len(c.piva) == 11 and c.piva.isdigit()):
        errors.append("Cedente: P.IVA mancante o non valida (11 cifre).")
    if c.is_persona_fisica:
        if not (c.nome and c.cognome):
            errors.append("Cedente persona fisica: Nome e Cognome obbligatori.")
    else:
        if not c.denominazione:
            errors.append("Cedente società: Denominazione obbligatoria.")
    if not c.indirizzo:    errors.append("Cedente: indirizzo obbligatorio.")
    if not c.cap:          errors.append("Cedente: CAP obbligatorio.")
    if not c.comune:       errors.append("Cedente: Comune obbligatorio.")
    if not c.provincia or len(c.provincia) != 2:
        errors.append("Cedente: Provincia obbligatoria (2 lettere).")

    cs = fattura.cessionario
    if not (cs.denominazione or (cs.nome and cs.cognome)):
        errors.append("Cessionario: Denominazione (o Nome+Cognome) obbligatori.")
    if not cs.indirizzo:   errors.append("Cessionario: indirizzo obbligatorio.")
    if not cs.cap:         errors.append("Cessionario: CAP obbligatorio.")
    if not cs.comune:      errors.append("Cessionario: Comune obbligatorio.")
    if not cs.provincia or len(cs.provincia) != 2:
        errors.append("Cessionario: Provincia obbligatoria (2 lettere).")
    cd = (cs.codice_destinatario or "").strip().upper()
    if len(cd) != 7:
        errors.append("Cessionario: Codice destinatario SDI deve essere 7 caratteri (usa '0000000' + PEC se privato).")
    if cd == "0000000" and not cs.pec_destinatario:
        errors.append("Cessionario: con CodiceDestinatario=0000000 serve la PEC.")

    if not fattura.righe:
        errors.append("Almeno una riga di dettaglio è obbligatoria.")
    for i, r in enumerate(fattura.righe, start=1):
        if not r.descrizione:
            errors.append(f"Riga {i}: descrizione obbligatoria.")
        if r.quantita <= 0:
            errors.append(f"Riga {i}: quantità deve essere > 0.")
        if r.prezzo_unitario < 0:
            errors.append(f"Riga {i}: prezzo unitario non può essere negativo.")

    if not fattura.numero:
        errors.append("Numero fattura obbligatorio.")
    if not fattura.data:
        errors.append("Data fattura obbligatoria.")

    return errors


# ─── Building blocks ───────────────────────────────────────────────────────
def _build_dati_trasmissione(parent: ET.Element, f: Fattura):
    dt = ET.SubElement(parent, "DatiTrasmissione")
    idt = ET.SubElement(dt, "IdTrasmittente")
    _add(idt, "IdPaese", "IT")
    _add(idt, "IdCodice", f.cedente.piva)
    _add(dt, "ProgressivoInvio", f.progressivo_invio)
    _add(dt, "FormatoTrasmissione", "FPR12")  # FPR12 = privati B2B/B2C; FPA12 = PA
    _add(dt, "CodiceDestinatario", f.cessionario.codice_destinatario)
    if f.cessionario.codice_destinatario == "0000000" and f.cessionario.pec_destinatario:
        _add(dt, "PECDestinatario", f.cessionario.pec_destinatario)


def _build_anagrafica_cedente(parent: ET.Element, c: Cedente):
    cp = ET.SubElement(parent, "CedentePrestatore")
    da = ET.SubElement(cp, "DatiAnagrafici")
    idi = ET.SubElement(da, "IdFiscaleIVA")
    _add(idi, "IdPaese", c.nazione or "IT")
    _add(idi, "IdCodice", c.piva)
    if c.codice_fiscale:
        _add(da, "CodiceFiscale", c.codice_fiscale)
    anag = ET.SubElement(da, "Anagrafica")
    if c.is_persona_fisica:
        _add(anag, "Nome",    c.nome)
        _add(anag, "Cognome", c.cognome)
    else:
        _add(anag, "Denominazione", c.denominazione)
    _add(da, "RegimeFiscale", c.regime_fiscale or "RF01")
    sede = ET.SubElement(cp, "Sede")
    _add(sede, "Indirizzo", c.indirizzo)
    _add(sede, "CAP",       c.cap)
    _add(sede, "Comune",    c.comune)
    if c.provincia:
        _add(sede, "Provincia", c.provincia.upper())
    _add(sede, "Nazione", c.nazione or "IT")


def _build_anagrafica_cessionario(parent: ET.Element, cs: Cessionario):
    cc = ET.SubElement(parent, "CessionarioCommittente")
    da = ET.SubElement(cc, "DatiAnagrafici")
    if cs.piva:
        idi = ET.SubElement(da, "IdFiscaleIVA")
        _add(idi, "IdPaese", cs.nazione or "IT")
        _add(idi, "IdCodice", cs.piva)
    if cs.codice_fiscale:
        _add(da, "CodiceFiscale", cs.codice_fiscale)
    anag = ET.SubElement(da, "Anagrafica")
    if cs.is_persona_fisica:
        _add(anag, "Nome",    cs.nome)
        _add(anag, "Cognome", cs.cognome)
    else:
        _add(anag, "Denominazione", cs.denominazione)
    sede = ET.SubElement(cc, "Sede")
    _add(sede, "Indirizzo", cs.indirizzo)
    _add(sede, "CAP",       cs.cap)
    _add(sede, "Comune",    cs.comune)
    if cs.provincia:
        _add(sede, "Provincia", cs.provincia.upper())
    _add(sede, "Nazione", cs.nazione or "IT")


def _build_dati_generali(parent: ET.Element, f: Fattura, totale: float):
    dg = ET.SubElement(parent, "DatiGenerali")
    dgd = ET.SubElement(dg, "DatiGeneraliDocumento")
    _add(dgd, "TipoDocumento",          f.tipo_documento)
    _add(dgd, "Divisa",                 f.divisa)
    _add(dgd, "Data",                   _fmt_date(f.data))
    _add(dgd, "Numero",                 f.numero)
    _add(dgd, "ImportoTotaleDocumento", _fmt_amount(totale))
    if f.causale:
        _add(dgd, "Causale", f.causale[:200])


def _build_dati_beni_servizi(parent: ET.Element, f: Fattura):
    dbs = ET.SubElement(parent, "DatiBeniServizi")
    # Dettaglio righe
    for i, r in enumerate(f.righe, start=1):
        dl = ET.SubElement(dbs, "DettaglioLinee")
        _add(dl, "NumeroLinea",     i)
        _add(dl, "Descrizione",     r.descrizione)
        _add(dl, "Quantita",        f"{r.quantita:.2f}")
        if r.unita_misura:
            _add(dl, "UnitaMisura", r.unita_misura)
        _add(dl, "PrezzoUnitario",  _fmt_amount(r.prezzo_unitario))
        _add(dl, "PrezzoTotale",    _fmt_amount(r.prezzo_totale))
        _add(dl, "AliquotaIVA",     _fmt_amount(r.aliquota_iva))

    # Riepilogo: aggrega righe per aliquota IVA
    aliquote = {}  # {aliquota: imponibile}
    for r in f.righe:
        aliquote.setdefault(r.aliquota_iva, 0.0)
        aliquote[r.aliquota_iva] += r.prezzo_totale
    for aliquota, imponibile in sorted(aliquote.items()):
        imponibile = round(imponibile, 2)
        imposta = round(imponibile * aliquota / 100.0, 2)
        dr = ET.SubElement(dbs, "DatiRiepilogo")
        _add(dr, "AliquotaIVA",        _fmt_amount(aliquota))
        _add(dr, "ImponibileImporto",  _fmt_amount(imponibile))
        _add(dr, "Imposta",            _fmt_amount(imposta))
        _add(dr, "EsigibilitaIVA",     "I")  # I = immediata


def _build_dati_pagamento(parent: ET.Element, f: Fattura, totale: float):
    dp = ET.SubElement(parent, "DatiPagamento")
    _add(dp, "CondizioniPagamento", "TP02")  # TP02 = pagamento completo (no rate)
    dett = ET.SubElement(dp, "DettaglioPagamento")
    _add(dett, "ModalitaPagamento", f.modalita_pagamento or "MP05")
    if f.data_scadenza:
        _add(dett, "DataScadenzaPagamento", _fmt_date(f.data_scadenza))
    _add(dett, "ImportoPagamento", _fmt_amount(totale))


# ─── API pubblica ──────────────────────────────────────────────────────────
def generate_xml(fattura: Fattura) -> str:
    """Genera l'XML FatturaPA 1.2.x come stringa.
    Solleva ValueError con elenco errori se la fattura non è valida."""
    errors = _validate(fattura)
    if errors:
        raise ValueError("FatturaPA non valida:\n- " + "\n- ".join(errors))

    # Calcolo totale documento (imponibile + IVA)
    totale = 0.0
    for r in fattura.righe:
        imponibile_riga = r.prezzo_totale
        iva_riga = imponibile_riga * r.aliquota_iva / 100.0
        totale += imponibile_riga + iva_riga
    totale = round(totale, 2)

    # Root element con namespace
    root = ET.Element("{%s}FatturaElettronica" % NS_P, attrib={
        "versione": "FPR12",
        "{%s}schemaLocation" % NS_XSI:
            "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2 "
            "http://www.fatturapa.gov.it/export/fatturazione/sdi/fatturapa/v1.2/Schema_del_file_xml_FatturaPA_versione_1.2.xsd",
    })

    header = ET.SubElement(root, "FatturaElettronicaHeader")
    _build_dati_trasmissione(header, fattura)
    _build_anagrafica_cedente(header, fattura.cedente)
    _build_anagrafica_cessionario(header, fattura.cessionario)

    body = ET.SubElement(root, "FatturaElettronicaBody")
    _build_dati_generali(body, fattura, totale)
    _build_dati_beni_servizi(body, fattura)
    _build_dati_pagamento(body, fattura, totale)

    # Pretty print
    ET.indent(root, space="  ", level=0)
    xml_bytes = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    return xml_bytes.decode("UTF-8")


def make_filename(piva_cedente: str, progressivo: str) -> str:
    """Nome file FatturaPA: ITxxxxxxxxxxx_xxxxx.xml
    progressivo è alfanumerico max 5 caratteri (encodato in base 36 dall'app)."""
    code = (piva_cedente or "").strip().zfill(11)
    prog = (progressivo or "").strip().zfill(5)[:5]
    return f"IT{code}_{prog}.xml"


def encode_progressivo(num: int) -> str:
    """Converte un intero in 5 caratteri base36 (uppercase). Max 60.466.175."""
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if num < 0:
        num = 0
    out = ""
    n = num
    for _ in range(5):
        out = chars[n % 36] + out
        n //= 36
    return out
