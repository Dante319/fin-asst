"""Spending analysis over the imported transactions.

Every figure here comes from rows in the database -- nothing is modelled or
estimated. The modelling lives in goals.py, deliberately kept separate so you
can always tell measured facts from projections. The one exception is declared
by name: average_surplus() can return method="declared", in which case the
income half came from a regime you stated rather than from statements, and it
says so in its own warning.

Two rules this module has learned the hard way:

* Inflows and outflows are summed SEPARATELY, never netted before they are
  classified. Netting first meant a $500 deposit landing in an uncategorised
  month cancelled $500 of real spending, and the resulting surplus was too
  high with nothing on screen to say so.
* A transaction the transfer matcher paired with its other half in another
  account is money that never left. It is excluded here by
  `transfer_peer_id IS NULL`, regardless of what category it carries -- the
  matcher's finding has to reach the totals or there was no point making it.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from . import income
from .config import NON_SPEND_CATEGORIES, group_of

# Every aggregate in this module starts from this: real money, not an internal
# movement the app has already accounted for on the other side.
NOT_AN_INTERNAL_MOVE = "transfer_peer_id IS NULL"


def month_key(d: str) -> str:
    return d[:7]


def _shift_month(ym: str, months: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    total = y * 12 + (m - 1) + months
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def valid_month(value: Optional[str]) -> Optional[str]:
    """Accept YYYY-MM and nothing else.

    A month arrives from a query string, and `_shift_month` does int() on
    slices of it, so anything else is a 500 on the dashboard.
    """
    if not value:
        return None
    text = str(value).strip()
    if len(text) != 7 or text[4] != "-":
        return None
    try:
        year, month = int(text[:4]), int(text[5:])
    except ValueError:
        return None
    if not (1 <= month <= 12 and 1900 <= year <= 2999):
        return None
    return f"{year:04d}-{month:02d}"


def months_available(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT substr(date, 1, 7) AS ym FROM transactions ORDER BY ym DESC"
    ).fetchall()
    return [r["ym"] for r in rows]


# ---------------------------------------------------------------------------
# Statement coverage
# ---------------------------------------------------------------------------
# A month is not finished because the calendar says so. It is finished when the
# statements covering it are all in. Get this wrong and every merchant on the
# account you have not imported yet appears to have stopped charging you.

DORMANT_DAYS = 75          # past this, an account is treated as closed, not late


def _last_day(ym: str) -> str:
    year, month = int(ym[:4]), int(ym[5:7])
    if month == 12:
        return f"{year}-12-31"
    first_of_next = date(year, month + 1, 1)
    return (first_of_next - timedelta(days=1)).isoformat()


def account_coverage(conn: sqlite3.Connection) -> list[dict]:
    """How far each account's statements actually reach."""
    return [
        {"account": r["name"], "first": r["f"], "last": r["l"], "count": int(r["n"])}
        for r in conn.execute(
            """SELECT a.name, MIN(t.date) f, MAX(t.date) l, COUNT(*) n
               FROM transactions t JOIN accounts a ON a.id = t.account_id
               GROUP BY a.name ORDER BY a.name""")
    ]


def coverage_end(conn: sqlite3.Connection, dormant_days: int = DORMANT_DAYS) -> Optional[str]:
    """The last date EVERY still-active account has statements up to.

    The weakest account sets the limit, because a month is only whole when
    every account that was in use during it has been imported. An account that
    has been silent for `dormant_days` past the newest data is treated as
    closed rather than late -- otherwise one card you stopped using would hold
    the whole app back for good.
    """
    accounts = account_coverage(conn)
    if not accounts:
        return None
    newest = max(a["last"] for a in accounts)
    cutoff = (date.fromisoformat(newest) - timedelta(days=dormant_days)).isoformat()
    active = [a for a in accounts if a["last"] >= cutoff]
    return min(a["last"] for a in active) if active else newest


# How much of a month's tail may be missing before it stops being usable.
# Two answers, because two kinds of question have different sensitivity:
#
#   0 days  -- "did this merchant stop charging me?" and "did this bill change
#              price?" A single missing day at the end of the month is enough
#              to make a subscription that bills on the 30th look cancelled.
#   5 days  -- totals, category comparisons and the forecast. Four missing days
#              out of thirty-one moves a monthly total by a few percent; throwing
#              the whole month away to avoid that loses far more than it saves.
STRICT_TOLERANCE_DAYS = 0
SETTLED_TOLERANCE_DAYS = 5


