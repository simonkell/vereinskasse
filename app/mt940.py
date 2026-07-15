from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from .camt import CamtReport, CamtTransaction, _fingerprint


TAG_PATTERN = re.compile(r"^:(\d{2}[A-Z]?):(.*)$")
ENTRY_PATTERN = re.compile(
    r"^(?P<booking>\d{6})(?P<value>\d{4})?(?P<direction>R?[DC])"
    r"(?P<funds>[A-Z])?(?P<amount>\d+(?:,\d{0,2})?)"
    r"(?P<rest>.*)$"
)


def _read_text(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp1252", "iso-8859-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Die MT940-Datei verwendet eine nicht unterstützte Zeichenkodierung.")


def _tags(text: str):
    parsed = []
    current = None
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip("\ufeff")
        match = TAG_PATTERN.match(line)
        if match:
            current = [match.group(1), match.group(2)]
            parsed.append(current)
        elif current is not None and line:
            current[1] += "\n" + line
    return parsed


def _iso_date(raw: str) -> str:
    year = int(raw[:2])
    year += 2000 if year < 70 else 1900
    try:
        return date(year, int(raw[2:4]), int(raw[4:6])).isoformat()
    except ValueError as exc:
        raise ValueError(f"Ungültiges MT940-Buchungsdatum: {raw!r}") from exc


def _amount_cents(raw: str, direction: str) -> int:
    try:
        cents = int(
            (Decimal(raw.replace(",", ".")) * 100).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    except (InvalidOperation, ValueError):
        raise ValueError(f"Ungültiger MT940-Betrag: {raw!r}") from None
    debit = direction.endswith("D")
    if direction.startswith("R"):
        debit = not debit
    return -abs(cents) if debit else abs(cents)


def _structured_details(raw: str) -> dict[str, list[str]]:
    compact = " ".join(part.strip() for part in raw.splitlines() if part.strip())
    matches = list(re.finditer(r"\?(\d{2})", compact))
    if not matches:
        return {"text": [compact]} if compact else {}
    values: dict[str, list[str]] = {}
    prefix = compact[: matches[0].start()].strip()
    if prefix:
        values.setdefault("text", []).append(prefix)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(compact)
        value = compact[match.end() : end].strip()
        if value:
            values.setdefault(match.group(1), []).append(value)
    return values


def _account_iban(raw: str) -> str:
    compact = re.sub(r"\s+", "", raw).upper()
    match = re.search(r"DE\d{20}", compact)
    return match.group(0) if match else ""


def parse_mt940(path: str | Path) -> CamtReport:
    tags = _tags(_read_text(path))
    if not tags or not any(tag == "61" for tag, _ in tags):
        raise ValueError("Keine MT940-Buchungen (:61:) gefunden.")

    account_iban = ""
    currency = "EUR"
    transactions = []
    index = 0
    while index < len(tags):
        tag, value = tags[index]
        if tag == "25":
            account_iban = _account_iban(value)
        elif tag in {"60F", "60M"}:
            match = re.search(r"[A-Z](\d{6})([A-Z]{3})", value)
            if match:
                currency = match.group(2)
        elif tag == "61":
            entry = ENTRY_PATTERN.match(value.splitlines()[0].replace(" ", ""))
            if not entry:
                raise ValueError(f"Eine MT940-Buchungszeile ist ungültig: {value!r}")
            booking_date = _iso_date(entry.group("booking"))
            value_date = None
            if entry.group("value"):
                value_date = f"{booking_date[:4]}-{entry.group('value')[:2]}-{entry.group('value')[2:]}"
                try:
                    value_date = date.fromisoformat(value_date).isoformat()
                except ValueError:
                    value_date = None
            amount = _amount_cents(entry.group("amount"), entry.group("direction"))
            rest = entry.group("rest")
            transaction_code = rest[:4] if len(rest) >= 4 else ""
            reference_part = rest[4:] if transaction_code else rest
            customer_reference, _, bank_reference = reference_part.partition("//")

            details_raw = ""
            if index + 1 < len(tags) and tags[index + 1][0] == "86":
                details_raw = tags[index + 1][1]
            details = _structured_details(details_raw)
            counterparty = " ".join(details.get("32", []) + details.get("33", [])).strip()
            counterparty_iban = next(
                (value for value in details.get("31", []) if re.fullmatch(r"[A-Z]{2}\d{13,32}", value.replace(" ", ""))),
                "",
            ).replace(" ", "")
            purpose_parts = []
            for code in [*[str(number) for number in range(20, 30)], "60", "61", "62", "63"]:
                purpose_parts.extend(details.get(code, []))
            if not purpose_parts:
                purpose_parts.extend(details.get("00", []) + details.get("text", []))
            purpose = " ".join(purpose_parts).strip()
            reference = customer_reference.strip() or bank_reference.strip()
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
                    bank_transaction_code=transaction_code,
                    fingerprint=fingerprint,
                )
            )
        index += 1

    if not transactions:
        raise ValueError("Die MT940-Datei enthält keine Buchungen.")
    return CamtReport(account_iban=account_iban, transactions=transactions)
