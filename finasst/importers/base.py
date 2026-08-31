"""Shared importer plumbing: the parsed row, fingerprinting, and the ABC."""
from __future__ import annotations

import csv
import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

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
        fingerprint is account + date + cleaned merchant + amount.

        It deliberately uses the CLEANED description rather than the raw one.
        The same charge can appear in two different Amex exports with different
        raw text -- the activity export appends the merchant address, the
        year-end summary does not -- and those two files overlap by a couple of
        weeks in December. Fingerprinting the raw string would let that fortnight
        be counted twice.

        Two genuinely identical same-day charges (two $5 coffees at the same
        shop) collapse into one. That is the accepted trade-off, and it is far
        better than silently doubling a month of spending.
        """
        key = "|".join([
            account_name.strip().lower(),
            self.date.isoformat(),
            re.sub(r"\s+", " ", self.description.strip().lower()),
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


def _cell_to_str(value) -> str:
    """Render one openpyxl cell as the string the CSV-shaped importers expect.

    Excel stores numbers and dates as real types, not text, so a column that
    a CSV export would have written as "82.14" or "2026-07-03" arrives here
    as a float or a datetime instead. Reformat both back into the plain
    strings parse_amount / parse_date already know how to read, rather than
    teaching every importer two input shapes.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float):
        # Excel has no distinct integer type; 82.0 should read as "82", not
        # "82.0", so a whole-number amount still parses the same as a CSV's.
        return f"{value:.10g}"
    return str(value).strip()


def read_excel_rows(path: Path) -> list[list[str]]:
    """Read the first sheet of an .xlsx/.xls workbook into CSV-shaped rows.

    Only the active (first) sheet is read. A statement export is one table on
    one sheet; if a workbook ever has more, that is a sign it needs its own
    importer rather than a guess at which sheet is the real one.
    """
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ImportError("Reading Excel statements needs openpyxl. Run: uv sync") from exc
    wb = load_workbook(str(path), read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows = [
            [_cell_to_str(cell) for cell in row]
            for row in ws.iter_rows(values_only=True)
        ]
    finally:
        wb.close()
    return [row for row in rows if any(c.strip() for c in row)]


def header_of(rows: Sequence[Sequence[str]]) -> list[str]:
    return [c.strip().lower() for c in rows[0]] if rows else []


@dataclass
class StatementCheck:
    """Whether parsed rows reconcile against a statement's own stated totals.

    Shared by every statement parser so that "reconciles" means one thing.

    The primary test is always the same: opening + every parsed amount ==
    closing. If that holds, no row was missed, duplicated or misread, since any
    of those would move the total.

    Row-by-row balance breaks are weighted differently depending on how the
    parser got its amounts, which is why `walk_is_evidence` exists rather than
    each importer quietly applying its own rule:

    * A parser that reads amounts from their own column (EQ Bank) treats a break
      as cosmetic -- statements list same-day rows in an order the balance column
      disagrees with, and ordering changes no aggregate. `walk_is_evidence=False`.
    * A parser that DERIVES which number is the amount from the balance chain
      (Simplii chequing, where the two numbers arrive ambiguously) has no
      independent reading, so a break means the derivation itself failed and the
      row cannot be trusted. `walk_is_evidence=True`.
    """
    opening: Optional[float]
    closing: Optional[float]
    computed_closing: Optional[float]
    balance_breaks: int = 0
    walk_is_evidence: bool = False

    @property
    def ok(self) -> bool:
        if self.opening is None or self.closing is None or self.computed_closing is None:
            return self.balance_breaks == 0
        totals_agree = abs(self.computed_closing - self.closing) < 0.01
        if self.walk_is_evidence:
            return totals_agree and self.balance_breaks == 0
        return totals_agree

    def describe(self) -> str:
        if self.ok:
            if self.closing is not None:
                note = f"reconciles to the ${self.closing:,.2f} closing balance"
                if self.balance_breaks:
                    note += f" ({self.balance_breaks} same-day rows listed out of balance order)"
                return note
            return "balances reconcile"
        parts = []
        if self.closing is not None and self.computed_closing is not None:
            drift = self.computed_closing - self.closing
            if abs(drift) >= 0.01:
                parts.append(f"closing balance is off by ${drift:,.2f}")
        if self.balance_breaks:
            parts.append(f"{self.balance_breaks} rows do not match the running balance")
        return "; ".join(parts) or "did not reconcile"


MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
MONTH_RE = "|".join(MONTHS)


def money(text: str) -> float:
    """Parse a bare statement amount such as '2,314.13'."""
    return float(str(text).replace(",", "").replace("$", "").strip())


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
