"""Shared importer plumbing: the parsed row, fingerprinting, and the ABC."""
from __future__ import annotations

import csv
import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Sequence

DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y", "%d-%b-%Y",
    "%b %d, %Y", "%d %b %Y", "%Y/%m/%d",
)


@dataclass
class ParsedTx:
    date: date
    description: str
    raw_description: str
    amount: float          # negative = outflow, per config's sign convention
    currency: str = "CAD"
    extra: dict = field(default_factory=dict)

    def fingerprint(self, account_name: str) -> str:
        """Stable identity for a transaction.

        Re-importing an overlapping statement must not duplicate rows, so the
        fingerprint covers everything the issuer gives us that identifies the
        charge. Two genuinely identical same-day charges (two $5 coffees at the
        same shop) collapse into one -- an accepted trade-off, and the
        alternative (importing a statement twice and silently doubling your
        spend) is far worse.
        """
        key = "|".join([
            account_name.strip().lower(),
            self.date.isoformat(),
            re.sub(r"\s+", " ", self.raw_description.strip().lower()),
            f"{self.amount:.2f}",
        ])
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def parse_date(value: str) -> date:
    v = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {value!r}")


def parse_amount(value: str) -> float:
    """Parse issuer amount text into a float.

    Handles $, thousands separators, and accounting-style negatives: ($12.34).
    """
    v = (value or "").strip()
    if not v:
        return 0.0
    negative = v.startswith("(") and v.endswith(")")
    v = v.strip("()").replace("$", "").replace(",", "").replace(" ", "").strip()
    if v in ("", "-"):
        return 0.0
    amount = float(v)
    return -amount if negative else amount


def clean_description(raw: str) -> str:
    """Turn issuer noise into something a human recognises.

    Strips reference numbers, trailing city/province codes, and repeated
    whitespace. Kept conservative: it is better to leave a merchant string
    slightly ugly than to mangle two different merchants into one name.
    """
    s = re.sub(r"\s+", " ", (raw or "").strip())
    s = re.sub(r"\b\d{6,}\b", "", s)                      # long reference numbers
    s = re.sub(r"\s+[A-Z]{2}\s*$", "", s)                 # trailing province code
    s = re.sub(r"^(POS PURCHASE|VISA DEBIT PUR|PURCHASE)\s+", "", s, flags=re.I)
    s = re.sub(r"[#*]{2,}\d*", "", s)
    return re.sub(r"\s+", " ", s).strip(" -,") or (raw or "").strip()


def read_rows(path: Path) -> list[list[str]]:
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        return [row for row in csv.reader(fh) if any(c.strip() for c in row)]


def header_of(rows: Sequence[Sequence[str]]) -> list[str]:
    return [c.strip().lower() for c in rows[0]] if rows else []


class Importer(ABC):
    name: str = "base"

    @classmethod
    @abstractmethod
    def sniff(cls, rows: Sequence[Sequence[str]]) -> bool:
        """Return True if this importer recognises the file's shape."""

    @classmethod
    @abstractmethod
    def parse(cls, rows: Sequence[Sequence[str]]) -> list[ParsedTx]:
        """Convert raw CSV rows into normalised transactions."""
