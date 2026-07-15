import sqlite3
from pathlib import Path

from flask import current_app, g


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('bank', 'cash')),
    iban TEXT,
    currency TEXT NOT NULL DEFAULT 'EUR',
    opening_balance_cents INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(organization_id, name),
    UNIQUE(organization_id, iban)
);

CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
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
    account_id INTEGER REFERENCES accounts(id),
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

CREATE TABLE IF NOT EXISTS review_shares (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    label TEXT NOT NULL,
    year TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS transaction_splits (
    id INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    amount_cents INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS year_closures (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    year TEXT NOT NULL,
    summary_hash TEXT NOT NULL,
    closed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(organization_id, year)
);

CREATE TABLE IF NOT EXISTS transaction_adjustments (
    id INTEGER PRIMARY KEY,
    original_transaction_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
    reversal_transaction_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
    replacement_transaction_id INTEGER UNIQUE REFERENCES transactions(id),
    kind TEXT NOT NULL CHECK(kind IN ('reversal', 'correction')),
    reason TEXT NOT NULL,
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
    db.execute("INSERT OR IGNORE INTO organizations(id, name) VALUES (1, 'Mein Verein')")
    _ensure_column(db, "import_batches", "account_id", "INTEGER REFERENCES accounts(id)")
    _ensure_column(db, "transactions", "account_id", "INTEGER REFERENCES accounts(id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_import_batches_account ON import_batches(account_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_transaction_splits_transaction ON transaction_splits(transaction_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_transaction_adjustments_original ON transaction_adjustments(original_transaction_id)")
    _migrate_existing_accounts(db)
    db.executemany(
        "INSERT OR IGNORE INTO categories(name, tax_area) VALUES (?, ?)",
        DEFAULT_CATEGORIES,
    )
    db.commit()


def _ensure_column(db, table, column, definition):
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_existing_accounts(db):
    ibans = db.execute(
        """
        SELECT DISTINCT account_iban FROM transactions WHERE account_iban IS NOT NULL AND account_iban != ''
        UNION
        SELECT DISTINCT account_iban FROM import_batches WHERE account_iban IS NOT NULL AND account_iban != ''
        """
    ).fetchall()
    for index, row in enumerate(ibans, start=1):
        iban = row["account_iban"]
        suffix = iban[-4:] if len(iban) >= 4 else iban
        db.execute(
            """INSERT OR IGNORE INTO accounts(organization_id, name, kind, iban)
               VALUES (1, ?, 'bank', ?)""",
            (f"Bankkonto {suffix}" if suffix else f"Bankkonto {index}", iban),
        )
    db.execute(
        """UPDATE transactions SET account_id=(
               SELECT id FROM accounts WHERE accounts.iban=transactions.account_iban
           ) WHERE account_id IS NULL AND account_iban IS NOT NULL"""
    )
    db.execute(
        """UPDATE import_batches SET account_id=(
               SELECT id FROM accounts WHERE accounts.iban=import_batches.account_iban
           ) WHERE account_id IS NULL AND account_iban IS NOT NULL"""
    )


def log_action(db, action, entity_type, entity_id=None, details=None):
    db.execute(
        "INSERT INTO audit_log(action, entity_type, entity_id, details) VALUES (?, ?, ?, ?)",
        (action, entity_type, entity_id, details),
    )
