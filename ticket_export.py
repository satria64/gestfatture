"""Export ticket assistenza in CSV / PDF."""
import csv
import io
from datetime import datetime


def tickets_to_csv(tickets, include_messages: bool = True) -> str:
    """Restituisce stringa CSV con i ticket. Una riga per ticket;
    i messaggi se inclusi vanno in una colonna concatenata."""
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    headers = ["ID", "Aperto il", "Aggiornato il", "Utente", "Stato",
               "Priorita", "Categoria", "Oggetto"]
    if include_messages:
        headers.append("Messaggi (concat)")
    w.writerow(headers)
    for t in tickets:
        row = [
            t.id,
            t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "",
            t.updated_at.strftime("%Y-%m-%d %H:%M") if t.updated_at else "",
            t.user.username if t.user else "",
            t.status,
            t.priority,
            t.category,
            t.subject,
        ]
        if include_messages:
            msgs = []
            for m in (t.messages or []):
                if m.is_internal:
                    continue
                author = m.author.username if m.author else "?"
                ts = m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else ""
                msgs.append(f"[{ts}] {author}: {m.body}")
            row.append("\n---\n".join(msgs))
        w.writerow(row)
    return buf.getvalue()


def tickets_to_pdf(tickets, title: str = "Ticket di assistenza") -> bytes:
    """Genera un PDF tabellare con la lista ticket. Restituisce bytes."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                     Paragraph, Spacer)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
    )
    styles = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=styles["BodyText"],
                          fontSize=8, leading=10, wordWrap="CJK")
    cell_subj = ParagraphStyle("cell_subj", parent=cell, fontSize=8.5,
                                fontName="Helvetica-Bold")
    elements = []

    elements.append(Paragraph(f"<b>{title}</b>", styles["Title"]))
    elements.append(Paragraph(
        f"Generato il {datetime.now().strftime('%d/%m/%Y %H:%M')} · "
        f"{len(tickets)} ticket totali",
        styles["Italic"]))
    elements.append(Spacer(1, 6 * mm))

    head = ["ID", "Aperto", "Utente", "Stato", "Pri", "Cat", "Oggetto", "Aggiornato"]
    data = [head]
    for t in tickets:
        data.append([
            Paragraph(str(t.id), cell),
            Paragraph(t.created_at.strftime("%d/%m/%Y") if t.created_at else "", cell),
            Paragraph(t.user.username if t.user else "", cell),
            Paragraph(t.status, cell),
            Paragraph(t.priority, cell),
            Paragraph(t.category, cell),
            Paragraph(t.subject[:200], cell_subj),
            Paragraph(t.updated_at.strftime("%d/%m/%Y") if t.updated_at else "", cell),
        ])
    col_widths = [12 * mm, 22 * mm, 28 * mm, 22 * mm, 18 * mm, 22 * mm,
                  None, 22 * mm]  # None = espandi
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a5f")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, 0), 9),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
            [colors.white, colors.HexColor("#f8fafc")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(tbl)
    doc.build(elements)
    return buf.getvalue()