def complete_months(conn: sqlite3.Connection, tolerance_days: int = STRICT_TOLERANCE_DAYS) -> list[str]:
    """Months whose statements are in, oldest first.

    This is the window detectors and the forecast work in. The calendar month
    in progress is excluded automatically: its last day is in the future, so no
    statement reaches it.

    `tolerance_days` is how much of the month's tail may be missing. Pass 0 when
    a gap at the end of the month could invent a finding; see the constants
    above for which questions need which.
    """
    end = coverage_end(conn)
    if end is None:
        return []
    limit = (date.fromisoformat(end) + timedelta(days=max(int(tolerance_days), 0))).isoformat()
    return sorted(m for m in months_available(conn) if _last_day(m) <= limit)


def coverage_gap(conn: sqlite3.Connection) -> Optional[str]:
    """A sentence naming the account that is holding the window back."""
    accounts = account_coverage(conn)
    if len(accounts) < 2:
        return None
    end = coverage_end(conn)
    newest = max(a["last"] for a in accounts)
    if end is None or end >= newest:
        return None
    behind = sorted(a for a in accounts if a["last"] == end)
    names = ", ".join(a["account"] for a in behind)
    settled = complete_months(conn, SETTLED_TOLERANCE_DAYS)
    strict = complete_months(conn, STRICT_TOLERANCE_DAYS)
    last_settled = settled[-1] if settled else "nothing"
    note = (
        f"{names} only reaches {end}, while your other accounts reach {newest}. "
        f"Figures below stop at {last_settled} rather than reporting a half-imported "
        "month as one where your spending suddenly fell."
    )
    if strict and settled and strict[-1] != settled[-1]:
        note += (
            f" Findings about a merchant starting or stopping stop a month earlier "
            f"({strict[-1]}), because a few missing days at the end of a month are "
            "enough to make a subscription look cancelled."
        )
    return note


def monthly_totals(conn: sqlite3.Connection) -> list[dict]:
    """Income, spend and surplus for every month with data.

    Transfers, matched internal movements and contributions to savings are
    excluded: a credit-card payment is not income to the chequing account's
    owner, and money moved into a TFSA has not been spent.

    Money arriving with no rule to explain it is NOT counted as income. It goes
    to `unknown_income`, which the surplus warning reads. Counting it would
    turn a one-off repayment from a friend into a permanent raise.
    """
    rows = conn.execute(
        f"""SELECT substr(date, 1, 7) AS ym,
                   COALESCE(category, 'Uncategorised') AS cat,
                   SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS out_,
                   SUM(CASE WHEN amount > 0 THEN  amount ELSE 0 END) AS in_
            FROM transactions
            WHERE {NOT_AN_INTERNAL_MOVE}
            GROUP BY ym, cat
            ORDER BY ym"""
    ).fetchall()

    by_month: dict[str, dict] = defaultdict(
        lambda: {"income": 0.0, "spend": 0.0, "saved": 0.0,
                 "uncategorised": 0.0, "unknown_income": 0.0}
    )
    for r in rows:
        bucket = by_month[r["ym"]]
        cat, out, inflow = r["cat"], float(r["out_"]), float(r["in_"])
        if cat == "Income":
            # An outflow filed as Income is a clawback -- a reversed deposit.
            bucket["income"] += inflow - out
        elif cat == "Savings & Investments":
            bucket["saved"] += out - inflow
        elif cat == "Transfer":
            continue
        elif cat == "Uncategorised":
            bucket["spend"] += out
            bucket["uncategorised"] += out
            bucket["unknown_income"] += inflow
        else:
            # An inflow inside a spend category is a refund and genuinely
            # reduces that month's spending.
            bucket["spend"] += out - inflow

    out_rows = []
    for ym in sorted(by_month):
        b = by_month[ym]
        out_rows.append({
            "month": ym,
            "income": round(b["income"], 2),
            "spend": round(b["spend"], 2),
            "saved": round(b["saved"], 2),
            "surplus": round(b["income"] - b["spend"] - b["saved"], 2),
            "uncategorised": round(b["uncategorised"], 2),
            "unknown_income": round(b["unknown_income"], 2),
        })
    return out_rows


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


def _window(conn: sqlite3.Connection, month: Optional[str], months: int) -> Optional[tuple[str, str]]:
    available = months_available(conn)
    if not available:
        return None
    end = valid_month(month) or available[0]
    span = max(int(months or 1), 1)          # 0 or a negative would invert the range
    return _shift_month(end, -(span - 1)), end


