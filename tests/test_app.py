import io
import re
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from app import create_app
from tests.test_camt import CAMT
from tests.test_importers import CSV_STATEMENT, MT940


class AppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp.name)
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "ADMIN_PASSWORD": "test-password",
                "DATA_DIR": data_dir,
                "DATABASE": str(data_dir / "test.sqlite3"),
            }
        )
        self.client = self.app.test_client()
        self.client.post("/login", data={"password": "test-password"})

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self):
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def test_import_is_idempotent(self):
        response = self.client.post(
            "/import",
            data={"csrf_token": self.csrf(), "statement": (io.BytesIO(CAMT.encode()), "camt.xml")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            "/import",
            data={"csrf_token": self.csrf(), "statement": (io.BytesIO(CAMT.encode()), "camt.xml")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        connection = sqlite3.connect(self.app.config["DATABASE"])
        count = connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        batch_count = connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0]
        connection.close()
        self.assertEqual(count, 2)
        self.assertEqual(batch_count, 1)

    def test_import_creates_and_assigns_bank_account(self):
        self.client.post(
            "/import",
            data={"csrf_token": self.csrf(), "statement": (io.BytesIO(CAMT.encode()), "camt.xml")},
            content_type="multipart/form-data",
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        account = connection.execute("SELECT id,iban,kind FROM accounts").fetchone()
        assigned = connection.execute(
            "SELECT COUNT(*) FROM transactions WHERE account_id=?", (account[0],)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(account[1], "DE02120300000000202051")
        self.assertEqual(account[2], "bank")
        self.assertEqual(assigned, 2)

    def test_mt940_import_uses_same_journal(self):
        response = self.client.post(
            "/import",
            data={"csrf_token": self.csrf(), "statement": (io.BytesIO(MT940.encode("cp1252")), "umsatz.mt940")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        connection = sqlite3.connect(self.app.config["DATABASE"])
        amounts = [row[0] for row in connection.execute("SELECT amount_cents FROM transactions ORDER BY id")]
        batch = connection.execute("SELECT filename,imported_count FROM import_batches").fetchone()
        connection.close()
        self.assertEqual(amounts, [4250, -1999])
        self.assertEqual(batch, ("umsatz.mt940", 2))

    def test_camt_and_mt940_versions_of_same_transactions_are_duplicates(self):
        self.client.post(
            "/import",
            data={"csrf_token": self.csrf(), "statement": (io.BytesIO(CAMT.encode()), "umsatz.xml")},
            content_type="multipart/form-data",
        )
        self.client.post(
            "/import",
            data={"csrf_token": self.csrf(), "statement": (io.BytesIO(MT940.encode("cp1252")), "umsatz.mt940")},
            content_type="multipart/form-data",
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        transaction_count = connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        second_batch = connection.execute(
            "SELECT imported_count,duplicate_count FROM import_batches ORDER BY id DESC LIMIT 1"
        ).fetchone()
        connection.close()
        self.assertEqual(transaction_count, 2)
        self.assertEqual(second_batch, (0, 2))

    def test_csv_preview_and_mapping_import(self):
        self.client.post(
            "/accounts",
            data={
                "csrf_token": self.csrf(), "kind": "bank", "name": "Vereinskonto",
                "iban": "DE02120300000000202051", "opening_balance": "0",
            },
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        account_id = connection.execute("SELECT id FROM accounts").fetchone()[0]
        connection.close()
        preview = self.client.post(
            "/import/csv/preview",
            data={
                "csrf_token": self.csrf(), "account_id": str(account_id),
                "statement": (io.BytesIO(CSV_STATEMENT.encode()), "umsatz.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(preview.status_code, 200)
        self.assertIn("Spalten zuordnen".encode(), preview.data)
        with self.client.session_transaction() as session:
            token = session["csv_preview"]["token"]
        response = self.client.post(
            "/import/csv/complete",
            data={
                "csrf_token": self.csrf(), "token": token,
                "booking_date": "Buchungstag", "value_date": "Valutadatum",
                "counterparty": "Begünstigter/Zahlungspflichtiger",
                "purpose": "Verwendungszweck", "amount": "Betrag",
                "currency": "Währung", "reference": "Referenz",
            },
        )
        self.assertEqual(response.status_code, 302)
        connection = sqlite3.connect(self.app.config["DATABASE"])
        amounts = [row[0] for row in connection.execute("SELECT amount_cents FROM transactions ORDER BY id")]
        connection.close()
        self.assertEqual(amounts, [4250, -1999])

    def test_cash_entry_is_added_to_shared_journal(self):
        self.client.post(
            "/accounts",
            data={
                "csrf_token": self.csrf(),
                "kind": "cash",
                "name": "Barkasse",
                "opening_balance": "10,00",
            },
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        account_id = connection.execute("SELECT id FROM accounts WHERE kind='cash'").fetchone()[0]
        connection.close()
        response = self.client.post(
            f"/accounts/{account_id}/cash-entry",
            data={
                "csrf_token": self.csrf(),
                "booking_date": "2026-07-15",
                "direction": "expense",
                "amount": "3,50",
                "purpose": "Briefmarken",
                "receipt_status": "missing",
            },
        )
        self.assertEqual(response.status_code, 302)
        connection = sqlite3.connect(self.app.config["DATABASE"])
        transaction = connection.execute(
            "SELECT account_id,amount_cents,bank_transaction_code FROM transactions"
        ).fetchone()
        balance = connection.execute(
            """SELECT opening_balance_cents + COALESCE(SUM(amount_cents),0)
               FROM accounts LEFT JOIN transactions ON transactions.account_id=accounts.id
               WHERE accounts.id=?""",
            (account_id,),
        ).fetchone()[0]
        connection.close()
        self.assertEqual(transaction, (account_id, -350, "CASH"))
        self.assertEqual(balance, 650)

    def test_review_link_is_public_read_only_and_revocable(self):
        self.client.post(
            "/import",
            data={"csrf_token": self.csrf(), "statement": (io.BytesIO(CAMT.encode()), "camt.xml")},
            content_type="multipart/form-data",
        )
        response = self.client.post(
            "/reviews",
            data={"csrf_token": self.csrf(), "year": "2026", "expires_days": "7"},
        )
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'href="(http://localhost/review/[^\"]+)"', response.data)
        self.assertIsNotNone(match)
        public_path = match.group(1).decode().removeprefix("http://localhost")
        anonymous = self.app.test_client()
        public_response = anonymous.get(public_path)
        self.assertEqual(public_response.status_code, 200)
        self.assertIn("Kassenprüfung".encode(), public_response.data)

        connection = sqlite3.connect(self.app.config["DATABASE"])
        share_id = connection.execute("SELECT id FROM review_shares").fetchone()[0]
        connection.close()
        self.client.post(
            f"/reviews/{share_id}/revoke", data={"csrf_token": self.csrf()}
        )
        self.assertEqual(anonymous.get(public_path).status_code, 404)

    def test_split_booking_must_match_full_amount(self):
        self.client.post(
            "/import",
            data={"csrf_token": self.csrf(), "statement": (io.BytesIO(CAMT.encode()), "camt.xml")},
            content_type="multipart/form-data",
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        transaction_id = connection.execute(
            "SELECT id FROM transactions WHERE amount_cents=4250"
        ).fetchone()[0]
        categories = [row[0] for row in connection.execute("SELECT id FROM categories LIMIT 2")]
        connection.close()
        response = self.client.post(
            f"/transactions/{transaction_id}/splits",
            data={
                "csrf_token": self.csrf(),
                "split_category_id": [str(categories[0]), str(categories[1])],
                "split_amount": ["20,00", "22,50"],
                "split_note": ["Teil A", "Teil B"],
            },
        )
        self.assertEqual(response.status_code, 302)
        connection = sqlite3.connect(self.app.config["DATABASE"])
        split_total = connection.execute(
            "SELECT SUM(amount_cents) FROM transaction_splits WHERE transaction_id=?",
            (transaction_id,),
        ).fetchone()[0]
        category_id = connection.execute(
            "SELECT category_id FROM transactions WHERE id=?", (transaction_id,)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(split_total, 4250)
        self.assertIsNone(category_id)

    def test_year_close_locks_changes_and_builds_archive(self):
        self.client.post(
            "/import",
            data={"csrf_token": self.csrf(), "statement": (io.BytesIO(CAMT.encode()), "camt.xml")},
            content_type="multipart/form-data",
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        category_id = connection.execute("SELECT id FROM categories LIMIT 1").fetchone()[0]
        connection.execute(
            "UPDATE transactions SET category_id=?,receipt_status='not_required'", (category_id,)
        )
        connection.commit()
        transaction_id = connection.execute("SELECT id FROM transactions LIMIT 1").fetchone()[0]
        connection.close()
        response = self.client.post(
            "/year-close/2026", data={"csrf_token": self.csrf()}
        )
        self.assertEqual(response.status_code, 302)
        archive_response = self.client.get("/years/2026/archive.zip")
        self.assertEqual(archive_response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(archive_response.data)) as archive:
            names = set(archive.namelist())
        self.assertTrue({"buchungen.csv", "pruefbericht.json", "manifest.json"}.issubset(names))

        self.client.post(
            f"/transactions/{transaction_id}/update",
            data={
                "csrf_token": self.csrf(),
                "category_id": str(category_id),
                "receipt_status": "not_required",
                "note": "darf nicht gespeichert werden",
            },
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        note = connection.execute("SELECT note FROM transactions WHERE id=?", (transaction_id,)).fetchone()[0]
        connection.close()
        self.assertIsNone(note)

    def test_protected_routes_require_login(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/").status_code, 302)
        self.assertIn("/login", anonymous.get("/").headers["Location"])

    def test_authenticated_product_pages_render(self):
        for path in ("/", "/transactions", "/accounts", "/import", "/categories", "/reviews", "/year-close", "/audit"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


if __name__ == "__main__":
    unittest.main()
