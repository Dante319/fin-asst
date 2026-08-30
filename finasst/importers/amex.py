"""American Express Canada CSV export.

Covers both the Cobalt and the SimplyCash Preferred -- Amex uses one export
format across its Canadian consumer cards. Amex writes a purchase as a
POSITIVE number (it is a charge to the card) and a payment or refund as
negative, so the sign is flipped to match this project's convention.
"""
from __future__ import annotations

from typing import Sequence

from .base import Importer, ParsedTx, clean_description, header_of, parse_amount, parse_date


class AmexImporter(Importer):
    name = "amex"

    @classmethod
    def sniff(cls, rows: Sequence[Sequence[str]]) -> bool:
        head = header_of(rows)
        if not head:
            return False
        joined = " ".join(head)
        has_date = any(h.startswith("date") for h in head)
        has_amount = "amount" in head
        amexish = any(k in joined for k in ("card member", "cardmember", "account #", "appears on your statement as"))
        return has_date and has_amount and amexish

    @classmethod
    def parse(cls, rows: Sequence[Sequence[str]]) -> list[ParsedTx]:
        head = header_of(rows)
        idx = {name: i for i, name in enumerate(head)}

        def col(*candidates: str) -> int | None:
            for c in candidates:
                if c in idx:
                    return idx[c]
            for c in candidates:
                for name, i in idx.items():
                    if name.startswith(c):
                        return i
            return None

        i_date = col("date")
        i_desc = col("description", "merchant")
        i_amt = col("amount")
        i_extra = col("additional information", "appears on your statement as")
        if i_date is None or i_desc is None or i_amt is None:
            raise ValueError("Amex CSV is missing a Date, Description or Amount column")

        out: list[ParsedTx] = []
        for row in rows[1:]:
            if len(row) <= max(i_date, i_desc, i_amt):
                continue
            try:
                d = parse_date(row[i_date])
            except ValueError:
                continue  # footer or blank row
            raw = row[i_desc].strip()
            if i_extra is not None and len(row) > i_extra and row[i_extra].strip():
                raw = f"{raw} {row[i_extra].strip()}"
            amount = -parse_amount(row[i_amt])  # Amex: charge is positive; we want negative
            out.append(ParsedTx(
                date=d,
                description=clean_description(raw),
                raw_description=raw,
                amount=round(amount, 2),
            ))
        return out
