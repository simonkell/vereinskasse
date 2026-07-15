import tempfile
import unittest
from pathlib import Path

from app.csv_import import parse_csv, preview_csv
from app.mt940 import parse_mt940


MT940 = """:20:STARTUMSE
:25:DE02120300000000202051
:28C:00001/001
:60F:C260701EUR1000,00
:61:2607140714C42,50NTRFE2E-001//BANKREF1
:86:020?00UEBERWEISUNG?20Mitgliedsbeitrag 2026?32Max Mustermann?31DE11111111111111111111
:61:2607150715D19,99NMSCREF-002//BANKREF2
:86:005?00LASTSCHRIFT?20Rechnung 4711?32Bürobedarf GmbH
:62F:C260715EUR1022,51
"""

CSV_STATEMENT = """Buchungstag;Valutadatum;Begünstigter/Zahlungspflichtiger;Verwendungszweck;Betrag;Währung;Referenz
14.07.2026;14.07.2026;Max Mustermann;Mitgliedsbeitrag 2026;42,50;EUR;E2E-001
15.07.2026;15.07.2026;Bürobedarf GmbH;Rechnung 4711;-19,99;EUR;REF-002
"""


class ImporterTest(unittest.TestCase):
    def test_mt940_parses_credit_debit_and_structured_details(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "statement.mt940"
            path.write_text(MT940, encoding="cp1252")
            report = parse_mt940(path)
        self.assertEqual(report.account_iban, "DE02120300000000202051")
        self.assertEqual([tx.amount_cents for tx in report.transactions], [4250, -1999])
        self.assertEqual(report.transactions[0].counterparty, "Max Mustermann")
        self.assertEqual(report.transactions[0].purpose, "Mitgliedsbeitrag 2026")
        self.assertEqual(report.transactions[1].bank_reference, "REF-002")

    def test_csv_preview_detects_common_german_bank_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "statement.csv"
            path.write_text(CSV_STATEMENT, encoding="utf-8")
            preview = preview_csv(path)
        self.assertEqual(preview.delimiter, ";")
        self.assertEqual(preview.detected["booking_date"], "Buchungstag")
        self.assertEqual(preview.detected["amount"], "Betrag")
        self.assertEqual(preview.detected["purpose"], "Verwendungszweck")

    def test_csv_mapping_creates_stable_signed_transactions(self):
        mapping = {
            "booking_date": "Buchungstag",
            "value_date": "Valutadatum",
            "counterparty": "Begünstigter/Zahlungspflichtiger",
            "purpose": "Verwendungszweck",
            "amount": "Betrag",
            "currency": "Währung",
            "reference": "Referenz",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "statement.csv"
            path.write_text(CSV_STATEMENT, encoding="utf-8")
            first = parse_csv(path, mapping, "DE02120300000000202051")
            second = parse_csv(path, mapping, "DE02120300000000202051")
        self.assertEqual([tx.amount_cents for tx in first.transactions], [4250, -1999])
        self.assertEqual(first.transactions[0].fingerprint, second.transactions[0].fingerprint)

    def test_csv_rejects_unknown_required_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "statement.csv"
            path.write_text(CSV_STATEMENT, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "gültig zuordnen"):
                parse_csv(
                    path,
                    {"booking_date": "Nicht vorhanden", "amount": "Betrag", "purpose": "Verwendungszweck"},
                )


if __name__ == "__main__":
    unittest.main()
