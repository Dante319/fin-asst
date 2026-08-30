#!/usr/bin/env python3
"""Generate synthetic statements in real issuer formats.

These are fake but plausible Toronto-shaped statements: rent, a transit pass,
groceries and takeout, a couple of subscriptions, a monthly remittance to
Chennai, and a salary deposit. Use them to exercise the app before pointing it
at real exports. Committed to the repo on purpose -- nothing here is real.
"""
from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(20260830)

OUT = Path(__file__).resolve().parent / "samples"
MONTHS = 8  # statements ending with the month before today

AMEX_MERCHANTS = [
    ("LOBLAWS #1032 TORONTO ON", (60, 190), 4),
    ("NO FRILLS TORONTO ON", (35, 95), 2),
    ("STARBUCKS #4471 TORONTO ON", (5, 14), 6),
    ("UBER EATS TORONTO ON", (22, 65), 4),
    ("DOORDASH*ORDER TORONTO ON", (28, 72), 2),
    ("PAI NORTHERN THAI TORONTO ON", (45, 120), 1),
    ("LCBO/RAO #219 TORONTO ON", (25, 80), 1),
    ("BAR RAVAL TORONTO ON", (40, 110), 1),
    ("NETFLIX.COM 866-579-7172", (20.99, 20.99), 1),
    ("SPOTIFY P0A4B TORONTO ON", (11.99, 11.99), 1),
    ("APPLE.COM/BILL ITUNES.COM ON", (2.99, 12.99), 1),
    ("AMAZON.CA*MK4TR AMAZON.CA ON", (18, 140), 2),
    ("UBER *TRIP TORONTO ON", (12, 38), 3),
    ("CINEPLEX #8901 TORONTO ON", (16, 45), 1),
    ("SHOPPERS DRUG MART #1290 TORONTO ON", (12, 60), 1),
    ("GOODLIFE FITNESS TORONTO ON", (56.49, 56.49), 1),
]

# (details, direction, amount range, times per month)
SIMPLII_ROWS = [
    ("PAYROLL DEPOSIT POLYAI CANADA", "income", (4150, 4150), 2),
    ("INTERNET BANKING E-TRANSFER RENT", "out", (2150, 2150), 1),
    ("ROGERS COMMUNICATIONS PREAUTH", "out", (72.32, 72.32), 1),
    ("TORONTO HYDRO PREAUTH", "out", (48, 96), 1),
    ("PRESTO/METROLINX TORONTO ON", "out", (156, 156), 1),
    ("WISE PAYMENTS CANADA INR", "out", (600, 800), 1),
    ("WEALTHSIMPLE INVESTMENTS TFSA", "out", (500, 500), 1),
    ("METRO #742 TORONTO ON", "out", (30, 85), 2),
    ("TIM HORTONS #4521 TORONTO ON", "out", (4, 12), 3),
]


def month_starts(n: int) -> list[date]:
    today = date.today().replace(day=1)
    starts = []
    for i in range(n, 0, -1):
        y, m = divmod(today.year * 12 + (today.month - 1) - i, 12)
        starts.append(date(y, m + 1, 1))
    return starts


def rand_day(start: date) -> date:
    nxt = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    span = (nxt - start).days
    return start + timedelta(days=random.randrange(span))


def money(lo: float, hi: float) -> float:
    return round(random.uniform(lo, hi), 2)


def write_amex(path: Path, card_name: str, statement_credit: bool = True) -> None:
    """Amex Canada export: header row, purchases as POSITIVE amounts."""
    rows = [["Date", "Description", "Card Member", "Account #", "Amount", "Additional Information"]]
    for start in month_starts(MONTHS):
        entries = []
        for merchant, (lo, hi), per_month in AMEX_MERCHANTS:
            for _ in range(random.randint(max(per_month - 1, 0), per_month + 1)):
                entries.append((rand_day(start), merchant, money(lo, hi)))
        if statement_credit:
            entries.append((start + timedelta(days=random.randrange(3, 8)),
                            "PAYMENT RECEIVED - THANK YOU",
                            -round(sum(e[2] for e in entries), 2)))
        for d, merchant, amount in sorted(entries):
            rows.append([d.strftime("%m/%d/%Y"), merchant, card_name,
                         "-51004", f"{amount:.2f}", ""])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    print(f"{path.name}: {len(rows) - 1} rows")


def write_simplii(path: Path) -> None:
    """Simplii export: NO header, [date, details, funds out, funds in, balance]."""
    rows = []
    balance = 4200.00
    for start in month_starts(MONTHS):
        entries = []
        for details, kind, (lo, hi), times in SIMPLII_ROWS:
            for _ in range(times):
                entries.append((rand_day(start), details, kind, money(lo, hi)))
        for d, details, kind, amount in sorted(entries):
            if kind == "income":
                balance += amount
                rows.append([d.strftime("%m/%d/%Y"), details, "", f"{amount:.2f}", f"{balance:.2f}"])
            else:
                balance -= amount
                rows.append([d.strftime("%m/%d/%Y"), details, f"{amount:.2f}", "", f"{balance:.2f}"])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    print(f"{path.name}: {len(rows)} rows")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    write_amex(OUT / "sample_amex_simplycash.csv", "SANTOSH KOLAGATI")
    write_simplii(OUT / "sample_simplii_chequing.csv")
    print(f"\nWrote synthetic statements to {OUT}")
