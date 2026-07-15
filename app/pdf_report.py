from __future__ import annotations

import html
import io
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


GREEN = colors.HexColor("#176B4D")
GREEN_SOFT = colors.HexColor("#E4F2EB")
INK = colors.HexColor("#17211C")
MUTED = colors.HexColor("#68736D")
LINE = colors.HexColor("#DFE5E1")
WASH = colors.HexColor("#F3F6F4")
RED = colors.HexColor("#A33C32")
AMBER = colors.HexColor("#93651B")


def money(cents, currency="EUR"):
    amount = (cents or 0) / 100
    value = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{value} {currency}"


def date_de(value):
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return value


def build_year_report_data(db, organization_id, year, closure=None):
    organization = db.execute(
        "SELECT name FROM organizations WHERE id=?", (organization_id,)
    ).fetchone()
    totals = db.execute(
        """SELECT COUNT(*) transaction_count,
                  COALESCE(SUM(CASE WHEN amount_cents>0 THEN amount_cents ELSE 0 END),0) income,
                  COALESCE(SUM(CASE WHEN amount_cents<0 THEN -amount_cents ELSE 0 END),0) expenses,
                  COALESCE(SUM(amount_cents),0) balance,
                  COALESCE(SUM(receipt_status='missing'),0) missing,
                  COALESCE(SUM(receipt_status='complete'),0) complete,
                  COALESCE(SUM(receipt_status='not_required'),0) not_required,
                  COALESCE(SUM(category_id IS NULL AND NOT EXISTS(
                      SELECT 1 FROM transaction_splits s WHERE s.transaction_id=transactions.id
                  )),0) uncategorized
           FROM transactions WHERE substr(booking_date,1,4)=?""",
        (year,),
    ).fetchone()
    allocations_sql = """
        WITH allocations AS (
            SELECT t.id transaction_id,t.amount_cents,c.name category_name,c.tax_area
            FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
            WHERE substr(t.booking_date,1,4)=?
              AND NOT EXISTS(SELECT 1 FROM transaction_splits s WHERE s.transaction_id=t.id)
            UNION ALL
            SELECT t.id,s.amount_cents,c.name,c.tax_area
            FROM transaction_splits s JOIN transactions t ON t.id=s.transaction_id
            JOIN categories c ON c.id=s.category_id
            WHERE substr(t.booking_date,1,4)=?
        )
    """
    categories = db.execute(
        allocations_sql
        + """SELECT COALESCE(category_name,'Ohne Kategorie') name,
                    COALESCE(tax_area,'Nicht zugeordnet') tax_area,
                    COUNT(DISTINCT transaction_id) transaction_count,
                    SUM(CASE WHEN amount_cents>0 THEN amount_cents ELSE 0 END) income,
                    -SUM(CASE WHEN amount_cents<0 THEN amount_cents ELSE 0 END) expenses,
                    SUM(amount_cents) balance
             FROM allocations GROUP BY category_name,tax_area
             ORDER BY tax_area,name""",
        (year, year),
    ).fetchall()
    tax_areas = db.execute(
        allocations_sql
        + """SELECT COALESCE(tax_area,'Nicht zugeordnet') name,
                    SUM(CASE WHEN amount_cents>0 THEN amount_cents ELSE 0 END) income,
                    -SUM(CASE WHEN amount_cents<0 THEN amount_cents ELSE 0 END) expenses,
                    SUM(amount_cents) balance
             FROM allocations GROUP BY tax_area ORDER BY name""",
        (year, year),
    ).fetchall()

    accounts = []
    for account in db.execute(
        """SELECT * FROM accounts WHERE organization_id=? AND (
               active=1 OR EXISTS(SELECT 1 FROM transactions t
                   WHERE t.account_id=accounts.id AND substr(t.booking_date,1,4)=?)
           ) ORDER BY kind,name""",
        (organization_id, year),
    ).fetchall():
        opening = db.execute(
            """SELECT ? + COALESCE(SUM(amount_cents),0) FROM transactions
               WHERE account_id=? AND booking_date<?""",
            (account["opening_balance_cents"], account["id"], f"{year}-01-01"),
        ).fetchone()[0]
        closing = db.execute(
            """SELECT ? + COALESCE(SUM(amount_cents),0) FROM transactions
               WHERE account_id=? AND booking_date<=?""",
            (account["opening_balance_cents"], account["id"], f"{year}-12-31"),
        ).fetchone()[0]
        reconciliation = db.execute(
            """SELECT * FROM account_reconciliations
               WHERE account_id=? AND substr(balance_date,1,4)=?
                 AND ((kind='bank_statement' AND balance_type IN ('CLBD','ITBD'))
                      OR kind='cash_count')
               ORDER BY balance_date DESC,
                        CASE balance_type WHEN 'CLBD' THEN 0 ELSE 1 END,id DESC LIMIT 1""",
            (account["id"], year),
        ).fetchone()
        reported = difference = None
        reconciliation_date = None
        if reconciliation:
            calculated_at_date = db.execute(
                """SELECT ? + COALESCE(SUM(amount_cents),0) FROM transactions
                   WHERE account_id=? AND booking_date<=?""",
                (
                    account["opening_balance_cents"],
                    account["id"],
                    reconciliation["balance_date"],
                ),
            ).fetchone()[0]
            reported = reconciliation["balance_cents"]
            difference = reported - calculated_at_date
            reconciliation_date = reconciliation["balance_date"]
        accounts.append(
            {
                "name": account["name"],
                "kind": account["kind"],
                "iban": account["iban"],
                "currency": account["currency"],
                "opening": opening,
                "closing": closing,
                "reported": reported,
                "difference": difference,
                "reconciliation_date": reconciliation_date,
            }
        )

    transactions = []
    rows = db.execute(
        """SELECT t.*,a.name account_name,c.name category_name,
                  (SELECT COUNT(*) FROM attachments x WHERE x.transaction_id=t.id) attachment_count,
                  (SELECT kind FROM transaction_adjustments x WHERE x.original_transaction_id=t.id) adjustment_kind,
                  CASE
                    WHEN EXISTS(SELECT 1 FROM transaction_adjustments x WHERE x.reversal_transaction_id=t.id) THEN 'reversal'
                    WHEN EXISTS(SELECT 1 FROM transaction_adjustments x WHERE x.replacement_transaction_id=t.id) THEN 'replacement'
                  END adjustment_role
           FROM transactions t LEFT JOIN accounts a ON a.id=t.account_id
           LEFT JOIN categories c ON c.id=t.category_id
           WHERE substr(t.booking_date,1,4)=? ORDER BY t.booking_date,t.id""",
        (year,),
    ).fetchall()
    for row in rows:
        splits = db.execute(
            """SELECT s.amount_cents,c.name FROM transaction_splits s
               JOIN categories c ON c.id=s.category_id
               WHERE s.transaction_id=? ORDER BY s.id""",
            (row["id"],),
        ).fetchall()
        category = row["category_name"] or "Ohne Kategorie"
        if splits:
            category = "; ".join(f"{item['name']} {money(item['amount_cents'])}" for item in splits)
        transactions.append(
            {
                **dict(row),
                "category_display": category,
                "description": row["counterparty"] or row["purpose"] or "Buchung",
            }
        )
    adjustments = db.execute(
        """SELECT x.* FROM transaction_adjustments x
           JOIN transactions t ON t.id=x.original_transaction_id
           WHERE substr(t.booking_date,1,4)=? ORDER BY x.id""",
        (year,),
    ).fetchall()
    return {
        "organization": organization["name"],
        "year": year,
        "is_final": closure is not None,
        "closed_at": closure["closed_at"] if closure else None,
        "summary_hash": closure["summary_hash"] if closure else None,
        "generated_at": closure["closed_at"] if closure else datetime.now(timezone.utc).isoformat(),
        "totals": dict(totals),
        "categories": [dict(row) for row in categories],
        "tax_areas": [dict(row) for row in tax_areas],
        "accounts": accounts,
        "transactions": transactions,
        "adjustments": [dict(row) for row in adjustments],
    }


