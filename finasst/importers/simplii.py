"""Simplii Financial CSV export (Visa and chequing).

Simplii exports without a header row, in the CIBC-style layout:
    Date, Transaction Details, Funds Out, Funds In[, Balance]
Exactly one of the two amount columns is populated per row.
"""
from __future__ import annotations

from typing import Sequence

from .base import Importer, ParsedTx, clean_description, parse_amount, parse_date


class SimpliiImporter(Importer):
    name = "simplii"

    @classmethod
    def sniff(cls, rows: Sequence[Sequence[str]]) -> bool:
        if not rows:
            return False
        first = rows[0]
        if len(first) < 4:
            return False
        # Headerless: the first cell of the first row must already be a date.
        try:
            parse_date(first[0])
        except ValueError:
            return False
        # And the two amount columns must be mutually exclusive.
        out, inn = first[2].strip(), first[3].strip()
        return bool(out) != bool(inn) or (not out and not inn)

    @classmethod
    def parse(cls, rows: Sequence[Sequence[str]]) -> list[ParsedTx]:
        out: list[ParsedTx] = []
        for row in rows:
            if len(row) < 4:
                continue
            try:
                d = parse_date(row[0])
            except ValueError:
                continue
            raw = row[1].strip()
            funds_out = parse_amount(row[2])
            funds_in = parse_amount(row[3])
            amount = funds_in - funds_out  # outflow becomes negative
            if amount == 0:
                continue
            out.append(ParsedTx(
                date=d,
                description=clean_description(raw),
                raw_description=raw,
                amount=round(amount, 2),
            ))
        return out
