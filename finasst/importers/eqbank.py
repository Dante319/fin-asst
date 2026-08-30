"""EQ Bank PDF statements.

EQ has no CSV export, so the statement PDF is the only machine-readable source.
Text extraction is inherently more fragile than a real export, which is why this
importer verifies its own work: every statement carries a running balance
column, so the parsed transactions must walk the opening balance to the closing
balance exactly. If they do not, the parse is wrong and it says so instead of
quietly returning plausible-looking numbers.

Statement layout:

    Date Description Withdrawals Deposits Balance
    Jan 7 Direct deposit from Nexxt Intellige  $2,000.00 $3,123.45
    Jan 7 Auto-withdrawal by NBC LINE OF CR  - $68.13 $3,055.32

A withdrawal is written with a leading minus, a deposit without. Row dates carry
no year -- it comes from the statement period in the header.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .base import ParsedTx, clean_description

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

TXN_RE = re.compile(
    r"^(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(?P<day>\d{1,2})\s+"
    r"(?P<desc>.*?)\s+(?P<neg>-\s*)?\$(?P<amount>[\d,]+\.\d{2})\s+\$(?P<balance>[\d,]+\.\d{2})$"
)
PERIOD_RE = re.compile(r"([A-Z][a-z]+)\s+\d{1,2},\s+(\d{4})\s+to\s+([A-Z][a-z]+)\s+\d{1,2},\s+(\d{4})")
# Text extraction emits the activity-summary block as four bare amounts and then
# their four labels, so the amounts are read positionally rather than by label:
#   $1,503.45 / + $4,002.00 / - $5,396.56 / = $108.89
#   Opening balance / Total deposits / Total withdrawals / Closing balance
SUMMARY_AMOUNT_RE = re.compile(r"^[+\-=]?\s*\$([\d,]+\.\d{2})$")


def _money(text: str) -> float:
    return float(text.replace(",", ""))


class PdfTextUnavailable(RuntimeError):
    pass


def read_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise PdfTextUnavailable(
            "Reading PDF statements needs pypdf. Run: uv sync"
        ) from exc
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


@dataclass
class StatementCheck:
    """Whether the parsed rows actually reconcile against the statement."""
    opening: Optional[float]
    closing: Optional[float]
    computed_closing: Optional[float]
    balance_breaks: int

    @property
    def ok(self) -> bool:
        """Does the statement add up?

        The test that matters is opening + every parsed amount == closing. If
        that holds, no row was missed, duplicated or misread, because any of
        those would move the total.

        Row-by-row balance breaks are reported but are NOT a failure on their
        own: statements list same-day transactions in an order that does not
        always match the balance column, and ordering changes no aggregate this
        app computes. Only when the closing balance is unavailable does the
        walk become the only evidence there is.
        """
        if self.opening is None or self.closing is None or self.computed_closing is None:
            return self.balance_breaks == 0
        return abs(self.computed_closing - self.closing) < 0.01

    def describe(self) -> str:
        if self.opening is not None and self.closing is not None and self.ok:
            note = f"reconciles to the ${self.closing:,.2f} closing balance"
            if self.balance_breaks:
                note += f" ({self.balance_breaks} same-day rows listed out of balance order)"
            return note
        if self.ok:
            return "balances reconcile"
        parts = []
        if self.closing is not None and self.computed_closing is not None:
            drift = self.computed_closing - self.closing
            if abs(drift) >= 0.01:
                parts.append(f"closing balance is off by ${drift:,.2f}")
        if self.balance_breaks:
            parts.append(f"{self.balance_breaks} rows do not match the running balance")
        return "; ".join(parts) or "did not reconcile"


class EQBankImporter:
    """Not a CSV Importer -- it consumes extracted PDF text instead of rows."""

    name = "eqbank"

    @classmethod
    def sniff_text(cls, text: str) -> bool:
        return "eqbank.ca" in text.lower() or "eq bank" in text.lower()

    @classmethod
    def statement_year_month(cls, text: str) -> tuple[Optional[int], Optional[int]]:
        match = PERIOD_RE.search(text)
        if not match:
            return None, None
        try:
            start = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%B %Y")
            return start.year, start.month
        except ValueError:
            return None, None

    @classmethod
    def summary_balances(cls, lines: list[str]) -> tuple[Optional[float], Optional[float]]:
        """Opening and closing balance from the activity-summary block."""
        try:
            start = next(i for i, l in enumerate(lines) if l.lower().startswith("your activity summary"))
        except StopIteration:
            return None, None
        amounts = []
        for line in lines[start + 1:start + 9]:
            m = SUMMARY_AMOUNT_RE.match(line)
            if m:
                amounts.append(_money(m.group(1)))
            elif amounts:
                break
        if len(amounts) >= 4:
            return amounts[0], amounts[3]
        return None, None

    @classmethod
    def parse_text(cls, text: str) -> tuple[list[ParsedTx], StatementCheck]:
        year, month = cls.statement_year_month(text)
        lines_all = [l.strip() for l in text.splitlines() if l.strip()]
        opening, closing = cls.summary_balances(lines_all)

        out: list[ParsedTx] = []
        balances: list[float] = []
        for line in (l.strip() for l in text.splitlines()):
            match = TXN_RE.match(line)
            if not match:
                continue
            mon = MONTHS[match.group("mon")]
            row_year = year
            if year and month:
                # A statement can carry a row from the tail of the previous
                # month or the head of the next one; keep the year sane.
                if mon == 12 and month == 1:
                    row_year = year - 1
                elif mon == 1 and month == 12:
                    row_year = year + 1
            amount = _money(match.group("amount"))
            if match.group("neg"):
                amount = -amount
            raw = re.sub(r"\s+", " ", match.group("desc")).strip()
            out.append(ParsedTx(
                date=date(row_year or date.today().year, mon, int(match.group("day"))),
                description=clean_description(raw),
                raw_description=raw,
                amount=round(amount, 2),
            ))
            balances.append(_money(match.group("balance")))

        # Walk the running balance: each row's stated balance must equal the
        # previous one plus that row's amount.
        breaks = 0
        for i in range(1, len(balances)):
            if abs((balances[i - 1] + out[i].amount) - balances[i]) > 0.01:
                breaks += 1
        if opening is not None and balances:
            if abs((opening + out[0].amount) - balances[0]) > 0.01:
                breaks += 1

        computed = round(opening + sum(t.amount for t in out), 2) if opening is not None else None
        return out, StatementCheck(opening, closing, computed, breaks)
