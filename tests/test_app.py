import io
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

    def test_protected_routes_require_login(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/").status_code, 302)
        self.assertIn("/login", anonymous.get("/").headers["Location"])


if __name__ == "__main__":
    unittest.main()

