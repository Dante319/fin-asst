"""SQLite storage.

Schema is deliberately small and readable -- you should be able to open
data/finasst.db in any sqlite browser and understand it without this file.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from . import config

SCHEMA = """
PRAGMA journal_mode = DELETE;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    issuer      TEXT NOT NULL,              -- amex | simplii | generic
    kind        TEXT NOT NULL DEFAULT 'credit',  -- credit | chequing | savings
    currency    TEXT NOT NULL DEFAULT 'CAD',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date            TEXT NOT NULL,          -- ISO YYYY-MM-DD
    description     TEXT NOT NULL,          -- cleaned merchant string
    raw_description TEXT NOT NULL,          -- exactly as the issuer wrote it
    amount          REAL NOT NULL,          -- negative = outflow
    currency        TEXT NOT NULL DEFAULT 'CAD',
    category        TEXT,                   -- NULL = not yet categorised
    category_source TEXT,                   -- rule | manual | default
    fingerprint     TEXT NOT NULL UNIQUE,   -- makes re-importing a statement safe
    source_file     TEXT,
    imported_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tx_date     ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_tx_account  ON transactions(account_id);

CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY,
    pattern    TEXT NOT NULL,               -- matched case-insensitively
    match_type TEXT NOT NULL DEFAULT 'contains',  -- contains | regex | exact
    category   TEXT NOT NULL,
    priority   INTEGER NOT NULL DEFAULT 100,-- lower number wins
    is_user    INTEGER NOT NULL DEFAULT 0,  -- 1 = you taught it this
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(pattern, match_type)
);

CREATE TABLE IF NOT EXISTS goals (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    target_amount REAL NOT NULL,
    target_date   TEXT,                     -- ISO YYYY-MM-DD, NULL = no deadline
    saved_so_far  REAL NOT NULL DEFAULT 0,
    priority      INTEGER NOT NULL DEFAULT 100,  -- lower number funded first
    monthly_min   REAL NOT NULL DEFAULT 0,  -- floor this goal gets before others
    active        INTEGER NOT NULL DEFAULT 1,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per money-transfer-abroad, tied to the transaction that paid for it.
-- The CAD side is imported; the INR side has to come from you, because no
-- statement records what actually landed at the other end.
CREATE TABLE IF NOT EXISTS remittances (
    id            INTEGER PRIMARY KEY,
    tx_id         INTEGER NOT NULL UNIQUE REFERENCES transactions(id) ON DELETE CASCADE,
    sent_amount   REAL NOT NULL,           -- CAD leaving the account, positive
    received      REAL,                    -- INR actually credited, if you know it
    received_ccy  TEXT NOT NULL DEFAULT 'INR',
    fee           REAL,                    -- explicit fee, when the provider states one
    provider      TEXT,
    notes         TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cached reference rates. Populated only by an explicit `finasst fx sync`;
-- nothing in this app reaches the network on its own.
CREATE TABLE IF NOT EXISTS fx_rates (
    date   TEXT NOT NULL,
    base   TEXT NOT NULL,
    quote  TEXT NOT NULL,
    rate   REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'ecb',
    PRIMARY KEY (date, base, quote)
);
"""

# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so they are applied one at a time and duplicates ignored.
MIGRATIONS = [
    # Links an outgoing transaction to the incoming one in another account that
    # it turned out to be. Set by the transfer matcher, not by importers.
    ("transactions", "transfer_peer_id", "INTEGER"),
]

DEFAULT_SETTINGS = {
    "annual_return_rate": "0.04",   # what savings earn, nominal
    "annual_inflation": "0.025",    # erodes future target costs
    "surplus_lookback_months": "3", # how much history defines "normal" surplus
    "manual_monthly_surplus": "",   # set to a number to override the computed one
}


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_migrations(conn: sqlite3.Connection) -> None:
    existing = {
        table: {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for table in {t for t, _, _ in MIGRATIONS}
    }
    for table, column, decl in MIGRATIONS:
        if column not in existing.get(table, set()):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    apply_migrations(conn)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
        )
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


def get_or_create_account(
    conn: sqlite3.Connection,
    name: str,
    issuer: str,
    kind: str = "credit",
    currency: str = config.DEFAULT_CURRENCY,
) -> int:
    row = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO accounts(name, issuer, kind, currency) VALUES (?, ?, ?, ?)",
        (name, issuer, kind, currency),
    )
    conn.commit()
    return int(cur.lastrowid)


def accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
