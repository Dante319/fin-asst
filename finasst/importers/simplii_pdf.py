"""Simplii PDF statements: the Cash Back Visa credit card and the no-fee
chequing account. Simplii's CSV export only covers some accounts and not
everyone remembers to pull it before the export window closes, so these
read the statement PDF directly -- the same reasoning as the EQ Bank
importer in eqbank.py, and they reuse its reconciliation approach: a parse
that cannot be checked against the statement's own totals is not trusted.

Both statements go through pypdf text extraction, which mashes adjacent
table columns together with no separator. The two layouts need different
un-mashing:

**Credit card** ("Your new charges and credits"): each transaction is two
physical lines -- `Trans-date Post-date Description City Prov` then, on the
next line, ` Spend-category Amount`. The amount is unambiguous because it is
alone on its line.

**Chequing** (`your no fee chequing account`): each transaction is one line
ending in two numbers jammed together, e.g. `...528.1860.00`. Column-position
theory does not explain the join order (payments and deposits are ordered
differently), but the running balance does: the FIRST number is always the
balance left by that row, and the SECOND is the transaction amount. That
holds because it lets every row be checked against the previous row's balance
with no ambiguity -- see StatementCheck below.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from .base import ParsedTx, clean_description

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
MONTH_RE = "|".join(MONTHS)

AMOUNT_RE = r"[\d,]+\.\d{2}"


@dataclass
class StatementCheck:
    """Whether the parsed rows reconcile against the statement's own totals."""
    opening: Optional[float]
    closing: Optional[float]
    computed_closing: Optional[float]
    balance_breaks: int = 0

    @property
    def ok(self) -> bool:
        if self.opening is None or self.closing is None or self.computed_closing is None:
            return self.balance_breaks == 0
        return abs(self.computed_closing - self.closing) < 0.01 and self.balance_breaks == 0

    def describe(self) -> str:
        if self.ok:
            note = "balances reconcile"
            if self.closing is not None:
                note = f"reconciles to the ${self.closing:,.2f} closing balance"
            return note
        parts = []
        if self.closing is not None and self.computed_closing is not None:
            drift = self.computed_closing - self.closing
            if abs(drift) >= 0.01:
                parts.append(f"closing balance is off by ${drift:,.2f}")
        if self.balance_breaks:
            parts.append(f"{self.balance_breaks} rows do not match the running balance")
        return "; ".join(parts) or "did not reconcile"


def _money(text: str) -> float:
    return float(text.replace(",", ""))


def _lines(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines()]


# ---------------------------------------------------------------------------
# Credit card (Cash Back Visa)
# ---------------------------------------------------------------------------

# The credit-card period line spells months out in full: "July 11 to
# August 10, 2026" -- unrelated to MONTH_RE, which is Jan/Feb/... for the
# per-row dates.
PERIOD_RE = re.compile(r"([A-Za-z]+)\s+\d{1,2}\s+to\s+([A-Za-z]+)\s+\d{1,2},\s+(\d{4})")
# e.g. "Jul 10 Jul 13 Ý METRO  759               TORONTO      ON"
CC_DATE_LINE_RE = re.compile(
    rf"^(?P<tmon>{MONTH_RE})\s+(?P<tday>\d{{1,2}})\s+"
    rf"(?P<pmon>{MONTH_RE})\s+(?P<pday>\d{{1,2}})\s+"
    rf"(?:Ý\s+)?(?P<desc>\S.*\S|\S)$"
)
# e.g. " Retail and Grocery 32.44" (single-line payment rows also match: the
# description swallows the leading date-free text, category is optional)
CC_AMOUNT_TAIL_RE = re.compile(rf"^(?P<rest>.*?)\s*(?P<amount>{AMOUNT_RE})$")
# e.g. "Jul 30 Jul 31 PAYMENT THANK YOU/PAIEMENT MERCI 430.00" -- a payment
# row is fully self-contained on one line, unlike a charge row.
CC_PAYMENT_LINE_RE = re.compile(
    rf"^(?P<tmon>{MONTH_RE})\s+(?P<tday>\d{{1,2}})\s+"
    rf"(?P<pmon>{MONTH_RE})\s+(?P<pday>\d{{1,2}})\s+"
    rf"(?P<desc>.+?)\s+(?P<amount>{AMOUNT_RE})$"
)


