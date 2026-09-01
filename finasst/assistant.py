"""The catalogue of questions the assistant is allowed to ask the database.

The point of this module is what it does NOT do. A language model asked "how
much did I spend on eating out in July" could read a list of transactions and
add them up, and it would be right most of the time. Most of the time is not
good enough for money, and a wrong total delivered fluently is worse than no
answer, because nothing about it looks wrong.

So the model never sees rows to total. It picks a question from this catalogue
and gets back the same figure the web page would show, computed by the same
code. Its job is choosing the query and explaining the result -- not arithmetic.

Two properties every tool here holds to:

* **Read-only.** The connection is opened through SQLite's own read-only mode,
  so this is a guarantee the code cannot lose track of, not a convention.
* **Caveats travel with the number.** Every result carries the window it was
  computed over, and says when that window is short, when spending is partly
  uncategorised, or when a fifth of the money is going somewhere unseen. A
  figure that arrives without its qualifications will be quoted without them.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Callable, Optional

from . import analytics, config, coverage, db, goals as goals_mod, insights, rules

MAX_ROWS = 200


class DataNotReady(RuntimeError):
    """The database exists but the app has never set it up."""


def open_db(path=None) -> sqlite3.Connection:
    conn = db.connect_readonly(path)
    try:
        conn.execute("SELECT transfer_peer_id FROM transactions LIMIT 1").fetchone()
    except sqlite3.OperationalError as exc:
        raise DataNotReady(
            "This database predates the current schema and the assistant cannot "
            "migrate it, because it is read-only by design. Run `finasst init` "
            "once with the app itself, then ask again."
        ) from exc
    return conn


# ---------------------------------------------------------------------------
# Shared caveats
# ---------------------------------------------------------------------------

def _window(conn) -> dict:
    settled = analytics.complete_months(conn, analytics.SETTLED_TOLERANCE_DAYS)
    strict = analytics.complete_months(conn, analytics.STRICT_TOLERANCE_DAYS)
    return {
        "months_with_complete_statements": settled,
        "months_safe_for_start_stop_findings": strict,
        "statement_coverage_ends": analytics.coverage_end(conn),
        "coverage_gap": analytics.coverage_gap(conn),
    }


def _quality(conn, months: Optional[list[str]] = None) -> list[str]:
    """Everything a person should know before quoting a figure from this data."""
    notes: list[str] = []
    gap = analytics.coverage_gap(conn)
    if gap:
        notes.append(gap)

    totals = analytics.monthly_totals(conn)
    covered = set(months or analytics.complete_months(conn, analytics.SETTLED_TOLERANCE_DAYS))
    used = [m for m in totals if m["month"] in covered]
    spend = sum(m["spend"] for m in used)
    unc = sum(m["uncategorised"] for m in used)
    if spend > 0 and unc / spend > 0.05:
        notes.append(
            f"{100 * unc / spend:.0f}% of spending in this window is uncategorised "
            f"(${unc:,.0f}), so category figures are lower bounds."
        )
    unknown_in = sum(m["unknown_income"] for m in used)
    if unknown_in > 0:
        notes.append(
            f"${unknown_in:,.0f} arrived in this window with no rule to explain it. It is "
            "NOT counted as income, so surplus and forecast figures are conservative."
        )
    cov = coverage.coverage(conn)
    if cov.gap_ratio > 0.05:
        notes.append(
            f"${cov.invisible:,.0f} ({100 * cov.gap_ratio:.0f}% of all money out) went to "
            "accounts with no statements here. Spending figures cover only the rest."
        )
    warning = coverage.income_regime_warning(conn)
    if warning:
        notes.append(warning)
    return notes


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def describe_data(conn) -> dict:
    """What is in the database and how far you can trust it. Call this first."""
    return {
        "accounts": analytics.account_coverage(conn),
        "window": _window(conn),
        "months_with_any_data": analytics.months_available(conn),
        "uncategorised_transactions": analytics.uncategorised_count(conn),
        "transactions": int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]),
        "categories_in_use": config.CATEGORIES,
        "category_groups": config.CATEGORY_GROUPS,
        "today": date.today().isoformat(),
        "caveats": _quality(conn),
    }


def monthly_totals(conn) -> dict:
    settled = set(analytics.complete_months(conn, analytics.SETTLED_TOLERANCE_DAYS))
    rows = analytics.monthly_totals(conn)
    for row in rows:
        row["statements_complete"] = row["month"] in settled
    return {"months": rows, "window": _window(conn), "caveats": _quality(conn),
            "note": "Rows with statements_complete=false are partly imported. Do not "
                    "compare them with complete months or describe them as a fall in spending."}


def category_breakdown(conn, month: Optional[str] = None, months: int = 1) -> dict:
    rows = analytics.category_breakdown(conn, month, months)
    return {"month": analytics.valid_month(month), "window_months": months,
            "categories": rows, "total": round(sum(r["spend"] for r in rows), 2),
            "caveats": _quality(conn)}


def top_merchants(conn, month: Optional[str] = None, months: int = 1, limit: int = 15) -> dict:
    return {"merchants": analytics.top_merchants(conn, month, months, min(int(limit), 50)),
            "caveats": _quality(conn)}


def search_transactions(
    conn, query: str = "", category: str = "", month: str = "",
    min_amount: float = 0.0, limit: int = 50,
) -> dict:
    """Actual rows. For inspecting specific charges, not for totalling them --
    the total is capped and would be a total of the page, not of the data."""
    sql = ["""SELECT t.date, t.description, t.amount, t.category, t.category_source,
                     a.name AS account FROM transactions t
              JOIN accounts a ON a.id = t.account_id WHERE 1=1"""]
    params: list = []
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql.append(r"AND (t.description LIKE ? ESCAPE '\' OR t.raw_description LIKE ? ESCAPE '\')")
        params += [f"%{escaped}%", f"%{escaped}%"]
    if category in config.CATEGORIES:
        sql.append("AND t.category = ?")
        params.append(category)
    if analytics.valid_month(month):
        sql.append("AND substr(t.date, 1, 7) = ?")
        params.append(analytics.valid_month(month))
    if min_amount:
        sql.append("AND abs(t.amount) >= ?")
        params.append(abs(float(min_amount)))
    sql.append("ORDER BY t.date DESC, t.id DESC LIMIT ?")
    capped = min(max(int(limit or 1), 1), MAX_ROWS)
    params.append(capped)
    rows = [dict(r) for r in conn.execute(" ".join(sql), params)]

    count_sql = " ".join(sql[:-1]).replace(
        """SELECT t.date, t.description, t.amount, t.category, t.category_source,
                     a.name AS account FROM transactions t""", "SELECT COUNT(*) FROM transactions t")
    total = int(conn.execute(count_sql, params[:-1]).fetchone()[0])
    return {
        "matched": total, "returned": len(rows), "transactions": rows,
        "note": ("This is a page of rows, not a total. If you need a sum, use "
                 "category_breakdown or top_merchants -- summing this list gives the "
                 "total of the page." if total > len(rows) else
                 "Every matching row is here."),
    }


def recurring_charges(conn) -> dict:
    return {
        "charges_regularly": analytics.recurring_charges(conn),
        "note": "Merchants charging a similar amount in at least three months. Gaps are "
                "allowed, so this includes things that are not committed costs.",
        "caveats": _quality(conn),
    }


def committed_costs(conn) -> dict:
    items = sorted(insights.fixed_costs(conn).values(), key=lambda f: -f["monthly"])
    return {
        "committed": [{k: v for k, v in f.items() if k != "by_month"} for f in items],
        "monthly_total": round(sum(f["monthly"] for f in items), 2),
        "note": "Stricter than recurring_charges: one charge, every month of the window, "
                "steady amount. This is what goes out whether or not you do anything. "
                "Rent paid by a variable method may not qualify and can fall outside this.",
        "caveats": _quality(conn),
    }


def findings(conn, limit: int = 20) -> dict:
    found = insights.all_insights(conn, limit=min(int(limit), 50))
    return {
        "findings": [
            {"kind": f.kind, "severity": f.severity, "headline": f.headline,
             "detail": f.detail, "arithmetic": f.basis, "amount": round(f.amount, 2),
             "merchant": f.merchant, "category": f.category, "month": f.month}
            for f in found
        ],
        "note": "Each finding carries the arithmetic it came from in `arithmetic`. Quote it "
                "when you report the finding; a surprising claim without its sum invites "
                "the reader to distrust everything else.",
        "caveats": _quality(conn),
    }


def forecast(conn, months: int = 3) -> dict:
    f = insights.forecast(conn, ahead=min(max(int(months or 1), 1), 12))
    return {
        "usable": f.ok, "months": f.months, "basis": f.basis, "warnings": f.warnings,
        "fixed": f.fixed, "variable_typical": f.variable_typical,
        "variable_low": f.variable_low, "variable_high": f.variable_high,
        "monthly_income": f.monthly_income, "income_source": f.income_source,
        "note": "Report the range, not just the midpoint. A single number reads as a promise.",
    }


def surplus(conn) -> dict:
    lookback = int(float(db.get_setting(conn, "surplus_lookback_months", "3") or 3))
    result = analytics.average_surplus(conn, lookback)
    result["note"] = ("This is the figure the goal engine spends. `method` says how it was "
                      "derived; when it is not measured from statements alone, say so.")
    return result


def coverage_report(conn) -> dict:
    cov = coverage.coverage(conn)
    return {
        "total_money_out": cov.total_outflow,
        "visible_spending": cov.visible_spend,
        "matched_internal_transfers": cov.matched_internal,
        "unexplained_transfers": cov.unmatched_outflow,
        "to_untracked_savings": cov.to_untracked_savings,
        "invisible_total": cov.invisible,
        "share_of_money_out_unseen": round(cov.gap_ratio, 4),
        "destinations": cov.destinations[:30],
        "accounts_imported": cov.accounts,
        "verdict": cov.verdict(),
    }


def goals(conn, surplus_override: Optional[float] = None) -> dict:
    every = goals_mod.load_goals(conn)
    if not every:
        return {"goals": [], "note": "No goals have been set up."}
    used = surplus_override
    if used is None:
        used = analytics.average_surplus(
            conn, int(float(db.get_setting(conn, "surplus_lookback_months", "3") or 3))
        )["surplus"]
    projection = goals_mod.project(
        every, used,
        annual_return_rate=float(db.get_setting(conn, "annual_return_rate", "0.04") or 0.04),
        annual_inflation=float(db.get_setting(conn, "annual_inflation", "0.025") or 0.025),
    )
    return {
        "surplus_used": used,
        "surplus_was_overridden": surplus_override is not None,
        "total_committed_monthly": projection.total_committed_monthly,
        "warnings": projection.warnings,
        "goals": [
            {"name": g.name, "target_after_inflation": g.target_amount,
             "wanted_by": g.target_date.isoformat() if g.target_date else None,
             "projected": g.projected_date.isoformat() if g.projected_date else None,
             "on_track": g.on_track, "slip_months": g.slip_months,
             "needs_monthly": g.required_monthly,
             "shortfall_on_the_date": g.shortfall_at_target_date, "note": g.note}
            for g in projection.goals
        ],
        "note": "All goals draw on ONE surplus in priority order. A goal is only on track "
                "once the goals above it have taken their share.",
    }


def explain_category(conn, description: str) -> dict:
    result = rules.explain(conn, description)
    return {
        "description": description,
        "category": result.category,
        "decided_by": (
            {"pattern": result.winner.pattern, "match_type": result.winner.match_type,
             "priority": result.winner.priority, "is_yours": result.winner.is_user}
            if result.winner else None),
        "also_matched": [
            {"pattern": r.pattern, "category": r.category, "priority": r.priority}
            for r in result.also_matched],
        "note": ("Nothing matches, so a transaction like this stays uncategorised."
                 if result.winner is None else ""),
    }


def uncategorised(conn, limit: int = 30) -> dict:
    rows = analytics.uncategorised(conn, min(int(limit), MAX_ROWS))
    return {
        "count": analytics.uncategorised_count(conn),
        "biggest": [
            {"date": r["date"], "description": r["description"],
             "amount": r["amount"], "account": r["account_name"]}
            for r in rows
        ],
        "note": "Largest first. These are excluded from income and reported as a lower "
                "bound in spending, which is why they matter more than their count suggests.",
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _tool(name: str, fn: Callable, description: str, properties: dict, required=None) -> dict:
    return {
        "name": name, "fn": fn, "description": description,
        "inputSchema": {"type": "object", "properties": properties,
                        "required": required or [], "additionalProperties": False},
    }


MONTH = {"type": "string", "description": "Month as YYYY-MM. Omit for the latest."}
WINDOW = {"type": "integer", "description": "How many months back from `month` to include.",
          "default": 1}

TOOLS: list[dict] = [
    _tool("describe_data", describe_data,
          "What is in this database and how far it can be trusted: accounts, how far each "
          "one's statements reach, which months are complete, how much is uncategorised. "
          "CALL THIS FIRST in any conversation — every other figure depends on it.", {}),
    _tool("monthly_totals", monthly_totals,
          "Income, spending, savings and surplus for every month, flagged for whether that "
          "month's statements are all in.", {}),
    _tool("category_breakdown", category_breakdown,
          "Spending by category for a month or a window of months.",
          {"month": MONTH, "months": WINDOW}),
    _tool("top_merchants", top_merchants,
          "Biggest merchants by spend for a month or window.",
          {"month": MONTH, "months": WINDOW,
           "limit": {"type": "integer", "default": 15, "maximum": 50}}),
    _tool("search_transactions", search_transactions,
          "Individual transactions matching a merchant search, category, month or minimum "
          "amount. For inspecting specific charges. Do NOT sum these to answer a 'how much' "
          "question — the result is a capped page, so the sum would be wrong. Use "
          "category_breakdown or top_merchants for totals.",
          {"query": {"type": "string", "description": "Merchant text to search for."},
           "category": {"type": "string", "enum": config.CATEGORIES},
           "month": MONTH,
           "min_amount": {"type": "number", "description": "Only charges at least this large."},
           "limit": {"type": "integer", "default": 50, "maximum": MAX_ROWS}}),
    _tool("recurring_charges", recurring_charges,
          "Merchants charging a similar amount in at least three months. Gaps allowed.", {}),
    _tool("committed_costs", committed_costs,
          "The stricter set: one charge, every month, steady amount — what goes out whether "
          "or not you do anything.", {}),
    _tool("findings", findings,
          "The insight engine's ranked findings: duplicate charges, price changes, "
          "subscriptions starting or stopping, unusual charges, category spikes. Each "
          "carries the arithmetic behind it.",
          {"limit": {"type": "integer", "default": 20, "maximum": 50}}),
    _tool("forecast", forecast,
          "Projected income, fixed and variable spending, and surplus for the coming months, "
          "with the range from the cheapest and dearest months observed.",
          {"months": {"type": "integer", "default": 3, "maximum": 12}}),
    _tool("surplus", surplus,
          "The monthly surplus baseline the goal engine spends, with how it was derived and "
          "any warning attached to it.", {}),
    _tool("coverage_report", coverage_report,
          "How much money left for accounts this app has no statements for, and where it "
          "went. Essential context for any spending figure.", {}),
    _tool("goals", goals,
          "Savings goals projected against one shared monthly surplus in priority order.",
          {"surplus_override": {"type": "number",
                                "description": "Try a different monthly surplus."}}),
    _tool("explain_category", explain_category,
          "Which rule decides the category for a given merchant description, and what it beat.",
          {"description": {"type": "string"}}, ["description"]),
    _tool("uncategorised", uncategorised,
          "Transactions no rule matched, largest first.",
          {"limit": {"type": "integer", "default": 30, "maximum": MAX_ROWS}}),
]

BY_NAME = {t["name"]: t for t in TOOLS}


def call(conn, name: str, arguments: dict[str, Any] | None = None) -> Any:
    tool = BY_NAME.get(name)
    if tool is None:
        raise KeyError(f"No such tool: {name}. Available: {', '.join(sorted(BY_NAME))}")
    allowed = set(tool["inputSchema"]["properties"])
    kwargs = {k: v for k, v in (arguments or {}).items() if k in allowed and v is not None}
    return tool["fn"](conn, **kwargs)


INSTRUCTIONS = """\
This server exposes one person's own financial data, computed by the same code that
renders their local finance app. Every figure you get back is already calculated.

How to use it well:

1. Call `describe_data` before anything else. It tells you which months have complete
   statements and how much is uncategorised. Figures for a half-imported month are not
   comparable with complete ones, and describing one as a fall in spending is wrong.
2. Do not do arithmetic on transaction lists. `search_transactions` returns a capped
   page, so summing it gives the total of the page, not of the data. Totals come from
   `category_breakdown`, `top_merchants`, `committed_costs` or `monthly_totals`.
3. Pass on the caveats. Every result carries a `caveats` list: uncategorised spending,
   money going to accounts with no statements, an income change mid-window. A figure
   quoted without them will be acted on as if it were complete.
4. Findings carry their own `arithmetic`. Quote it. A surprising claim without its sum
   invites the reader to distrust the ones that are right too.
5. This server is read-only and cannot categorise anything, add rules, or change goals.
   Suggest those as actions for the person to take in the app.
6. Amounts are Canadian dollars. Negative means money left the account.
"""