def category_breakdown(
    conn: sqlite3.Connection, month: Optional[str] = None, months: int = 1
) -> list[dict]:
    """Spend by category. `month` is YYYY-MM; omit it for the latest month."""
    window = _window(conn, month, months)
    if window is None:
        return []
    start, end = window

    rows = conn.execute(
        f"""SELECT COALESCE(category, 'Uncategorised') AS cat,
                   SUM(-amount) AS spend, COUNT(*) AS n
            FROM transactions
            WHERE substr(date, 1, 7) BETWEEN ? AND ?
              AND amount < 0 AND {NOT_AN_INTERNAL_MOVE}
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


def top_merchants(
    conn: sqlite3.Connection, month: Optional[str] = None, months: int = 1, limit: int = 15
) -> list[dict]:
    window = _window(conn, month, months)
    if window is None:
        return []
    start, end = window
    rows = conn.execute(
        f"""SELECT description, COALESCE(category, 'Uncategorised') AS cat,
                   SUM(-amount) AS spend, COUNT(*) AS n
            FROM transactions
            WHERE substr(date, 1, 7) BETWEEN ? AND ? AND amount < 0
              AND {NOT_AN_INTERNAL_MOVE}
              AND COALESCE(category, '') NOT IN ('Transfer', 'Savings & Investments')
            GROUP BY lower(description)
            ORDER BY spend DESC LIMIT ?""",
        (start, end, max(int(limit or 1), 1)),
    ).fetchall()
    return [
        {"merchant": r["description"], "category": r["cat"],
         "spend": round(float(r["spend"]), 2), "count": r["n"],
         "group": group_of(r["cat"])}
        for r in rows
    ]


def _quality_warnings(used: list[dict]) -> list[str]:
    """What is wrong with the months a baseline was built from."""
    warnings = []
    unc = sum(m["uncategorised"] for m in used)
    spend = sum(m["spend"] for m in used)
    if spend > 0 and unc / spend > 0.10:
        warnings.append(
            f"{100 * unc / spend:.0f}% of spending in the months used is still "
            "uncategorised, so this surplus is approximate."
        )
    unknown_in = sum(m["unknown_income"] for m in used)
    if unknown_in > 0:
        warnings.append(
            f"${unknown_in:,.0f} arrived in those months with no rule to explain it. "
            "It is NOT counted as income here -- categorise it, because if any of it "
            "is pay this surplus is too low, and if none of it is, it is correct."
        )
    return warnings


def average_surplus(conn: sqlite3.Connection, lookback: int = 3) -> dict:
    """The monthly surplus the goal engine spends.

    Uses whole months only -- a partial current month would understate income
    or spending depending on when in the month you imported, and a surplus
    figure that swings with the import date is worse than no figure.

    If an income regime is declared (see finasst/income.py), the average never
    crosses its start: months in which you were paid by a job you no longer
    have describe an income you no longer receive. When the regime is too new
    to have enough whole months of its own, the baseline switches method --
    declared pay minus observed outgoings -- and `method` says so, because a
    figure built from a declaration deserves to be labelled differently from
    one built from statements.
    """
    empty = {"surplus": 0.0, "months_used": 0, "basis": [], "method": "none",
             "regime": None, "basis_label": "no complete months yet"}
    totals = monthly_totals(conn)
    if not totals:
        return {**empty, "warning": "No transactions imported yet."}

    today = date.today().strftime("%Y-%m")
    complete = [m for m in totals if m["month"] < today]
    if not complete:
        return {
            **empty,
            "warning": "Only the current (incomplete) month has data -- import a "
                       "full month before trusting projections.",
        }

    lookback = max(int(lookback or 1), 1)
    regime = income.current_regime(conn)
    warnings: list[str] = []

    if regime is not None:
        eligible = [m for m in complete if m["month"] >= regime.first_whole_month]

        if len(eligible) < income.MIN_REGIME_MONTHS:
            # The situation this whole mechanism exists for: the income changed
            # so recently that no honest average of it exists yet. Take the
            # income from the declaration and the outgoings from the statements,
            # and be explicit that the two halves came from different places.
            used = complete[-lookback:]
            outgoings = sum(m["spend"] + m["saved"] for m in used) / len(used)
            declared = income.declared_monthly(conn)
            span = ", ".join(m["month"] for m in used)
            how_many = ("no whole month of statements under it yet"
                        if not eligible else
                        "only one whole month of statements under it")
            warnings.append(
                f"{regime.name} began {regime.started_on.isoformat()} and there is "
                f"{how_many}, which is too few to average. This surplus is declared pay "
                f"(${declared:,.0f}/month, from ${regime.amount:,.0f} {regime.label}) minus "
                f"your average outgoings over {span} (${outgoings:,.0f}/month). "
                "The spending half is measured; the income half is what you told the app."
            )
            warnings += _quality_warnings(used)
            return {
                "surplus": round(declared - outgoings, 2),
                "months_used": len(used),
                "basis": [m["month"] for m in used],
                "method": "declared",
                "regime": regime.name,
                "basis_label": f"declared pay less spending in {span}",
                "warning": " ".join(warnings) or None,
            }

        if len(eligible) < len(complete):
            warnings.append(
                f"Months before {regime.first_whole_month} are excluded: {regime.name} "
                f"began {regime.started_on.isoformat()}, and a surplus averaged across "
                "that change describes the income you used to have."
            )
        drifted = income.drift(conn, regime, totals)
        if drifted:
            warnings.append(drifted)
        candidates = eligible
    else:
        candidates = complete

    used = candidates[-lookback:]
    surplus = sum(m["surplus"] for m in used) / len(used)
    warnings += _quality_warnings(used)
    span = ", ".join(m["month"] for m in used)
    return {
        "surplus": round(surplus, 2),
        "months_used": len(used),
        "basis": [m["month"] for m in used],
        "method": "observed",
        "regime": regime.name if regime else None,
        "basis_label": f"average of {span}",
        "warning": " ".join(warnings) or None,
    }


def uncategorised(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT t.*, a.name AS account_name FROM transactions t
           JOIN accounts a ON a.id = t.account_id
           WHERE t.category IS NULL
           ORDER BY abs(t.amount) DESC LIMIT ?""",
        (max(int(limit or 1), 1),),
    ).fetchall()