def _statement_year_month(text: str) -> tuple[Optional[int], Optional[int]]:
    """Year (and end-month, for Dec/Jan rollover) from the statement period line."""
    m = PERIOD_RE.search(text)
    if not m:
        return None, None
    try:
        end = datetime.strptime(f"{m.group(2)} {m.group(3)}", "%B %Y")
        return end.year, end.month
    except ValueError:
        return None, None


def _resolve_year(mon: int, period_year: Optional[int], period_end_month: Optional[int]) -> int:
    if period_year is None:
        return date.today().year
    if period_end_month is not None:
        if mon == 12 and period_end_month == 1:
            return period_year - 1
        if mon == 1 and period_end_month == 12:
            return period_year + 1
    return period_year


def _money_line(text: str, label: str) -> Optional[float]:
    """First dollar amount after `label`, tolerating $/=/+/- and spacing in
    between (statements write "New balance = $1,805.02" in one place and
    "New balance $1,805.02" in another)."""
    m = re.search(rf"{re.escape(label)}[^\d]{{0,8}}({AMOUNT_RE})", text)
    return _money(m.group(1)) if m else None


class SimpliiCreditPdfImporter:
    """The Cash Back Visa credit card statement."""

    name = "simplii-cc-pdf"

    @classmethod
    def sniff_text(cls, text: str) -> bool:
        low = text.lower()
        return "simplii" in low and ("cash back visa" in low or "your new charges and credits" in low)

    @classmethod
    def parse_text(cls, text: str) -> tuple[list[ParsedTx], StatementCheck]:
        period_year, period_end_month = _statement_year_month(text)
        lines = _lines(text)

        out: list[ParsedTx] = []

        # "Your payments" -- single-line rows, amount is money paid toward
        # the balance, positive in this app's sign convention (same as Amex).
        try:
            pay_start = next(i for i, l in enumerate(lines) if l == "Your payments")
            pay_end = next(i for i, l in enumerate(lines) if l.startswith("Total payments"))
        except StopIteration:
            pay_start = pay_end = None
        if pay_start is not None:
            for line in lines[pay_start + 1:pay_end]:
                m = CC_PAYMENT_LINE_RE.match(line)
                if not m:
                    continue
                mon = MONTHS[m.group("tmon")]
                yr = _resolve_year(mon, period_year, period_end_month)
                raw = m.group("desc").strip()
                out.append(ParsedTx(
                    date=date(yr, mon, int(m.group("tday"))),
                    description=clean_description(raw),
                    raw_description=raw,
                    amount=round(_money(m.group("amount")), 2),
                ))

        # "Your new charges and credits" -- two physical lines per row: a
        # date+merchant line, then a category+amount line.
        try:
            chg_start = next(i for i, l in enumerate(lines) if l == "Your new charges and credits")
        except StopIteration:
            chg_start = None
        if chg_start is not None:
            # No single line marks the end of this section: it repeats
            # across as many statement pages as there are charges, each with
            # its own "Card number" sub-header and page-boilerplate footer in
            # between. Scanning to the end of the document is safe because
            # only an adjacent date-line + amount-line pair produces a
            # transaction; everything else (headers, legal text, the
            # category-subtotal table) matches neither regex.
            pending_desc = None
            pending_date = None
            for line in lines[chg_start + 1:]:
                date_m = CC_DATE_LINE_RE.match(line)
                if date_m:
                    mon = MONTHS[date_m.group("tmon")]
                    yr = _resolve_year(mon, period_year, period_end_month)
                    pending_date = date(yr, mon, int(date_m.group("tday")))
                    pending_desc = date_m.group("desc").strip()
                    continue
                if pending_date is None:
                    continue
                amt_m = CC_AMOUNT_TAIL_RE.match(line)
                if amt_m:
                    merchant = pending_desc
                    out.append(ParsedTx(
                        date=pending_date,
                        description=clean_description(merchant),
                        raw_description=f"{merchant} ({amt_m.group('rest').strip()})".strip(" ()"),
                        amount=round(-_money(amt_m.group("amount")), 2),  # a charge leaves you
                    ))
                # Pairing is strictly the line right after a date-line, matched
                # or not -- a page break can land between them, and letting
                # pending_date survive past that risks pairing a later,
                # unrelated amount with a stale merchant name.
                pending_date = pending_desc = None

        opening = _money_line(text, "Previous balance")
        closing = _money_line(text, "New balance")
        # Verify against what THIS parse produced, not the statement's own
        # printed subtotals -- a statement that is internally consistent
        # tells us nothing about whether our regexes actually caught every
        # row. A charge lowers the parsed amount (it is negative) and raises
        # the balance owed, so the balance walk is opening MINUS the sum.
        computed_closing = round(opening - sum(t.amount for t in out), 2) if opening is not None else None
        check = StatementCheck(opening=opening, closing=closing, computed_closing=computed_closing)
        return out, check


