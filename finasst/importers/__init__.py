"""Importer registry, format detection, and the DB-writing import service."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .amex import AmexImporter, AmexYearEndImporter
from .base import Importer, ParsedTx, read_rows
from .eqbank import EQBankImporter, read_pdf_text
from .generic import GenericImporter
from .simplii import SimpliiImporter

# Order matters: specific issuers first, generic last as the catch-all.
IMPORTERS: list[type[Importer]] = [AmexYearEndImporter, AmexImporter, SimpliiImporter, GenericImporter]
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
    check: str | None = None   # self-verification note, where the format allows one

    @property
    def summary(self) -> str:
        line = (
            f"{self.account}: {self.inserted} new, {self.duplicates} already known "
            f"({self.importer} format, {self.first_date} to {self.last_date})"
        )
        return f"{line} -- {self.check}" if self.check else line


def import_file(
    conn: sqlite3.Connection,
    path: str | Path,
    account_name: str,
    issuer: str | None = None,
    kind: str = "credit",
    currency: str = "CAD",
) -> ImportResult:
    """Load one statement into the database. Safe to run twice on the same file.

    Handles both CSV exports and PDF statements; the extension picks the route.
    """
    from ..db import get_or_create_account

    path = Path(path)
    check_note: str | None = None

    if path.suffix.lower() == ".pdf" or issuer == "eqbank":
        text = read_pdf_text(path)
        if not (EQBankImporter.sniff_text(text) or issuer == "eqbank"):
            raise ValueError(
                f"{path.name} is a PDF, but not one this app recognises. "
                "Only EQ Bank statements are supported as PDFs so far."
            )
        txs, check = EQBankImporter.parse_text(text)
        if not txs:
            raise ValueError(f"No transactions found in {path.name}")
        if not check.ok:
            raise ValueError(
                f"{path.name} did not reconcile: {check.describe()}. "
                "Refusing to import numbers that do not add up -- send me the "
                "statement layout so the parser can be fixed."
            )
        check_note = check.describe()
        importer_name = EQBankImporter.name
    else:
        rows = read_rows(path)
        if not rows:
            raise ValueError(f"{path.name} is empty")
        imp = BY_NAME[issuer] if issuer and issuer in BY_NAME else detect(rows)
        if imp is None:
            raise ValueError(
                f"Could not work out the format of {path.name}. "
                "Pass an issuer explicitly, or check the file has a header row."
            )
        txs = imp.parse(rows)
        importer_name = imp.name

    account_id = get_or_create_account(conn, account_name, importer_name, kind, currency)

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
        account=account_name, importer=importer_name, parsed=len(txs),
        inserted=inserted, duplicates=duplicates,
        first_date=dates[0] if dates else None,
        last_date=dates[-1] if dates else None,
        check=check_note,
    )


# Kept so older call sites and docs keep working.
import_csv = import_file
