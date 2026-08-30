"""American Express Canada exports.

Two different files come out of the Amex site and both are handled here.

**Activity export** (`Date, Date Processed, Description, Amount`, sometimes with
a wider set of columns including Merchant, Address and Reference). One format
across the Canadian consumer cards, so Cobalt and SimplyCash both land here.

**Year-end summary** (`Category, Sub-Category, Date, Month-Billed, Transaction,
Charges $, Credits $`). A whole year in one file, with Amex's own categories and
separate charge/credit columns. Its dates are DD/MM/YYYY, which is parsed
explicitly rather than guessed -- 01/02/2025 is the 1st of February, and letting
a generic parser see it as the 2nd of January would silently misfile a month of
transactions.

In both files a purchase is a POSITIVE number, because it is a charge against
the card. The sign is flipped on the way in to match this project's convention
that money leaving you is negative.
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from .base import Importer, ParsedTx, clean_description, header_of, parse_amount, parse_date


class AmexImporter(Importer):
    """The transaction activity export."""

    name = "amex"

    @classmethod
    def sniff(cls, rows: Sequence[Sequence[str]]) -> bool:
        head = header_of(rows)
        if not head:
            return False
        has_date = any(h == "date" or h.startswith("date") for h in head)
        has_desc = "description" in head
        has_amount = "amount" in head
        # 'date processed' is the reliable Amex tell; the wider export adds
        # merchant/address columns, and older exports carried 'card member'.
        amexish = (
            "date processed" in head
            or "foreign spend amount" in head
            or "card member" in head
            or "cardmember" in head
            or "account #" in head
        )
        return has_date and has_desc and has_amount and amexish

    @classmethod
    def parse(cls, rows: Sequence[Sequence[str]]) -> list[ParsedTx]:
        head = header_of(rows)
        idx = {name: i for i, name in enumerate(head)}

        i_date = idx.get("date")
        i_desc = idx.get("description")
        i_amt = idx.get("amount")
        i_extra = idx.get("additional information")
        i_city = idx.get("city / province")
        if i_date is None or i_desc is None or i_amt is None:
            raise ValueError("Amex activity CSV is missing a Date, Description or Amount column")

        out: list[ParsedTx] = []
        for row in rows[1:]:
            if len(row) <= max(i_date, i_desc, i_amt):
                continue
            try:
                d = parse_date(row[i_date])
            except ValueError:
                continue  # blank or footer row
            raw = row[i_desc].strip()
            if not raw:
                continue
            for extra_col in (i_extra, i_city):
                if extra_col is not None and len(row) > extra_col:
                    value = row[extra_col].strip().replace("\n", " ")
                    if value and value.lower() not in raw.lower():
                        raw = f"{raw} {value}"
            amount = -parse_amount(row[i_amt])  # a charge is positive at Amex
            if amount == 0:
                continue
            out.append(ParsedTx(
                date=d,
                description=clean_description(row[i_desc]),
                raw_description=raw,
                amount=round(amount, 2),
            ))
        return out


class AmexYearEndImporter(Importer):
    """The year-end summary export."""

    name = "amex-yearend"

    @classmethod
    def sniff(cls, rows: Sequence[Sequence[str]]) -> bool:
        head = header_of(rows)
        return (
            "sub-category" in head
            and "transaction" in head
            and any(h.startswith("charges") for h in head)
        )

    @classmethod
    def parse(cls, rows: Sequence[Sequence[str]]) -> list[ParsedTx]:
        head = header_of(rows)
        idx = {name: i for i, name in enumerate(head)}

        def col(prefix: str) -> int | None:
            for name, i in idx.items():
                if name.startswith(prefix):
                    return i
            return None

        i_date = idx.get("date")
        i_txn = idx.get("transaction")
        i_charge = col("charges")
        i_credit = col("credits")
        i_cat = idx.get("category")
        i_sub = idx.get("sub-category")
        if i_date is None or i_txn is None or i_charge is None:
            raise ValueError("Amex year-end CSV is missing Date, Transaction or Charges")

        out: list[ParsedTx] = []
        for row in rows[1:]:
            if len(row) <= max(i_date, i_txn, i_charge):
                continue
            raw_date = row[i_date].strip()
            try:
                # Explicitly day-first. Amex writes 14/09/2025 for 14 September.
                d = datetime.strptime(raw_date, "%d/%m/%Y").date()
            except ValueError:
                try:
                    d = parse_date(raw_date)
                except ValueError:
                    continue
            raw = row[i_txn].strip()
            if not raw:
                continue
            charge = parse_amount(row[i_charge]) if len(row) > i_charge else 0.0
            credit = parse_amount(row[i_credit]) if i_credit is not None and len(row) > i_credit else 0.0
            amount = credit - charge  # a charge leaves you, so it goes negative
            if amount == 0:
                continue

            extra = {}
            if i_cat is not None and len(row) > i_cat:
                extra["amex_category"] = row[i_cat].strip()
            if i_sub is not None and len(row) > i_sub:
                extra["amex_subcategory"] = row[i_sub].strip()

            out.append(ParsedTx(
                date=d,
                description=clean_description(raw),
                raw_description=raw,
                amount=round(amount, 2),
                extra=extra,
            ))
        return out