# ---------------------------------------------------------------------------
# Chequing (no-fee chequing account)
# ---------------------------------------------------------------------------

DC_PERIOD_RE = re.compile(r"statement period:\s*[A-Za-z]+ \d{1,2},\s*\d{4}\s*-\s*([A-Za-z]+) \d{1,2},\s*(\d{4})")
DC_ROW_RE = re.compile(
    rf"^(?P<tmon>{MONTH_RE})\s+(?P<tday>\d{{1,2}})\s+(?P<emon>{MONTH_RE})\s+(?P<eday>\d{{1,2}})\s+"
    rf"(?P<desc>.+?)\s+(?P<bal>{AMOUNT_RE})(?P<amt>{AMOUNT_RE})$"
)
DC_OPENING_RE = re.compile(rf"^(?P<tmon>{MONTH_RE})\s+(?P<tday>\d{{1,2}})\s+(?P<emon>{MONTH_RE})\s+(?P<eday>\d{{1,2}})\s+"
                            rf"BALANCE FORWARD\s+(?P<bal>{AMOUNT_RE})$")


class SimpliiChequingPdfImporter:
    """The no-fee chequing account statement."""

    name = "simplii-chequing-pdf"

    @classmethod
    def sniff_text(cls, text: str) -> bool:
        low = text.lower()
        return "simplii" in low and "no fee chequing account" in low

    @classmethod
    def parse_text(cls, text: str) -> tuple[list[ParsedTx], StatementCheck]:
        m = DC_PERIOD_RE.search(text)
        period_year = int(m.group(2)) if m else None
        period_end_month = MONTHS.get(m.group(1)[:3]) if m else None

        lines = _lines(text)
        out: list[ParsedTx] = []
        opening: Optional[float] = None
        prev_balance: Optional[float] = None
        breaks = 0

        for line in lines:
            opening_m = DC_OPENING_RE.match(line)
            if opening_m:
                opening = _money(opening_m.group("bal"))
                prev_balance = opening
                continue

            row_m = DC_ROW_RE.match(line)
            if not row_m:
                continue
            balance = _money(row_m.group("bal"))
            amount = _money(row_m.group("amt"))
            if prev_balance is not None:
                # Direction comes from the balance delta, not a funds-out /
                # funds-in column -- text extraction jams those two columns
                # together in an order that flips depending on which one is
                # populated, so the balance is the only reliable signal.
                signed = round(balance - prev_balance, 2)
                if abs(abs(signed) - amount) > 0.01:
                    breaks += 1
                amount = signed
            mon = MONTHS[row_m.group("tmon")]
            yr = _resolve_year(mon, period_year, period_end_month)
            raw = row_m.group("desc").strip()
            out.append(ParsedTx(
                date=date(yr, mon, int(row_m.group("tday"))),
                description=clean_description(raw),
                raw_description=raw,
                amount=round(amount, 2),
            ))
            prev_balance = balance

        closing = _money_line(text, "closing balance")
        total_out = _money_line(text, "total funds out")
        total_in = _money_line(text, "total funds in")
        computed_closing = None
        if opening is not None and total_out is not None and total_in is not None:
            computed_closing = round(opening + total_in - total_out, 2)
        check = StatementCheck(opening=opening, closing=closing,
                                computed_closing=computed_closing, balance_breaks=breaks)
        return out, check
