"""Spending analysis over the imported transactions.

Every figure here comes from rows in the database -- nothing is modelled or
estimated. The modelling lives in goals.py, deliberately kept separate so you
can always tell measured facts from projections.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date
from typing import Optional

from .config import NON_SPEND_CATEGORIES, group_of


def month_key(d: str) -> str:
    return d[:7]


def _shift_month(ym: str, months: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    total = y * 12 + (m - 1) + months
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def months_available(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT substr(date, 1, 7) AS ym FROM transactions ORDER BY ym DESC"
    ).fetchall()
    return [r["ym"] for r in rows]


def monthly_totals(conn: sqlite3.Connection) -> list[dict]:
    """Income, spend and surplus for every month with data.

    Transfers and contributions to savings are excluded from both sides: a
    credit card payment is not income to the chequing account's owner, and
    money moved into a TFSA has not been spent.
    """
    rows = conn.execute(
        """SELECT substr(date, 1, 7) AS ym,
                  COALESCE(category, 'Uncategorised') AS cat,
                  SUM(amount) AS total
           FROM transactions
           GROUP BY ym, cat
           ORDER BY ym"""
    ).fetchall()

    by_month: dict[str, dict] = defaultdict(
        lambda: {"income": 0.0, "spend": 0.0, "saved": 0.0, "uncategorised": 0.0}
    )
    for r in rows:
        bucket = by_month[r["ym"]]
        cat, total = r["cat"], float(r["total"])
        if cat == "Income":
            bucket["income"] += total
        elif cat == "Savings & Investments":
            bucket["saved"] += -total
        elif cat == "Transfer":
            continue
        elif cat == "Uncategorised":
            bucket["uncategorised"] += -total
            bucket["spend"] += -total
        else:
            bucket["spend"] += -total

    out = []
    for ym in sorted(by_month):
        b = by_month[ym]
        out.append({
            "month": ym,
            "income": round(b["income"], 2),
            "spend": round(b["spend"], 2),
            "saved": round(b["saved"], 2),
            "surplus": round(b["income"] - b["spend"] - b["saved"], 2),
            "uncategorised": round(b["uncategorised"], 2),
        })
    return out


def category_breakdown(
    conn: sqlite3.Connection, month: Optional[str] = None, months: int = 1
) -> list[dict]:
    """Spend by category. `month` is YYYY-MM; omit it for the latest month."""
    available = months_available(conn)
    if not available:
        return []
    end = month or available[0]
    start = _shift_month(end, -(months - 1))

    rows = conn.execute(
        """SELECT COALESCE(category, 'Uncategorised') AS cat,
                  SUM(-amount) AS spend, COUNT(*) AS n
           FROM transactions
           WHERE substr(date, 1, 7) BETWEEN ? AND ?
             AND amount < 0
           GROUP BY cat
           HAVING spend > 0
           ORDER BY spend DESC""",
        (start, end),
    ).fetchall()

    result = [
        {"category": r["cat"], "spend": round(float(r["spend"]), 2), "count": r["n"],
         "group": group_of(r["cat"])}
        for r in rows
        if r["cat"] not in NON_SPEND_CATEGORIES
    ]
    total = sum(r["spend"] for r in result) or 1.0
    for r in result:
        r["share"] = round(100 * r["spend"] / total, 1)
    return result


def group_breakdown(categories: list[dict]) -> list[dict]:
    """Roll a category breakdown up into the four spend groups.

    Takes the already-computed category rows rather than re-querying, so the
    group totals can never disagree with the category totals they summarise.
    """
    totals: dict[str, dict] = {}
    for row in categories:
        bucket = totals.setdefault(
            row["group"], {"name": row["group"], "spend": 0.0, "count": 0, "categories": []}
        )
        bucket["spend"] += row["spend"]
        bucket["count"] += row["count"]
        bucket["categories"].append(row["category"])
    grand = sum(b["spend"] for b in totals.values()) or 1.0
    out = sorted(totals.values(), key=lambda b: b["spend"], reverse=True)
    for b in out:
        b["spend"] = round(b["spend"], 2)
        b["share"] = round(100 * b["spend"] / grand, 1)
    return out


def top_merchants(
    conn: sqlite3.Connection, month: Optional[str] = None, months: int = 1, limit: int = 15
) -> list[dict]:
    available = months_available(conn)
    if not available:
        return []
    end = month or available[0]
    start = _shift_month(end, -(months - 1))
    rows = conn.execute(
        """SELECT description, COALESCE(category, 'Uncategorised') AS cat,
                  SUM(-amount) AS spend, COUNT(*) AS n
           FROM transactions
           WHERE substr(date, 1, 7) BETWEEN ? AND ? AND amount < 0
             AND COALESCE(category, '') NOT IN ('Transfer', 'Savings & Investments')
           GROUP BY lower(description)
           ORDER BY spend DESC LIMIT ?""",
        (start, end, limit),
    ).fetchall()
    return [
        {"merchant": r["description"], "category": r["cat"],
         "spend": round(float(r["spend"]), 2), "count": r["n"],
         "group": group_of(r["cat"])}
        for r in rows
    ]


def average_surplus(conn: sqlite3.Connection, lookback: int = 3) -> dict:
    """The monthly surplus the goal engine spends.

    Uses whole months only -- a partial current month would understate income
    or spending depending on when in the month you imported, and a surplus
    figure that swings with the import date is worse than no figure.
    """
    totals = monthly_totals(conn)
    if not totals:
        return {"surplus": 0.0, "months_used": 0, "basis": [], "warning": "No transactions imported yet."}

    today = date.today().strftime("%Y-%m")
    complete = [m for m in totals if m["month"] < today]
    if not complete:
        return {
            "surplus": 0.0, "months_used": 0, "basis": [],
            "warning": "Only the current (incomplete) month has data -- import a full month before trusting projections.",
        }

    used = complete[-lookback:]
    surplus = sum(m["surplus"] for m in used) / len(used)
    warning = None
    unc = sum(m["uncategorised"] for m in used)
    spend = sum(m["spend"] for m in used) or 1
    if unc / spend > 0.10:
        warning = (
            f"{100 * unc / spend:.0f}% of spending in the months used is still uncategorised, "
            "so this surplus is approximate."
        )
    return {
        "surplus": round(surplus, 2),
        "months_used": len(used),
        "basis": [m["month"] for m in used],
        "warning": warning,
    }


def uncategorised(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT t.*, a.name AS account_name FROM transactions t
           JOIN accounts a ON a.id = t.account_id
           WHERE t.category IS NULL
           ORDER BY abs(t.amount) DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def recurring_charges(conn: sqlite3.Connection, min_months: int = 3) -> list[dict]:
    """Merchants charging a similar amount in most months -- your true fixed cost."""
    rows = conn.execute(
        """SELECT lower(description) AS key, description,
                  COUNT(DISTINCT substr(date, 1, 7)) AS months,
                  AVG(-amount) AS avg_amount,
                  COALESCE(category, 'Uncategorised') AS cat
           FROM transactions
           WHERE amount < 0
           GROUP BY key
           HAVING months >= ?
           ORDER BY avg_amount * months DESC""",
        (min_months,),
    ).fetchall()
    return [
        {"merchant": r["description"], "months": r["months"],
         "avg_amount": round(float(r["avg_amount"]), 2), "category": r["cat"],
         "group": group_of(r["cat"])}
        for r in rows
    ]
