from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from .camt import CamtReport, CamtTransaction, _fingerprint


HEADER_ALIASES = {
    "booking_date": ("buchungstag", "buchungsdatum", "datum", "date", "booking date"),
    "value_date": ("valuta", "valutadatum", "wertstellung", "value date"),
    "amount": ("betrag", "umsatz", "amount", "betrag (eur)"),
    "counterparty": (
        "beguenstigter/zahlungspflichtiger",
        "begünstigter/zahlungspflichtiger",
        "zahlungspflichtiger",
        "empfänger/auftraggeber",
        "empfaenger/auftraggeber",
        "gegenpartei",
        "name",
    ),
    "purpose": ("verwendungszweck", "buchungstext", "beschreibung", "purpose", "text"),
    "reference": ("referenz", "kundenreferenz", "end-to-end-referenz", "mandatsreferenz"),
    "currency": ("währung", "waehrung", "currency"),
    "counterparty_iban": ("iban", "kontonummer/iban", "gegenkonto iban"),
}


@dataclass(frozen=True)
class CsvPreview:
    headers: list[str]
    rows: list[list[str]]
    delimiter: str
    encoding: str
    detected: dict[str, str]


def _decode(path: str | Path) -> tuple[str, str]:
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp1252", "iso-8859-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("Die CSV-Datei verwendet eine nicht unterstützte Zeichenkodierung.")


def _delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:10])
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:
        return ";"


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def preview_csv(path: str | Path) -> CsvPreview:
    text, encoding = _decode(path)
    delimiter = _delimiter(text)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if len(rows) < 2:
        raise ValueError("Die CSV-Datei enthält keine Buchungszeilen.")
    headers = [cell.strip() or f"Spalte {index + 1}" for index, cell in enumerate(rows[0])]
    detected = {}
    for field, aliases in HEADER_ALIASES.items():
        for header in headers:
            normalized = _normalized(header)
            if normalized in aliases or any(alias in normalized for alias in aliases):
                detected[field] = header
                break
    return CsvPreview(
        headers=headers,
        rows=[row + [""] * (len(headers) - len(row)) for row in rows[1:6]],
        delimiter=delimiter,
        encoding=encoding,
        detected=detected,
    )


def _date(raw: str) -> str:
    value = raw.strip()
    for pattern in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value[:10], pattern).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Ungültiges CSV-Datum: {raw!r}")


def _amount(raw: str) -> int:
    value = raw.strip().replace("\xa0", "").replace(" ", "")
    negative = value.startswith("-") or (value.startswith("(") and value.endswith(")"))
    value = value.strip("-+()")
    if not value:
        raise ValueError("Ein CSV-Betrag fehlt.")
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    try:
        cents = int(
            (Decimal(value) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    except InvalidOperation:
        raise ValueError(f"Ungültiger CSV-Betrag: {raw!r}") from None
    return -abs(cents) if negative else cents


def parse_csv(path: str | Path, mapping: dict[str, str], account_iban: str = "") -> CamtReport:
    preview = preview_csv(path)
    indices = {field: preview.headers.index(header) for field, header in mapping.items() if header in preview.headers}
    missing = [field for field in ("booking_date", "amount", "purpose") if field not in indices]
    if missing:
        raise ValueError("Bitte Buchungsdatum, Betrag und Verwendungszweck gültig zuordnen.")
    text, _ = _decode(path)
    rows = list(csv.reader(io.StringIO(text), delimiter=preview.delimiter))[1:]
    transactions = []
    for line_number, row in enumerate(rows, start=2):
        if not any(cell.strip() for cell in row):
            continue
        row += [""] * (len(preview.headers) - len(row))
        try:
            booking_date = _date(row[indices["booking_date"]])
            amount = _amount(row[indices["amount"]])
        except (ValueError, IndexError) as exc:
            raise ValueError(f"CSV-Zeile {line_number}: {exc}") from exc

        def value(field):
            return row[indices[field]].strip() if field in indices else ""

        purpose = value("purpose")
        if not purpose:
            raise ValueError(f"CSV-Zeile {line_number}: Verwendungszweck fehlt.")
        value_date = _date(value("value_date")) if value("value_date") else None
        currency = value("currency").upper() or "EUR"
        counterparty = value("counterparty")
        counterparty_iban = re.sub(r"\s+", "", value("counterparty_iban")).upper()
        reference = value("reference")
        fingerprint = _fingerprint(
            [account_iban, booking_date, str(amount), currency, reference, counterparty, purpose]
        )
        transactions.append(
            CamtTransaction(
                account_iban=account_iban,
                booking_date=booking_date,
                value_date=value_date,
                amount_cents=amount,
                currency=currency,
                counterparty=counterparty,
                counterparty_iban=counterparty_iban,
                purpose=purpose,
                bank_reference=reference,
                bank_transaction_code="CSV",
                fingerprint=fingerprint,
            )
        )
    if not transactions:
        raise ValueError("Die CSV-Datei enthält keine Buchungen.")
    return CamtReport(account_iban=account_iban, transactions=transactions)
