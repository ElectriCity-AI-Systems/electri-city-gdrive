"""Local durable delivery receipts. Call mutations under processing_lock().

No raw IPN or exception messages are stored. ``sending`` is deliberately terminal
for automatic workers until the owner independently reconciles SMTP acceptance.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
from typing import Iterator


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    path.chmod(0o600)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA synchronous=FULL')
        yield db
    finally:
        db.close()


def initialize(path: Path, legacy: Path, valid_id) -> None:
    # Read first: inaccessible legacy state must fail closed, never skip dedupe.
    ids = legacy.read_text(encoding='utf-8').split() if legacy.exists() else []
    with connect(path) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS deliveries (
            txn_id TEXT PRIMARY KEY, status TEXT NOT NULL
                CHECK(status IN ('pending','retry','sending','sent')),
            name TEXT, email TEXT, license_key TEXT, message_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            currency TEXT, amount TEXT, product_field TEXT, product_value TEXT
        )''')
        db.executemany("INSERT OR IGNORE INTO deliveries(txn_id,status) VALUES (?, 'sent')",
                       ((txn,) for txn in ids if valid_id(txn)))


def enqueue(path: Path, txn: str, name: str, email: str, form: dict,
            product_field: str) -> None:
    with connect(path) as db, db:
        db.execute('''INSERT OR IGNORE INTO deliveries
            (txn_id,status,name,email,currency,amount,product_field,product_value)
            VALUES (?, 'pending', ?, ?, ?, ?, ?, ?)''',
            (txn, name, email, form.get('mc_currency'), form.get('mc_gross'),
             product_field, form.get(product_field)))


def claim(path: Path, txn: str) -> dict | None:
    with connect(path) as db, db:
        result = db.execute("""UPDATE deliveries SET status='sending', attempts=attempts+1,
            last_error=NULL WHERE txn_id=? AND status IN ('pending','retry')""", (txn,))
        if result.rowcount != 1:
            return None
        row = db.execute('SELECT * FROM deliveries WHERE txn_id=?', (txn,)).fetchone()
        return dict(row)


def save_key(path: Path, txn: str, key: str, message_id: str) -> None:
    with connect(path) as db, db:
        db.execute('UPDATE deliveries SET license_key=?, message_id=? WHERE txn_id=?',
                   (key, message_id, txn))


def set_status(path: Path, txn: str, status: str, error: str | None = None) -> None:
    with connect(path) as db, db:
        db.execute('UPDATE deliveries SET status=?, last_error=? WHERE txn_id=?',
                   (status, error, txn))


def pending(path: Path) -> list[str]:
    with connect(path) as db:
        return [row[0] for row in db.execute(
            "SELECT txn_id FROM deliveries WHERE status IN ('pending','retry') ORDER BY rowid")]


def accepted(path: Path, txn: str) -> bool:
    """Read only: checking recovery eligibility must not initialize a database."""
    if not path.exists():
        return False
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        return db.execute("SELECT 1 FROM deliveries WHERE txn_id=? AND status='sent'",
                          (txn,)).fetchone() is not None
    finally:
        db.close()
