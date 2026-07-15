import io
import re
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from app import create_app
from app.camt import _fingerprint
from tests.test_camt import CAMT, CAMT_NESTED_PARTIES
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

    def test_startup_repairs_blank_sparda_counterparties_from_original(self):
        self.client.post(
            "/import",
            data={
                "csrf_token": self.csrf(),
                "statement": (io.BytesIO(CAMT_NESTED_PARTIES.encode()), "sparda.xml"),
            },
            content_type="multipart/form-data",
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        row = connection.execute(
            """SELECT id,account_iban,booking_date,amount_cents,currency,
                      bank_reference,purpose
               FROM transactions ORDER BY id LIMIT 1"""
        ).fetchone()
        legacy_fingerprint = _fingerprint(
            [row[1], row[2], str(row[3]), row[4], row[5], "", row[6]]
        )
        connection.execute(
            """UPDATE transactions
               SET counterparty='',counterparty_iban='',fingerprint=? WHERE id=?""",
            (legacy_fingerprint, row[0]),
        )
        connection.commit()
        connection.close()

        create_app(dict(self.app.config))

        connection = sqlite3.connect(self.app.config["DATABASE"])
        repaired = connection.execute(
            "SELECT counterparty,counterparty_iban,fingerprint FROM transactions WHERE id=?",
            (row[0],),
        ).fetchone()
        connection.close()
        self.assertEqual(repaired[0], "Erika Beispiel")
        self.assertEqual(repaired[1], "DE11111111111111111111")
        self.assertNotEqual(repaired[2], legacy_fingerprint)

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

    def test_camt_balances_set_opening_balance_and_reconcile(self):
        self.client.post(
            "/import",
            data={
                "csrf_token": self.csrf(),
                "statement": (io.BytesIO(CAMT_NESTED_PARTIES.encode()), "sparda.xml"),
            },
            content_type="multipart/form-data",
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        account = connection.execute(
            "SELECT id,opening_balance_cents FROM accounts"
        ).fetchone()
        balances = connection.execute(
            """SELECT balance_type,balance_date,balance_cents
               FROM account_reconciliations ORDER BY balance_type"""
        ).fetchall()
        connection.close()
        self.assertEqual(account[1], 10000)
        self.assertEqual(
            balances,
            [("CLBD", "2026-06-02", 10505), ("OPBD", "2026-06-01", 10000)],
        )
        page = self.client.get("/accounts")
        self.assertIn("Abgeglichen".encode(), page.data)
        self.assertIn("105,05 EUR".encode(), page.data)
        connection = sqlite3.connect(self.app.config["DATABASE"])
        category_id = connection.execute("SELECT id FROM categories LIMIT 1").fetchone()[0]
        connection.execute(
            "UPDATE transactions SET category_id=?,receipt_status='not_required'", (category_id,)
        )
        connection.commit()
        connection.close()
        self.client.post("/year-close/2026", data={"csrf_token": self.csrf()})
        archive_response = self.client.get("/years/2026/archive.zip")
        with zipfile.ZipFile(io.BytesIO(archive_response.data)) as archive:
            self.assertIn("kontenabgleich.csv", archive.namelist())
            self.assertIn("Abweichung".encode(), archive.read("kontenabgleich.csv"))

    def test_manual_opening_balance_is_not_overwritten_by_camt(self):
        self.client.post(
            "/accounts",
            data={
                "csrf_token": self.csrf(), "kind": "bank", "name": "Vereinskonto",
                "iban": "DE02120300000000202051", "opening_balance": "50,00",
            },
        )
        self.client.post(
            "/import",
            data={
                "csrf_token": self.csrf(),
                "statement": (io.BytesIO(CAMT_NESTED_PARTIES.encode()), "sparda.xml"),
            },
            content_type="multipart/form-data",
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        opening, source = connection.execute(
            "SELECT opening_balance_cents,opening_balance_source FROM accounts"
        ).fetchone()
        connection.close()
        self.assertEqual((opening, source), (5000, "manual"))

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

    def test_cash_count_is_compared_with_calculated_balance(self):
        self.client.post(
            "/accounts",
            data={
                "csrf_token": self.csrf(), "kind": "cash", "name": "Barkasse",
                "opening_balance": "10,00",
            },
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        account_id = connection.execute("SELECT id FROM accounts").fetchone()[0]
        connection.close()
        self.client.post(
            f"/accounts/{account_id}/cash-entry",
            data={
                "csrf_token": self.csrf(), "booking_date": "2026-12-31",
                "direction": "expense", "amount": "3,50", "purpose": "Porto",
                "receipt_status": "not_required",
            },
        )
        self.client.post(
            f"/accounts/{account_id}/cash-count",
            data={
                "csrf_token": self.csrf(), "balance_date": "2026-12-31",
                "balance": "6,50", "note": "Jahresendzählung",
            },
        )
        page = self.client.get("/accounts")
        self.assertIn("Gezählter Bestand".encode(), page.data)
        self.assertIn("Abgeglichen".encode(), page.data)

    def test_rules_preview_and_controlled_application(self):
        self.client.post(
            "/import",
            data={
                "csrf_token": self.csrf(),
                "statement": (io.BytesIO(CAMT_NESTED_PARTIES.encode()), "sparda.xml"),
            },
            content_type="multipart/form-data",
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        category_id = connection.execute(
            "SELECT id FROM categories WHERE name='Mitgliedsbeiträge'"
        ).fetchone()[0]
        connection.close()
        self.client.post(
            "/rules",
            data={
                "csrf_token": self.csrf(), "name": "Beiträge",
                "direction": "income", "purpose_contains": "Mitgliedsbeitrag",
                "category_id": str(category_id), "receipt_status": "not_required",
            },
        )
        rules_page = self.client.get("/rules")
        self.assertIn("1 Treffer".encode(), rules_page.data)
        journal = self.client.get("/transactions")
        self.assertIn("Vorschlag: Beiträge".encode(), journal.data)
        connection = sqlite3.connect(self.app.config["DATABASE"])
        rule_id = connection.execute("SELECT id FROM classification_rules").fetchone()[0]
        connection.close()
        self.client.post(
            f"/rules/{rule_id}/apply", data={"csrf_token": self.csrf()}
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        rows = connection.execute(
            "SELECT amount_cents,category_id,receipt_status FROM transactions ORDER BY id"
        ).fetchall()
        connection.close()
        self.assertEqual(rows[0], (2500, category_id, "not_required"))
        self.assertIsNone(rows[1][1])

    def test_bulk_update_changes_selected_transactions(self):
        self.client.post(
            "/import",
            data={"csrf_token": self.csrf(), "statement": (io.BytesIO(CAMT.encode()), "camt.xml")},
            content_type="multipart/form-data",
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        ids = [row[0] for row in connection.execute("SELECT id FROM transactions ORDER BY id")]
        category_id = connection.execute("SELECT id FROM categories LIMIT 1").fetchone()[0]
        connection.close()
        self.client.post(
            "/transactions/bulk-update",
            data={
                "csrf_token": self.csrf(), "transaction_id": [str(value) for value in ids],
                "category_id": str(category_id), "receipt_status": "not_required",
            },
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        rows = connection.execute(
            "SELECT category_id,receipt_status FROM transactions"
        ).fetchall()
        connection.close()
        self.assertEqual(rows, [(category_id, "not_required"), (category_id, "not_required")])

    def test_camera_upload_replace_and_delete_receipt(self):
        self.client.post(
            "/import",
            data={"csrf_token": self.csrf(), "statement": (io.BytesIO(CAMT.encode()), "camt.xml")},
            content_type="multipart/form-data",
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        transaction_id = connection.execute("SELECT id FROM transactions LIMIT 1").fetchone()[0]
        connection.close()
        detail = self.client.get(f"/transactions/{transaction_id}")
        self.assertIn(b'capture="environment"', detail.data)
        self.client.post(
            f"/transactions/{transaction_id}/attachments",
            data={
                "csrf_token": self.csrf(),
                "camera": (io.BytesIO(b"camera-photo"), "image", "image/jpeg"),
            },
            content_type="multipart/form-data",
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        attachment = connection.execute(
            "SELECT id,stored_path,original_name FROM attachments"
        ).fetchone()
        status = connection.execute(
            "SELECT receipt_status FROM transactions WHERE id=?", (transaction_id,)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(status, "complete")
        self.assertEqual(attachment[2], "image.jpg")
        old_path = Path(self.app.config["DATA_DIR"]) / attachment[1]
        self.assertTrue(old_path.exists())
        self.client.post(
            f"/attachments/{attachment[0]}/replace",
            data={
                "csrf_token": self.csrf(),
                "replacement": (io.BytesIO(b"replacement-pdf"), "rechnung.pdf"),
            },
            content_type="multipart/form-data",
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        replaced = connection.execute(
            "SELECT original_name,stored_path FROM attachments WHERE id=?", (attachment[0],)
        ).fetchone()
        connection.close()
        self.assertEqual(replaced[0], "rechnung.pdf")
        self.assertFalse(old_path.exists())
        self.client.post(
            f"/attachments/{attachment[0]}/delete", data={"csrf_token": self.csrf()}
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        count = connection.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
        status = connection.execute(
            "SELECT receipt_status FROM transactions WHERE id=?", (transaction_id,)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(count, 0)
        self.assertEqual(status, "missing")

    def test_cash_reversal_creates_immutable_counter_booking(self):
        self.client.post(
            "/accounts",
            data={
                "csrf_token": self.csrf(), "kind": "cash", "name": "Barkasse",
                "opening_balance": "0,00",
            },
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        account_id = connection.execute("SELECT id FROM accounts WHERE kind='cash'").fetchone()[0]
        category_id = connection.execute("SELECT id FROM categories LIMIT 1").fetchone()[0]
        connection.close()
        self.client.post(
            f"/accounts/{account_id}/cash-entry",
            data={
                "csrf_token": self.csrf(), "booking_date": "2026-07-15", "direction": "expense",
                "amount": "12,50", "purpose": "Porto", "category_id": str(category_id),
                "receipt_status": "not_required",
            },
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        original_id = connection.execute("SELECT id FROM transactions").fetchone()[0]
        connection.close()
        response = self.client.post(
            f"/transactions/{original_id}/adjust",
            data={
                "csrf_token": self.csrf(), "action": "reverse", "booking_date": "2026-07-16",
                "reason": "Buchung doppelt erfasst",
            },
        )
        self.assertEqual(response.status_code, 302)
        connection = sqlite3.connect(self.app.config["DATABASE"])
        rows = connection.execute(
            "SELECT id,amount_cents,bank_transaction_code,note FROM transactions ORDER BY id"
        ).fetchall()
        adjustment = connection.execute(
            "SELECT original_transaction_id,reversal_transaction_id,replacement_transaction_id,kind FROM transaction_adjustments"
        ).fetchone()
        connection.close()
        self.assertEqual([row[1] for row in rows], [-1250, 1250])
        self.assertEqual(rows[1][2:], ("REVERSAL", "Buchung doppelt erfasst"))
        self.assertEqual(adjustment, (rows[0][0], rows[1][0], None, "reversal"))

        self.client.post(
            f"/transactions/{original_id}/update",
            data={
                "csrf_token": self.csrf(), "category_id": str(category_id),
                "receipt_status": "not_required", "note": "nachträglich verändert",
            },
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        note = connection.execute("SELECT note FROM transactions WHERE id=?", (original_id,)).fetchone()[0]
        connection.close()
        self.assertEqual(note, "")

    def test_cash_correction_creates_reversal_and_replacement(self):
        self.client.post(
            "/accounts",
            data={
                "csrf_token": self.csrf(), "kind": "cash", "name": "Barkasse",
                "opening_balance": "0,00",
            },
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        account_id = connection.execute("SELECT id FROM accounts WHERE kind='cash'").fetchone()[0]
        category_id = connection.execute("SELECT id FROM categories LIMIT 1").fetchone()[0]
        connection.close()
        self.client.post(
            f"/accounts/{account_id}/cash-entry",
            data={
                "csrf_token": self.csrf(), "booking_date": "2026-07-15", "direction": "income",
                "amount": "10,00", "purpose": "Spende bar", "category_id": str(category_id),
                "receipt_status": "not_required",
            },
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        original_id = connection.execute("SELECT id FROM transactions").fetchone()[0]
        connection.close()
        response = self.client.post(
            f"/transactions/{original_id}/adjust",
            data={
                "csrf_token": self.csrf(), "action": "correct", "booking_date": "2026-07-16",
                "reason": "Zahlendreher im Betrag", "new_amount": "12,50",
                "new_purpose": "Spende bar korrigiert", "new_category_id": str(category_id),
            },
        )
        self.assertEqual(response.status_code, 302)
        connection = sqlite3.connect(self.app.config["DATABASE"])
        rows = connection.execute(
            "SELECT amount_cents,bank_transaction_code,purpose FROM transactions ORDER BY id"
        ).fetchall()
        adjustment = connection.execute(
            "SELECT kind,replacement_transaction_id FROM transaction_adjustments"
        ).fetchone()
        connection.close()
        self.assertEqual([row[0] for row in rows], [1000, -1000, 1250])
        self.assertEqual(sum(row[0] for row in rows), 1250)
        self.assertEqual(rows[2][1:], ("CORRECTION", "Spende bar korrigiert"))
        self.assertEqual(adjustment[0], "correction")
        self.assertIsNotNone(adjustment[1])
        journal = self.client.get("/transactions")
        self.assertIn("Korrektur".encode(), journal.data)
        detail = self.client.get(f"/transactions/{adjustment[1]}")
        self.assertIn("unveränderlichen Korrekturkette".encode(), detail.data)

        self.client.post(
            f"/accounts/{account_id}/cash-count",
            data={
                "csrf_token": self.csrf(), "balance_date": "2026-12-31",
                "balance": "12,50", "note": "Jahresendzählung",
            },
        )
        self.client.post("/year-close/2026", data={"csrf_token": self.csrf()})
        archive_response = self.client.get("/years/2026/archive.zip")
        self.assertEqual(archive_response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(archive_response.data)) as archive:
            self.assertIn("korrekturen.csv", archive.namelist())
            self.assertIn("Zahlendreher".encode(), archive.read("korrekturen.csv"))

    def test_imported_bank_transaction_cannot_be_stopped_in_software(self):
        self.client.post(
            "/import",
            data={"csrf_token": self.csrf(), "statement": (io.BytesIO(CAMT.encode()), "umsatz.xml")},
            content_type="multipart/form-data",
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        transaction_id = connection.execute("SELECT id FROM transactions LIMIT 1").fetchone()[0]
        connection.close()
        self.client.post(
            f"/transactions/{transaction_id}/adjust",
            data={
                "csrf_token": self.csrf(), "action": "reverse", "booking_date": "2026-07-16",
                "reason": "darf nicht gehen",
            },
        )
        connection = sqlite3.connect(self.app.config["DATABASE"])
        adjustment_count = connection.execute("SELECT COUNT(*) FROM transaction_adjustments").fetchone()[0]
        transaction_count = connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        connection.close()
        self.assertEqual(adjustment_count, 0)
        self.assertEqual(transaction_count, 2)

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
        for path in ("/", "/transactions", "/accounts", "/import", "/categories", "/rules", "/reviews", "/year-close", "/audit"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


if __name__ == "__main__":
    unittest.main()