def render_year_report(data):
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=24 * mm,
        bottomMargin=19 * mm,
        title=f"Kassenbericht {data['year']} - {data['organization']}",
        author=data["organization"],
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=22, leading=26, textColor=INK, spaceAfter=4 * mm))
    styles.add(ParagraphStyle(name="ReportIntro", parent=styles["BodyText"], fontSize=9, leading=13, textColor=MUTED, spaceAfter=5 * mm))
    styles.add(ParagraphStyle(name="Section", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=INK, spaceBefore=6 * mm, spaceAfter=3 * mm))
    styles.add(ParagraphStyle(name="Cell", parent=styles["BodyText"], fontSize=7.2, leading=9, textColor=INK))
    styles.add(ParagraphStyle(name="CellSmall", parent=styles["BodyText"], fontSize=6.5, leading=8, textColor=MUTED))
    styles.add(ParagraphStyle(name="CellRight", parent=styles["Cell"], alignment=TA_RIGHT))
    styles.add(ParagraphStyle(name="MetricLabel", parent=styles["BodyText"], fontSize=8, textColor=MUTED))
    styles.add(ParagraphStyle(name="MetricValue", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=14, leading=17, textColor=INK))

    def paragraph(value, style="Cell"):
        return Paragraph(html.escape(str(value or "-")), styles[style])

    def report_table(rows, widths, numeric_columns=()):
        table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
        commands = [
            ("BACKGROUND", (0, 0), (-1, 0), GREEN),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 7),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.35, LINE),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, WASH]),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 1), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
        ]
        for column in numeric_columns:
            commands.append(("ALIGN", (column, 1), (column, -1), "RIGHT"))
        table.setStyle(TableStyle(commands))
        return table

    def page_frame(canvas, doc):
        canvas.saveState()
        width, height = A4
        canvas.setFillColor(GREEN)
        canvas.roundRect(16 * mm, height - 17 * mm, 8 * mm, 8 * mm, 2 * mm, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawCentredString(20 * mm, height - 14.1 * mm, "V")
        canvas.setFillColor(INK)
        canvas.setFont("Helvetica-Bold", 8.5)
        canvas.drawString(27 * mm, height - 13.8 * mm, data["organization"])
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawRightString(width - 16 * mm, height - 13.8 * mm, f"Kassenbericht {data['year']}")
        canvas.setStrokeColor(LINE)
        canvas.line(16 * mm, 14 * mm, width - 16 * mm, 14 * mm)
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica", 7)
        canvas.drawString(16 * mm, 9.5 * mm, "Erstellt mit Vereinskasse")
        canvas.drawRightString(width - 16 * mm, 9.5 * mm, f"Seite {doc.page}")
        canvas.restoreState()

    story = [
        Spacer(1, 4 * mm),
        Paragraph(f"Kassenbericht {html.escape(data['year'])}", styles["ReportTitle"]),
        Paragraph(
            ("Endgültige Fassung" if data["is_final"] else "Entwurf - Geschäftsjahr noch nicht abgeschlossen")
            + f"<br/>Stand: {date_de(data['generated_at'])}",
            styles["ReportIntro"],
        ),
    ]
    totals = data["totals"]
    metric_cells = []
    for label, value, color in (
        ("Einnahmen", money(totals["income"]), GREEN),
        ("Ausgaben", money(totals["expenses"]), RED),
        ("Ergebnis", money(totals["balance"]), INK),
        ("Buchungen", str(totals["transaction_count"]), INK),
    ):
        metric_cells.append(
            Table(
                [[Paragraph(label, styles["MetricLabel"])], [Paragraph(value, ParagraphStyle("MetricColor", parent=styles["MetricValue"], textColor=color))]],
                colWidths=[42 * mm],
                rowHeights=[7 * mm, 11 * mm],
                style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), WASH), ("BOX", (0, 0), (-1, -1), 0.5, LINE), ("LEFTPADDING", (0, 0), (-1, -1), 6), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]),
            )
        )
    story.extend([Table([metric_cells], colWidths=[44.5 * mm] * 4, style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2)])), Spacer(1, 3 * mm)])

    completeness = (
        f"Belege: {totals['complete']} vollständig, {totals['not_required']} nicht erforderlich, "
        f"{totals['missing']} fehlend. Buchungen ohne Kategorie: {totals['uncategorized']}."
    )
    story.append(Paragraph(completeness, styles["ReportIntro"]))

    story.append(Paragraph("Konten und Kassenabgleich", styles["Section"]))
    account_rows = [["Konto", "Typ", "Anfang", "Ende", "Gemeldet", "Abweichung"]]
    for account in data["accounts"]:
        account_rows.append(
            [
                paragraph(account["name"]),
                paragraph("Barkasse" if account["kind"] == "cash" else "Bank"),
                paragraph(money(account["opening"], account["currency"]), "CellRight"),
                paragraph(money(account["closing"], account["currency"]), "CellRight"),
                paragraph(money(account["reported"], account["currency"]) if account["reported"] is not None else "-", "CellRight"),
                paragraph(money(account["difference"], account["currency"]) if account["difference"] is not None else "-", "CellRight"),
            ]
        )
    story.append(report_table(account_rows, [42 * mm, 18 * mm, 29 * mm, 29 * mm, 31 * mm, 29 * mm], (2, 3, 4, 5)))

    story.append(Paragraph("Auswertung nach Steuerbereich", styles["Section"]))
    tax_rows = [["Steuerbereich", "Einnahmen", "Ausgaben", "Ergebnis"]]
    for row in data["tax_areas"]:
        tax_rows.append([paragraph(row["name"]), paragraph(money(row["income"]), "CellRight"), paragraph(money(row["expenses"]), "CellRight"), paragraph(money(row["balance"]), "CellRight")])
    story.append(report_table(tax_rows, [76 * mm, 34 * mm, 34 * mm, 34 * mm], (1, 2, 3)))

    story.append(Paragraph("Auswertung nach Kategorie", styles["Section"]))
    category_rows = [["Kategorie", "Steuerbereich", "Einnahmen", "Ausgaben", "Ergebnis"]]
    for row in data["categories"]:
        category_rows.append([paragraph(row["name"]), paragraph(row["tax_area"], "CellSmall"), paragraph(money(row["income"]), "CellRight"), paragraph(money(row["expenses"]), "CellRight"), paragraph(money(row["balance"]), "CellRight")])
    story.append(report_table(category_rows, [48 * mm, 46 * mm, 28 * mm, 28 * mm, 28 * mm], (2, 3, 4)))

    if data["adjustments"]:
        story.extend([Paragraph("Stornos und Korrekturen", styles["Section"]), Paragraph(f"Im Berichtsjahr sind {len(data['adjustments'])} unveränderliche Storno- oder Korrekturketten dokumentiert. Die zugehörigen Buchungen sind im Journal gekennzeichnet.", styles["ReportIntro"])])

    story.extend([PageBreak(), Paragraph("Buchungsjournal", styles["Section"])])
    journal_rows = [["Datum", "Konto", "Gegenpartei / Zweck", "Kategorie", "Beleg", "Betrag"]]
    receipt_labels = {"missing": "Fehlt", "complete": "Vollst.", "not_required": "Nicht nötig"}
    for transaction in data["transactions"]:
        description = transaction["description"]
        if transaction["adjustment_kind"]:
            description += " [Original korrigiert]"
        elif transaction["adjustment_role"] == "reversal":
            description += " [Storno]"
        elif transaction["adjustment_role"] == "replacement":
            description += " [Korrektur]"
        journal_rows.append(
            [
                paragraph(date_de(transaction["booking_date"])),
                paragraph(transaction["account_name"] or "-", "CellSmall"),
                paragraph(description),
                paragraph(transaction["category_display"], "CellSmall"),
                paragraph(receipt_labels[transaction["receipt_status"]], "CellSmall"),
                paragraph(money(transaction["amount_cents"], transaction["currency"]), "CellRight"),
            ]
        )
    story.append(report_table(journal_rows, [18 * mm, 25 * mm, 55 * mm, 39 * mm, 18 * mm, 23 * mm], (5,)))

    final_note = "Dieser Bericht ist eine Vorschau und kann sich bis zum Jahresabschluss verändern."
    if data["is_final"]:
        final_note = f"Abgeschlossen am {date_de(data['closed_at'])}. Prüfsumme des Abschlussstands: {data['summary_hash']}"
    story.extend(
        [
            Spacer(1, 8 * mm),
            KeepTogether([Paragraph("Bestätigung", styles["Section"]), Paragraph(final_note, styles["ReportIntro"]), Spacer(1, 14 * mm), Table([["Ort, Datum", "Schatzmeister", "Kassenprüfer"]], colWidths=[59 * mm] * 3, style=TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.6, MUTED), ("TEXTCOLOR", (0, 0), (-1, 0), MUTED), ("FONTSIZE", (0, 0), (-1, 0), 8), ("TOPPADDING", (0, 0), (-1, 0), 4)]))]),
        ]
    )
    document.build(story, onFirstPage=page_frame, onLaterPages=page_frame)
    return output.getvalue()
