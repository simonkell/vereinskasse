import io
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import create_app
from tests.test_camt import CAMT


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

    def test_protected_routes_require_login(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/").status_code, 302)
        self.assertIn("/login", anonymous.get("/").headers["Location"])

    def test_authenticated_product_pages_render(self):
        for path in ("/", "/transactions", "/accounts", "/import", "/categories", "/reviews", "/audit"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


if __name__ == "__main__":
    unittest.main()
