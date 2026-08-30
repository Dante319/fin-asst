"""Fallback importer for any CSV with a header row.

Guesses which columns mean date / description / amount, so a new card, an
investment export or an INR statement can be loaded without new code. If the
guess is wrong you can override it explicitly via `column_map`.
"""
from __future__ import annotations

from typing import Sequence

from .base import Importer, ParsedTx, clean_description, header_of, parse_amount, parse_date

DATE_HINTS = ("date", "transaction date", "posted", "posting date", "txn date")
DESC_HINTS = ("description", "details", "merchant", "narration", "particulars", "memo", "payee")
AMOUNT_HINTS = ("amount", "value", "transaction amount", "amt")
DEBIT_HINTS = ("debit", "funds out", "withdrawal", "money out", "paid out")
CREDIT_HINTS = ("credit", "funds in", "deposit", "money in", "paid in")


def _find(head: Sequence[str], hints: Sequence[str]) -> int | None:
    for hint in hints:
        for i, name in enumerate(head):
            if name == hint:
                return i
    for hint in hints:
        for i, name in enumerate(head):
            if hint in name:
                return i
    return None


class GenericImporter(Importer):
    name = "generic"

    @classmethod
    def sniff(cls, rows: Sequence[Sequence[str]]) -> bool:
        head = header_of(rows)
        if not head or len(rows) < 2:
            return False
        has_date = _find(head, DATE_HINTS) is not None
        has_money = (
            _find(head, AMOUNT_HINTS) is not None
            or _find(head, DEBIT_HINTS) is not None
            or _find(head, CREDIT_HINTS) is not None
        )
        return has_date and has_money

    @classmethod
    def parse(cls, rows: Sequence[Sequence[str]], column_map: dict | None = None) -> list[ParsedTx]:
        head = header_of(rows)
        cm = column_map or {}
        i_date = cm.get("date", _find(head, DATE_HINTS))
        i_desc = cm.get("description", _find(head, DESC_HINTS))
        i_amt = cm.get("amount", _find(head, AMOUNT_HINTS))
        i_debit = cm.get("debit", _find(head, DEBIT_HINTS))
        i_credit = cm.get("credit", _find(head, CREDIT_HINTS))

        if i_date is None:
            raise ValueError("Could not find a date column in this CSV")
        if i_amt is None and i_debit is None and i_credit is None:
            raise ValueError("Could not find an amount column in this CSV")

        # A single signed amount column is ambiguous: some issuers write a
        # purchase as positive, others as negative. Decide from the data --
        # for a spending account most rows are outflows, so if the signed
        # column is overwhelmingly positive we treat positive as spend.
        flip = False
        if i_amt is not None:
            values = []
            for row in rows[1:]:
                if len(row) > i_amt:
                    try:
                        values.append(parse_amount(row[i_amt]))
                    except ValueError:
                        pass
            nonzero = [v for v in values if v]
            if nonzero and sum(1 for v in nonzero if v > 0) / len(nonzero) > 0.8:
                flip = True

        out: list[ParsedTx] = []
        for row in rows[1:]:
            if len(row) <= i_date:
                continue
            try:
                d = parse_date(row[i_date])
            except ValueError:
                continue
            raw = row[i_desc].strip() if i_desc is not None and len(row) > i_desc else ""
            if i_amt is not None and len(row) > i_amt:
                amount = parse_amount(row[i_amt])
                if flip:
                    amount = -amount
            else:
                debit = parse_amount(row[i_debit]) if i_debit is not None and len(row) > i_debit else 0.0
                credit = parse_amount(row[i_credit]) if i_credit is not None and len(row) > i_credit else 0.0
                amount = credit - debit
            if amount == 0:
                continue
            out.append(ParsedTx(
                date=d,
                description=clean_description(raw),
                raw_description=raw or "(no description)",
                amount=round(amount, 2),
            ))
        return out