def uncategorised_count(conn: sqlite3.Connection) -> int:
    """Count them in SQL. Fetching a capped page and taking len() of it both
    lies above the cap and drags a thousand rows across to produce one integer."""
    return int(conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category IS NULL"
    ).fetchone()[0])


# ---------------------------------------------------------------------------
# Recurring charges
# ---------------------------------------------------------------------------

RECURRING_MIN_MONTHS = 3
RECURRING_MAX_VARIATION = 0.35   # coefficient of variation, month-to-month
RECURRING_STALE_MONTHS = 2       # months of silence before it stops counting


def recurring_charges(
    conn: sqlite3.Connection,
    min_months: int = RECURRING_MIN_MONTHS,
    max_variation: float = RECURRING_MAX_VARIATION,
    stale_after: int = RECURRING_STALE_MONTHS,
) -> list[dict]:
    """Merchants charging a similar amount month after month -- your fixed cost.

    Three things this has to get right, and an earlier version got all three
    wrong by asking SQL for AVG(-amount) over every transaction:

    * The unit is a MONTH, not a charge. A shop visited weekly at $50 costs
      $200 a month, not $50; averaging per transaction understates it fourfold.
    * "Similar amount" has to be tested. Without it, a merchant billing $5,
      $500 and $2,000 across three months is reported as a $835/month fixed
      cost, ranked above the real ones.
    * A subscription cancelled last year is not a fixed cost. Anything silent
      for `stale_after` months past the newest data is dropped.

    `monthly` is what it actually costs in a month it charges you, and
    `variation` is how much that wobbles (0 = identical every month).
    """
    rows = conn.execute(
        f"""SELECT lower(description) AS key, description, substr(date, 1, 7) AS ym,
                   SUM(-amount) AS spent, COUNT(*) AS n,
                   COALESCE(category, 'Uncategorised') AS cat
            FROM transactions
            WHERE amount < 0 AND {NOT_AN_INTERNAL_MOVE}
              AND COALESCE(category, '') NOT IN ('Transfer', 'Savings & Investments')
            GROUP BY key, ym
            ORDER BY key, ym"""
    ).fetchall()
    if not rows:
        return []

    latest = max(r["ym"] for r in rows)
    cutoff = _shift_month(latest, -max(int(stale_after), 0))

    merchants: dict[str, dict] = {}
    for r in rows:
        m = merchants.setdefault(r["key"], {
            "merchant": r["description"], "category": r["cat"],
            "months": [], "amounts": [], "count": 0, "last": r["ym"],
        })
        m["months"].append(r["ym"])
        m["amounts"].append(float(r["spent"]))
        m["count"] += int(r["n"])
        m["last"] = max(m["last"], r["ym"])

    out = []
    for m in merchants.values():
        n = len(m["amounts"])
        if n < max(int(min_months), 1) or m["last"] < cutoff:
            continue
        mean = sum(m["amounts"]) / n
        if mean <= 0:
            continue
        spread = (sum((a - mean) ** 2 for a in m["amounts"]) / n) ** 0.5
        variation = spread / mean
        if variation > max_variation:
            continue
        out.append({
            "merchant": m["merchant"],
            "category": m["category"],
            "group": group_of(m["category"]),
            "months": n,
            "monthly": round(mean, 2),
            "avg_amount": round(mean, 2),   # kept: older callers read this name
            "variation": round(variation, 3),
            "charges": m["count"],
            "last_seen": m["last"],
        })
    out.sort(key=lambda r: r["monthly"], reverse=True)
    return out


def fixed_monthly_cost(conn: sqlite3.Connection) -> float:
    """What you would still pay in a month you did nothing."""
    return round(sum(r["monthly"] for r in recurring_charges(conn)), 2)
