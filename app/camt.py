from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from defusedxml import ElementTree as ET


@dataclass(frozen=True)
class CamtTransaction:
    account_iban: str
    booking_date: str
    value_date: str | None
    amount_cents: int
    currency: str
    counterparty: str
    counterparty_iban: str
    purpose: str
    bank_reference: str
    bank_transaction_code: str
    fingerprint: str


@dataclass(frozen=True)
class CamtReport:
    account_iban: str
    transactions: list[CamtTransaction]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(node, name):
    return [child for child in node.iter() if _local(child.tag) == name]


def _first_text(node, *path):
    current = node
    for name in path:
        current = next((c for c in list(current) if _local(c.tag) == name), None)
        if current is None:
            return ""
    return (current.text or "").strip()


def _desc_text(node, name):
    found = next((c for c in node.iter() if _local(c.tag) == name and c.text), None)
    return (found.text or "").strip() if found is not None else ""


def _iso_date(node, container):
    parent = next((c for c in list(node) if _local(c.tag) == container), None)
    if parent is None:
        return None
    raw = _first_text(parent, "Dt") or _first_text(parent, "DtTm")
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def _amount_cents(raw: str, credit_debit: str) -> int:
    try:
        cents = int((Decimal(raw) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Ungültiger CAMT-Betrag: {raw!r}") from None
    return -abs(cents) if credit_debit == "DBIT" else abs(cents)


def _fingerprint(values):
    normalized = "\x1f".join((value or "").strip() for value in values)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_camt(path: str | Path) -> CamtReport:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"Die Datei ist kein gültiges XML: {exc}") from exc

    reports = _children(root, "Rpt") or _children(root, "Stmt")
    if not reports:
        raise ValueError("Keine CAMT-Kontoauszugsdaten (Rpt/Stmt) gefunden.")

    parsed = []
    report_ibans = []
    for report in reports:
        account = next((c for c in list(report) if _local(c.tag) == "Acct"), None)
        iban = _desc_text(account, "IBAN") if account is not None else ""
        report_ibans.append(iban)

        for entry in [c for c in list(report) if _local(c.tag) == "Ntry"]:
            entry_amount_node = next((c for c in list(entry) if _local(c.tag) == "Amt"), None)
            if entry_amount_node is None or not entry_amount_node.text:
                continue
            entry_currency = entry_amount_node.attrib.get("Ccy", "EUR")
            entry_direction = _first_text(entry, "CdtDbtInd")
            booking_date = _iso_date(entry, "BookgDt")
            if not booking_date:
                raise ValueError("Eine Buchung enthält kein gültiges Buchungsdatum.")
            value_date = _iso_date(entry, "ValDt")
            entry_ref = _first_text(entry, "AcctSvcrRef") or _first_text(entry, "NtryRef")
            bank_code = _desc_text(entry, "BkTxCd")

            details = _children(entry, "TxDtls")
            candidates = details if details else [entry]
            for detail in candidates:
                tx_amount = next(
                    (c for c in detail.iter() if _local(c.tag) == "Amt" and c.text),
                    None,
                )
                amount_node = tx_amount if details and tx_amount is not None else entry_amount_node
                direction = _desc_text(detail, "CdtDbtInd") or entry_direction
                amount = _amount_cents(amount_node.text.strip(), direction)
                currency = amount_node.attrib.get("Ccy", entry_currency)

                if direction == "DBIT":
                    counterparty = _first_text(detail, "RltdPties", "Cdtr", "Nm")
                    counterparty_iban = _first_text(detail, "RltdPties", "CdtrAcct", "Id", "IBAN")
                else:
                    counterparty = _first_text(detail, "RltdPties", "Dbtr", "Nm")
                    counterparty_iban = _first_text(detail, "RltdPties", "DbtrAcct", "Id", "IBAN")

                purposes = [
                    (node.text or "").strip()
                    for node in detail.iter()
                    if _local(node.tag) == "Ustrd" and (node.text or "").strip()
                ]
                purpose = " ".join(purposes) or _desc_text(detail, "AddtlTxInf")
                reference = (
                    _first_text(detail, "Refs", "AcctSvcrRef")
                    or _first_text(detail, "Refs", "EndToEndId")
                    or _first_text(detail, "Refs", "TxId")
                    or entry_ref
                )
                fingerprint = _fingerprint(
                    [iban, booking_date, str(amount), currency, reference, counterparty, purpose]
                )
                parsed.append(
                    CamtTransaction(
                        account_iban=iban,
                        booking_date=booking_date,
                        value_date=value_date,
                        amount_cents=amount,
                        currency=currency,
                        counterparty=counterparty,
                        counterparty_iban=counterparty_iban,
                        purpose=purpose,
                        bank_reference=reference,
                        bank_transaction_code=bank_code,
                        fingerprint=fingerprint,
                    )
                )

    if not parsed:
        raise ValueError("Die CAMT-Datei enthält keine Buchungen.")
    unique_ibans = {iban for iban in report_ibans if iban}
    account_iban = next(iter(unique_ibans)) if len(unique_ibans) == 1 else ""
    return CamtReport(account_iban=account_iban, transactions=parsed)
