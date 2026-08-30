"""Importer registry, format detection, and the DB-writing import service."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .amex import AmexImporter
from .base import Importer, ParsedTx, read_rows
from .generic import GenericImporter
from .simplii import SimpliiImporter

# Order matters: specific issuers first, generic last as the catch-all.
IMPORTERS: list[type[Importer]] = [AmexImporter, SimpliiImporter, GenericImporter]
BY_NAME = {imp.name: imp for imp in IMPORTERS}


def detect(rows: Sequence[Sequence[str]]) -> type[Importer] | None:
    for imp in IMPORTERS:
        try:
            if imp.sniff(rows):
                return imp
        except Exception:
            continue
    return None


@dataclass
class ImportResult:
    account: str
    importer: str
    parsed: int
    inserted: int
    duplicates: int
    first_date: str | None = None
    last_date: str | None = None

    @property
    def summary(self) -> str:
        return (
            f"{self.account}: {self.inserted} new, {self.duplicates} already known "
            f"({self.importer} format, {self.first_date} to {self.last_date})"
        )


def import_csv(
    conn: sqlite3.Connection,
    path: str | Path,
    account_name: str,
    issuer: str | None = None,
    kind: str = "credit",
    currency: str = "CAD",
) -> ImportResult:
    """Load one CSV into the database. Safe to run twice on the same file."""
    from ..db import get_or_create_account

    path = Path(path)
    rows = read_rows(path)
    if not rows:
        raise ValueError(f"{path.name} is empty")

    imp = BY_NAME[issuer] if issuer and issuer in BY_NAME else detect(rows)
    if imp is None:
        raise ValueError(
            f"Could not work out the format of {path.name}. "
            "Pass an issuer explicitly, or check the file has a header row."
        )

    txs: list[ParsedTx] = imp.parse(rows)
    account_id = get_or_create_account(conn, account_name, imp.name, kind, currency)

    inserted = duplicates = 0
    for tx in txs:
        fp = tx.fingerprint(account_name)
        cur = conn.execute(
            """INSERT OR IGNORE INTO transactions
               (account_id, date, description, raw_description, amount,
                currency, fingerprint, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (account_id, tx.date.isoformat(), tx.description, tx.raw_description,
             tx.amount, currency, fp, path.name),
        )
        if cur.rowcount:
            inserted += 1
        else:
            duplicates += 1
    conn.commit()

    dates = sorted(t.date.isoformat() for t in txs)
    return ImportResult(
        account=account_name, importer=imp.name, parsed=len(txs),
        inserted=inserted, duplicates=duplicates,
        first_date=dates[0] if dates else None,
        last_date=dates[-1] if dates else None,
    )
