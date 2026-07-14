import sqlite3
from pathlib import Path

from flask import current_app, g


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL UNIQUE,
    stored_path TEXT NOT NULL,
    account_iban TEXT,
    imported_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    tax_area TEXT NOT NULL DEFAULT 'Ideeller Bereich',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    import_batch_id INTEGER NOT NULL REFERENCES import_batches(id),
    fingerprint TEXT NOT NULL UNIQUE,
    account_iban TEXT,
    booking_date TEXT NOT NULL,
    value_date TEXT,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'EUR',
    counterparty TEXT,
    counterparty_iban TEXT,
    purpose TEXT,
    bank_reference TEXT,
    bank_transaction_code TEXT,
    category_id INTEGER REFERENCES categories(id),
    receipt_status TEXT NOT NULL DEFAULT 'missing'
        CHECK(receipt_status IN ('missing', 'complete', 'not_required')),
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_transactions_booking_date ON transactions(booking_date);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category_id);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    original_name TEXT NOT NULL,
    stored_path TEXT NOT NULL UNIQUE,
    mime_type TEXT,
    file_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


DEFAULT_CATEGORIES = [
    ("Mitgliedsbeiträge", "Ideeller Bereich"),
    ("Spenden", "Ideeller Bereich"),
    ("Verwaltung", "Ideeller Bereich"),
    ("Versicherungen und Gebühren", "Ideeller Bereich"),
    ("Veranstaltungen", "Zweckbetrieb"),
    ("Vermögensverwaltung", "Vermögensverwaltung"),
    ("Wirtschaftlicher Geschäftsbetrieb", "Wirtschaftlicher Geschäftsbetrieb"),
]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 10000")
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    path = Path(current_app.config["DATABASE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    db = get_db()
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(SCHEMA)
    db.executemany(
        "INSERT OR IGNORE INTO categories(name, tax_area) VALUES (?, ?)",
        DEFAULT_CATEGORIES,
    )
    db.commit()


def log_action(db, action, entity_type, entity_id=None, details=None):
    db.execute(
        "INSERT INTO audit_log(action, entity_type, entity_id, details) VALUES (?, ?, ?, ?)",
        (action, entity_type, entity_id, details),
    )
