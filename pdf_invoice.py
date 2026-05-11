"""Generazione anteprima PDF di una fattura emessa.

NB: questa è un'anteprima leggibile/stampabile NON fiscalmente valida.
Il documento valido è l'XML FatturaPA inviato al SDI. Utile per:
- stampare una copia per archivio interno
- allegarla via email al cliente (cortesia, l'XML è poco leggibile)
- verifica visiva prima dell'emissione/invio

Layout A4 portrait, font Helvetica, sezioni:
1. Header (tipo doc + numero + date + riferimento se TD04)
2. Cedente + Cessionario (due colonne)
3. Tabella righe di dettaglio
4. Cassa previdenziale + Ritenuta (se presenti)
5. Riepilogo totali (right-aligned)
6. Footer pagamento + disclaimer
"""
import io
from datetime import datetime


def _fmt_eur(v) -> str:
    """Formatta importo come '€ 1.234,56' (separatore migliaia '.', decimale ',')."""
    n = float(v or 0)
    return "€ " + f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def generate_invoice_pdf(invoice) -> bytes:
    """Genera l'anteprima PDF dell'Invoice. Ritorna bytes pronti per send_file."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                     Paragraph, Spacer)
    from models import UserSetting

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"Fattura {invoice.number}",
        author="GestFatture",
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle('body', parent=styles['Normal'], fontSize=9, leading=11)
    body_small = ParagraphStyle('body_small', parent=body, fontSize=8, leading=10)
    h1 = ParagraphStyle('h1', parent=styles['Heading1'], fontSize=16,
                        spaceAfter=4)
    foot_style = ParagraphStyle('foot', parent=body, fontSize=7,
                                textColor=colors.grey, leading=9)

    story = []

    # ─── Header ────────────────────────────────────────────────────────────
    doc_label = {
        "TD01": "FATTURA",
        "TD04": "NOTA DI CREDITO",
        "TD05": "NOTA DI DEBITO",
        "TD06": "PARCELLA",
    }.get(invoice.document_type or "TD01", "DOCUMENTO")
    story.append(Paragraph(f"<b>{doc_label}</b> N° <b>{invoice.number}</b>", h1))
    story.append(Paragraph(
        f"Data emissione: <b>{invoice.issue_date.strftime('%d/%m/%Y')}</b> "
        f" &nbsp; Scadenza: <b>{invoice.due_date.strftime('%d/%m/%Y')}</b>",
        body
    ))
    if invoice.linked_invoice:
        story.append(Paragraph(
            f"Riferimento documento originale: <b>{invoice.linked_invoice.number}</b> "
            f"del {invoice.linked_invoice.issue_date.strftime('%d/%m/%Y')}",
            body
        ))
    story.append(Spacer(1, 8))

    # ─── Cedente + Cessionario (2 colonne in box) ──────────────────────────
    uid = invoice.user_id
    ced_name = UserSetting.get(uid, "company_name") or ""
    ced_piva = UserSetting.get(uid, "my_vat_number") or ""
    ced_addr = UserSetting.get(uid, "cedente_address") or ""
    ced_cap = UserSetting.get(uid, "cedente_cap") or ""
    ced_city = UserSetting.get(uid, "cedente_city") or ""
    ced_prov = UserSetting.get(uid, "cedente_provincia") or ""
    ced_cf = UserSetting.get(uid, "cedente_codice_fiscale") or ""
    ced_block = f"<b>CEDENTE</b><br/><b>{ced_name}</b>"
    if ced_piva:
        ced_block += f"<br/>P.IVA: {ced_piva}"
    if ced_cf and ced_cf != ced_piva:
        ced_block += f"<br/>C.F.: {ced_cf}"
    if ced_addr:
        ced_block += f"<br/>{ced_addr}"
    if ced_cap or ced_city or ced_prov:
        ced_block += f"<br/>{ced_cap} {ced_city} ({ced_prov})".strip()

    c = invoice.client
    ces_block = f"<b>CESSIONARIO</b><br/><b>{c.name}</b>"
    if c.vat_number:
        ces_block += f"<br/>P.IVA: {c.vat_number}"
    if c.codice_fiscale and c.codice_fiscale != c.vat_number:
        ces_block += f"<br/>C.F.: {c.codice_fiscale}"
    if c.address:
        ces_block += f"<br/>{c.address}"
    parts_addr = " ".join(s for s in [(c.cap or ""), (c.city or ""),
                                       f"({c.provincia})" if c.provincia else ""] if s)
    if parts_addr.strip():
        ces_block += f"<br/>{parts_addr}"
    if c.codice_destinatario:
        ces_block += (f"<br/>Cod. Destinatario: "
                      f"<font face='Courier'>{c.codice_destinatario}</font>")
    if c.pec:
        ces_block += f"<br/>PEC: {c.pec}"

    parties_table = Table(
        [[Paragraph(ced_block, body_small), Paragraph(ces_block, body_small)]],
        colWidths=[doc.width / 2 - 5 * mm, doc.width / 2 - 5 * mm],
    )
    parties_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.grey),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(parties_table)
    story.append(Spacer(1, 12))

    # ─── Righe di dettaglio ───────────────────────────────────────────────
    if invoice.lines:
        rows = [["#", "Descrizione", "Q.tà", "U.M.", "Prezzo unit.", "IVA", "Tot. riga"]]
        for ln in invoice.lines:
            tot_riga = ln.quantita * ln.prezzo_unitario
            iva_label = f"{ln.aliquota_iva:g}%"
            if ln.aliquota_iva == 0 and ln.natura:
                iva_label = f"0% {ln.natura}"
            rows.append([
                str(ln.numero_linea),
                Paragraph(ln.descrizione or "", body_small),
                f"{ln.quantita:g}",
                ln.unita_misura or "",
                _fmt_eur(ln.prezzo_unitario),
                iva_label,
                _fmt_eur(tot_riga),
            ])
        lines_table = Table(
            rows,
            colWidths=[8 * mm, 75 * mm, 14 * mm, 14 * mm, 24 * mm, 18 * mm, 27 * mm],
            repeatRows=1,
        )
        lines_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#e9ecef")),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ('ALIGN', (0, 0), (0, -1), 'CENTER'),
            ('ALIGN', (2, 0), (5, -1), 'CENTER'),
            ('ALIGN', (4, 1), (4, -1), 'RIGHT'),
            ('ALIGN', (6, 0), (6, -1), 'RIGHT'),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(lines_table)
        story.append(Spacer(1, 8))

    # ─── Cassa previdenziale + Ritenuta (se presenti) ──────────────────────
    extras = []
    if invoice.cassa_tipologia and (invoice.cassa_importo or 0) > 0:
        extras.append(
            f"<b>Cassa previdenziale {invoice.cassa_tipologia}</b> "
            f"({invoice.cassa_aliquota:g}%): {_fmt_eur(invoice.cassa_importo)}"
        )
    if invoice.ritenuta_tipologia and (invoice.ritenuta_importo or 0) > 0:
        extras.append(
            f"<b>Ritenuta d'acconto {invoice.ritenuta_tipologia}</b> "
            f"({invoice.ritenuta_aliquota:g}% — causale "
            f"{invoice.ritenuta_causale}): − {_fmt_eur(invoice.ritenuta_importo)}"
        )
    for ex in extras:
        story.append(Paragraph(ex, body_small))
    if extras:
        story.append(Spacer(1, 6))

    # ─── Riepilogo totali (right-aligned) ──────────────────────────────────
    netto = float(invoice.amount or 0) - float(invoice.ritenuta_importo or 0)
    totals_rows = [
        [Paragraph("Imponibile", body), Paragraph(_fmt_eur(invoice.imponibile or 0), body)],
    ]
    if (invoice.cassa_importo or 0) > 0:
        totals_rows.append([
            Paragraph("+ Cassa", body),
            Paragraph(_fmt_eur(invoice.cassa_importo), body),
        ])
    totals_rows.append([
        Paragraph("+ IVA", body),
        Paragraph(_fmt_eur(invoice.iva_amount or 0), body),
    ])
    totals_rows.append([
        Paragraph("<b>Totale documento</b>", body),
        Paragraph(f"<b>{_fmt_eur(invoice.amount)}</b>", body),
    ])
    if (invoice.ritenuta_importo or 0) > 0:
        totals_rows.append([
            Paragraph("<font color='#dc3545'>− Ritenuta</font>", body),
            Paragraph(f"<font color='#dc3545'>− {_fmt_eur(invoice.ritenuta_importo)}</font>", body),
        ])
        totals_rows.append([
            Paragraph("<b><font color='#198754'>Netto a pagare</font></b>", body),
            Paragraph(f"<b><font color='#198754'>{_fmt_eur(netto)}</font></b>", body),
        ])
    totals_table = Table(totals_rows, colWidths=[45 * mm, 35 * mm])
    totals_table.setStyle(TableStyle([
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('LINEABOVE', (0, 3), (-1, 3), 0.5, colors.black),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    # Allinea il blocco totali a destra
    outer = Table(
        [[None, totals_table]],
        colWidths=[doc.width - 80 * mm, 80 * mm],
    )
    outer.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(outer)
    story.append(Spacer(1, 12))

    # ─── Pagamento + Footer ───────────────────────────────────────────────
    story.append(Paragraph(
        "<b>Modalità di pagamento</b>: bonifico bancario (MP05)", body_small
    ))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"<i>Anteprima generata il {datetime.now().strftime('%d/%m/%Y %H:%M')} "
        f"da GestFatture (gestfatture.com). <b>Documento NON fiscalmente valido</b> — "
        f"il documento valido è l'XML FatturaPA inviato al Sistema di Interscambio (SDI). "
        f"Questa anteprima serve a fini di archivio interno e comunicazione al cliente.</i>",
        foot_style
    ))

    doc.build(story)
    return buf.getvalue()
