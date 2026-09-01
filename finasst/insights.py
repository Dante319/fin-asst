"""Findings the app can defend, and a forecast that shows its working.

Every insight here is arithmetic over rows you can go and look at. Each one
carries the numbers it was computed from (`basis`) and a link to the
transactions behind it (`link`), because a finance app telling you something
surprising has to be able to prove it — otherwise the honest response to a
surprising claim is to distrust the app, and then the good findings go
unread too.

Deliberately absent: anything that needs a model to decide. A language model
can help you *read* these (see finasst/llm.py) and can suggest categories for
merchants no rule matched, but nothing in this file asks one anything. A
number you would act on is computed here or not at all.

Silence is a valid output. Every detector below refuses to fire without enough
history, and says nothing rather than dressing up noise as a finding.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from statistics import median
from typing import Optional
from urllib.parse import urlencode

from . import analytics, income
from .config import NON_SPEND_CATEGORIES, group_of

# --- thresholds, in one place so they can be argued with -------------------
MIN_HISTORY_MONTHS = 3          # below this, no detector fires
PRICE_CHANGE_MIN_PCT = 0.05     # a 5% move on a bill is worth knowing about
PRICE_CHANGE_MIN_ABS = 1.00
PRICE_CHANGE_MAX_WOBBLE = 0.10   # a bill is steady; a favourite bar is not
PRICE_CHANGE_MAX_PCT = 0.60      # beyond this it is an outlier, not a repricing
SPIKE_MIN_RATIO = 1.5           # a category 50% above its own median
SPIKE_MIN_ABS = 75.00
UNUSUAL_MIN_RATIO = 3.0         # a charge 3x that merchant's usual
UNUSUAL_MIN_ABS = 100.00
DUPLICATE_MIN_ABS = 50.00
DUPLICATE_WINDOW_DAYS = 3
FORECAST_MONTHS = 3

SEVERITY_ORDER = {"flag": 0, "notable": 1, "watch": 2}


@dataclass
class Insight:
    kind: str
    severity: str            # flag | notable | watch
    headline: str
    detail: str
    basis: str               # the arithmetic, written out
    amount: float = 0.0
    merchant: str = ""
    category: str = ""
    month: str = ""
    link: str = ""

    @property
    def group(self) -> str:
        return group_of(self.category) if self.category else ""


def _link(**params) -> str:
    clean = {k: v for k, v in params.items() if v}
    return "/transactions?" + urlencode(clean) if clean else "/transactions"


def fixed_costs(conn: sqlite3.Connection, window: int = 6) -> dict[str, dict]:
    """Merchants that charged a steady amount in EVERY one of the last `window`
    complete months.

    Stricter than analytics.recurring_charges, deliberately. That function
    answers "what charges you regularly", which allows gaps — and a weekly
    grocery shop or a bi-monthly hydro bill both qualify. This one answers
    "what is committed", which is a stronger claim and the one the forecast and
    the fixed-cost finding both make out loud. Getting it wrong put a grocery
    bill and half a hydro bill into a figure described as money that goes out
    whether or not you do anything.
    """
    months = analytics.complete_months(conn, analytics.SETTLED_TOLERANCE_DAYS)[-window:]
    if len(months) < MIN_HISTORY_MONTHS:
        return {}
    out = {}
    for key, entry in _merchant_months(conn).items():
        amounts = [entry["by_month"].get(m) for m in months]
        if any(a is None for a in amounts):
            continue                      # skipped a month: not committed
        # A bill charges ONCE. A grocery shop visited weekly can total a steady
        # amount month after month and still be entirely discretionary --
        # calling it money that goes out whether or not you do anything is the
        # difference between a fixed cost and a habit.
        if any(entry["counts"].get(m, 0) != 1 for m in months):
            continue
        mean = sum(amounts) / len(amounts)
        if mean <= 0:
            continue
        spread = (sum((a - mean) ** 2 for a in amounts) / len(amounts)) ** 0.5
        if spread / mean > analytics.RECURRING_MAX_VARIATION:
            continue                      # too wobbly to be a bill
        out[key] = {"merchant": entry["merchant"], "category": entry["category"],
                    "monthly": round(mean, 2), "by_month": entry["by_month"],
                    "months": months}
    return out


def committed_monthly(conn: sqlite3.Connection, window: int = 6) -> float:
    return round(sum(f["monthly"] for f in fixed_costs(conn, window).values()), 2)


def _merchant_months(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per merchant, what it charged in each month it charged at all."""
    rows = conn.execute(
        f"""SELECT lower(description) AS key, description, substr(date, 1, 7) AS ym,
                   SUM(-amount) AS spent, COUNT(*) AS n,
                   COALESCE(category, 'Uncategorised') AS cat
            FROM transactions
            WHERE amount < 0 AND {analytics.NOT_AN_INTERNAL_MOVE}
              AND COALESCE(category, '') NOT IN ('Transfer', 'Savings & Investments')
            GROUP BY key, ym ORDER BY key, ym"""
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        entry = out.setdefault(r["key"], {"merchant": r["description"], "category": r["cat"],
                                          "by_month": {}, "counts": {}, "charges": 0})
        entry["by_month"][r["ym"]] = float(r["spent"])
        entry["counts"][r["ym"]] = int(r["n"])
        entry["charges"] += int(r["n"])
    return out


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def price_changes(conn: sqlite3.Connection, months: list[str]) -> list[Insight]:
    """A bill you already pay, at a different price than it used to be.

    Only for merchants charging once a month, every month — a shop you visit a
    variable number of times has a variable total by definition, and calling
    that a price rise would be nonsense.
    """
    if len(months) < MIN_HISTORY_MONTHS + 1:
        return []
    recent, earlier = months[-1], months[:-1]
    found = []
    for entry in _merchant_months(conn).values():
        by_month = entry["by_month"]
        history = [by_month[m] for m in earlier if m in by_month]
        if recent not in by_month or len(history) < MIN_HISTORY_MONTHS:
            continue
        if entry["charges"] != len(by_month):      # more than one charge a month
            continue
        # ...and one charge in EVERY month. Ontario hydro bills every second
        # month; calling $200 bi-monthly "$200 a month" and annualising it made
        # the yearly figure twice the truth, which is the number a person would
        # actually budget against.
        if not _charged_every_month(by_month, months):
            continue
        was, now = median(history), by_month[recent]
        # A price change is a change to a PRICE, which means the old amount has
        # to have been a price: steady, month after month. Without this test a
        # favourite bar visited once a month reads as a $580 price rise, which
        # is true arithmetic and a useless finding -- unusual_charge is the
        # detector that has something to say about it.
        if was > 0:
            spread = (sum((a - was) ** 2 for a in history) / len(history)) ** 0.5
            if spread / was > PRICE_CHANGE_MAX_WOBBLE:
                continue
        delta = now - was
        if abs(delta) < PRICE_CHANGE_MIN_ABS or was <= 0 or abs(delta) / was < PRICE_CHANGE_MIN_PCT:
            continue
        # A subscription going $20.99 -> $24.99 is a price rise. A merchant going
        # $60 -> $640 is not: nobody reprices by ten times. That is a one-off,
        # and unusual_charge is the detector that says so properly.
        if abs(delta) / was > PRICE_CHANGE_MAX_PCT:
            continue
        direction = "went up" if delta > 0 else "came down"
        found.append(Insight(
            kind="price_change",
            severity="notable" if delta > 0 else "watch",
            headline=f"{entry['merchant']} {direction} to ${now:,.2f}",
            detail=(f"It billed ${was:,.2f} a month across {len(history)} earlier months "
                    f"and ${now:,.2f} in {recent} — {delta:+,.2f} a month, "
                    f"{delta * 12:+,.0f} a year if it stays."),
            basis=f"median of {len(history)} monthly charges = ${was:,.2f}; {recent} = ${now:,.2f}",
            amount=delta, merchant=entry["merchant"], category=entry["category"], month=recent,
            link=_link(q=entry["merchant"][:24]),
        ))
    return found


def _charged_every_month(by_month: dict, window: list[str]) -> bool:
    """Did this merchant charge in EVERY month of this window?

    "Recurring" has to mean consecutive. Without this test a merchant appearing
    in four of the last eight months -- a restaurant you like -- reads as a
    subscription that has just been cancelled, which is both wrong and the kind
    of wrong that teaches you to ignore the page.
    """
    return bool(window) and all(m in by_month for m in window)


def recurring_started_or_stopped(conn: sqlite3.Connection, months: list[str]) -> list[Insight]:
    """Something new charging every month, or something regular that stopped."""
    if len(months) < MIN_HISTORY_MONTHS + 1:
        return []
    recent = months[-1]
    settled = months[:-1]
    found = []
    for entry in _merchant_months(conn).values():
        by_month = entry["by_month"]
        charged = [m for m in months if m in by_month]
        if len(charged) < MIN_HISTORY_MONTHS:
            continue
        typical = median(by_month.values())

        # Stopped: charged in at least three of the settled months, including
        # the last one, and then nothing.
        if recent not in by_month:
            run = settled[-MIN_HISTORY_MONTHS:]
            if _charged_every_month(by_month, run):
                found.append(Insight(
                    kind="recurring_stopped",
                    severity="watch",
                    headline=f"{entry['merchant']} stopped charging",
                    detail=(f"It billed about ${typical:,.2f} a month in {len(run)} months up to "
                            f"{run[-1]}, then nothing in {recent}. Either you cancelled it, or a "
                            "payment did not go through."),
                    basis=f"charged in {', '.join(run[-4:])}; absent in {recent}",
                    amount=-typical, merchant=entry["merchant"], category=entry["category"],
                    month=recent, link=_link(q=entry["merchant"][:24]),
                ))
            continue

        # Started: nothing before, then a run ending in the most recent month.
        first_seen = charged[0]
        if first_seen != months[0] and _charged_every_month(by_month, months[-MIN_HISTORY_MONTHS:]):
            months_before = months.index(first_seen)
            if months_before >= MIN_HISTORY_MONTHS and _charged_every_month(
                    by_month, months[months_before:]):
                found.append(Insight(
                    kind="recurring_started",
                    severity="watch",
                    headline=f"{entry['merchant']} is now a monthly charge",
                    detail=(f"Nothing for the first {months_before} months of your history, then "
                            f"${typical:,.2f} a month since {first_seen} — "
                            f"${typical * 12:,.0f} a year."),
                    basis=f"first charge {first_seen}; charged in {len(charged)} months since",
                    amount=typical, merchant=entry["merchant"], category=entry["category"],
                    month=first_seen, link=_link(q=entry["merchant"][:24]),
                ))
    return found


def category_spikes(conn: sqlite3.Connection, months: list[str]) -> list[Insight]:
    """A category well above what that same category usually costs you."""
    if len(months) < MIN_HISTORY_MONTHS + 1:
        return []
    recent = months[-1]
    history: dict[str, list[float]] = {}
    for m in months[:-1]:
        for row in analytics.category_breakdown(conn, m, 1):
            history.setdefault(row["category"], []).append(row["spend"])

    found = []
    for row in analytics.category_breakdown(conn, recent, 1):
        past = history.get(row["category"], [])
        if len(past) < MIN_HISTORY_MONTHS:
            continue
        usual = median(past)
        delta = row["spend"] - usual
        if usual <= 0 or delta < SPIKE_MIN_ABS or row["spend"] / usual < SPIKE_MIN_RATIO:
            continue
        found.append(Insight(
            kind="category_spike",
            severity="notable",
            headline=f"{row['category']} was ${delta:,.0f} above its usual in {recent}",
            detail=(f"${row['spend']:,.0f} across {row['count']} transactions, against a "
                    f"${usual:,.0f} median over the previous {len(past)} months."),
            basis=f"median of {len(past)} months = ${usual:,.0f}; {recent} = ${row['spend']:,.0f}",
            amount=delta, category=row["category"], month=recent,
            link=_link(category=row["category"], month=recent),
        ))
    return found


def unusual_charges(conn: sqlite3.Connection, months: list[str]) -> list[Insight]:
    """One charge far outside what that merchant normally costs."""
    if len(months) < MIN_HISTORY_MONTHS:
        return []
    recent = months[-1]
    rows = conn.execute(
        f"""SELECT id, description, lower(description) AS key, date, -amount AS spent,
                   COALESCE(category, 'Uncategorised') AS cat
            FROM transactions
            WHERE amount < 0 AND substr(date, 1, 7) = ? AND {analytics.NOT_AN_INTERNAL_MOVE}
              AND COALESCE(category, '') NOT IN ('Transfer', 'Savings & Investments')""",
        (recent,),
    ).fetchall()

    priors: dict[str, list[float]] = {}
    for r in conn.execute(
        f"""SELECT lower(description) AS key, -amount AS spent FROM transactions
            WHERE amount < 0 AND substr(date, 1, 7) < ? AND {analytics.NOT_AN_INTERNAL_MOVE}""",
        (recent,),
    ):
        priors.setdefault(r["key"], []).append(float(r["spent"]))

    found = []
    for r in rows:
        past = priors.get(r["key"], [])
        if len(past) < 4:
            continue
        usual = median(past)
        spent = float(r["spent"])
        if usual <= 0 or spent < UNUSUAL_MIN_ABS or spent / usual < UNUSUAL_MIN_RATIO:
            continue
        found.append(Insight(
            kind="unusual_charge",
            severity="notable",
            headline=f"${spent:,.2f} at {r['description']} on {r['date']}",
            detail=(f"You have {len(past)} earlier charges from this merchant with a median of "
                    f"${usual:,.2f}. This one is {spent / usual:.1f} times that."),
            basis=f"median of {len(past)} earlier charges = ${usual:,.2f}; this charge = ${spent:,.2f}",
            amount=spent - usual, merchant=r["description"], category=r["cat"], month=recent,
            link=_link(q=r["description"][:24]),
        ))
    return found


def possible_duplicates(conn: sqlite3.Connection, months: Optional[list[str]] = None,
                        recent_months: int = 3) -> list[Insight]:
    """The same merchant, the same amount to the cent, days apart.

    Same-day identical charges are already collapsed by the import fingerprint,
    so anything found here is a genuine near-repeat worth a second look.
    """
    # Bounded to the recent window: a duplicate from eighteen months ago is
    # either not a duplicate or long since dealt with, and it sorts at the top
    # of the page forever because it carries the highest severity.
    since = (months or [""])[-recent_months] if months and len(months) >= recent_months else ""
    rows = conn.execute(
        """SELECT a.id AS a_id, b.id AS b_id, a.description, a.date AS a_date,
                  b.date AS b_date, -a.amount AS spent, a.category
           FROM transactions a JOIN transactions b
             ON a.account_id = b.account_id
            AND lower(a.description) = lower(b.description)
            AND a.amount = b.amount AND a.id < b.id
           WHERE a.amount < -? AND julianday(b.date) - julianday(a.date) BETWEEN 0 AND ?
             AND substr(a.date, 1, 7) >= ?
           ORDER BY a.date DESC""",
        (DUPLICATE_MIN_ABS, DUPLICATE_WINDOW_DAYS, since),
    ).fetchall()
    return [
        Insight(
            kind="possible_duplicate",
            severity="flag",
            headline=f"{r['description']} charged ${float(r['spent']):,.2f} twice",
            detail=(f"Once on {r['a_date']} and again on {r['b_date']}, for exactly the same "
                    "amount. That is sometimes real and sometimes a double charge."),
            basis=f"transactions {r['a_id']} and {r['b_id']}, identical amount, "
                  f"{r['a_date']} and {r['b_date']}",
            amount=float(r["spent"]), merchant=r["description"], category=r["category"] or "",
            month=r["b_date"][:7], link=_link(q=r["description"][:24]),
        )
        for r in rows
    ]


def fixed_cost_pressure(conn: sqlite3.Connection, totals: list[dict]) -> list[Insight]:
    """How much of your income is spoken for before you decide anything."""
    covered = set(analytics.complete_months(conn, analytics.SETTLED_TOLERANCE_DAYS))
    complete = [m for m in totals if m["month"] in covered]
    if len(complete) < MIN_HISTORY_MONTHS:
        return []
    fixed = committed_monthly(conn)
    declared = income.declared_monthly(conn)
    earned = declared or (sum(m["income"] for m in complete[-3:]) / len(complete[-3:]))
    if earned <= 0 or fixed <= 0:
        return []
    share = fixed / earned
    if share < 0.05:
        # Below this it is not a finding, it is a rounding error with a
        # sentence wrapped round it.
        return []
    source = "your declared pay" if declared else "your average income over the last 3 months"
    return [Insight(
        kind="fixed_cost_pressure",
        severity="flag" if share > 0.6 else "notable" if share > 0.45 else "watch",
        headline=f"{share * 100:.0f}% of your income is committed before you choose anything",
        detail=(f"${fixed:,.0f} a month goes out on merchants that bill you a steady amount "
                f"every month, against ${earned:,.0f} of income. That leaves "
                f"${earned - fixed:,.0f} for everything else, including saving."),
        basis=f"committed = ${fixed:,.0f}/month from merchants charging a steady "
              f"amount in every one of the last 6 months; income = ${earned:,.0f} ({source})",
        amount=fixed,
    )]


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------

@dataclass
class Forecast:
    months: list[dict] = field(default_factory=list)
    fixed: float = 0.0
    variable_typical: float = 0.0
    variable_low: float = 0.0
    variable_high: float = 0.0
    monthly_income: float = 0.0
    income_source: str = ""
    basis: str = ""
    warnings: list[str] = field(default_factory=list)
    ok: bool = False


def forecast(conn: sqlite3.Connection, ahead: int = FORECAST_MONTHS) -> Forecast:
    """What the next few months look like if nothing changes.

    Built from three separately-sourced numbers rather than one trend line,
    because they behave differently and a single extrapolation hides that:

      fixed      merchants billing a steady amount every month
      variable   everything else, taken as the median of complete months, with
                 the best and worst month carried through as a range
      income     your declared pay if you have entered a regime, otherwise the
                 average of complete months

    The range matters more than the midpoint. A forecast quoted as one number
    invites the reader to treat it as a promise.
    """
    covered = set(analytics.complete_months(conn, analytics.SETTLED_TOLERANCE_DAYS))
    complete = [m for m in analytics.monthly_totals(conn) if m["month"] in covered]
    result = Forecast()

    gap = analytics.coverage_gap(conn)
    if gap:
        result.warnings.append(gap)
    if len(complete) < MIN_HISTORY_MONTHS:
        result.warnings.append(
            f"Only {len(complete)} fully-imported month(s) of history. A forecast needs at "
            f"least {MIN_HISTORY_MONTHS}, so there is nothing honest to show yet."
        )
        return result

    used = complete[-6:]
    this_ym = date.today().strftime("%Y-%m")
    behind = (int(this_ym[:4]) * 12 + int(this_ym[5:])) - (
        int(used[-1]["month"][:4]) * 12 + int(used[-1]["month"][5:]))
    if behind > 1:
        result.warnings.append(
            f"The newest complete month here is {used[-1]['month']}, {behind} months back. "
            "This projects forward from history that has a gap in front of it — import your "
            "recent statements before leaning on it."
        )
    committed = fixed_costs(conn, window=len(used))
    result.fixed = round(sum(f["monthly"] for f in committed.values()), 2)
    # Subtract what the committed merchants charged in THAT month, not today's
    # average of them. Using one figure for every month, then clamping the
    # result at zero, reported "variable $0" for a household that plainly spends
    # on groceries, and produced a best-to-worst range containing two of the
    # eight months it was computed from.
    variable = []
    for month in used:
        fixed_that_month = sum(
            f["by_month"].get(month["month"], 0.0) for f in committed.values())
        variable.append(month["spend"] - fixed_that_month)
    result.variable_typical = round(median(variable), 2)
    result.variable_low = round(min(variable), 2)
    result.variable_high = round(max(variable), 2)
    if result.variable_low < 0:
        result.warnings.append(
            "At least one month spent less than its own committed costs — a bill "
            "that did not land, or a refund. The low end of the range reflects that "
            "month and is not a month you should plan around."
        )

    declared = income.declared_monthly(conn)
    if declared:
        result.monthly_income = declared
        result.income_source = "your declared pay"
    else:
        result.monthly_income = round(sum(m["income"] for m in used) / len(used), 2)
        result.income_source = f"the average of {len(used)} complete months"

    saved = round(sum(m["saved"] for m in used) / len(used), 2)
    centre = result.monthly_income - result.fixed - result.variable_typical - saved

    ym = date.today().strftime("%Y-%m")
    for _ in range(max(int(ahead or 1), 1)):
        ym = analytics._shift_month(ym, 1)
        result.months.append({
            "month": ym,
            "income": result.monthly_income,
            "fixed": result.fixed,
            "variable": result.variable_typical,
            "saved": saved,
            "surplus": round(centre, 2),
            "best": round(result.monthly_income - result.fixed - result.variable_low - saved, 2),
            "worst": round(result.monthly_income - result.fixed - result.variable_high - saved, 2),
        })

    result.basis = (
        f"income ${result.monthly_income:,.0f} ({result.income_source}) − fixed "
        f"${result.fixed:,.0f} − variable ${result.variable_typical:,.0f} "
        f"(median of {len(used)} months, range ${result.variable_low:,.0f} to "
        f"${result.variable_high:,.0f}) − savings ${saved:,.0f}"
    )
    unknown = sum(m["unknown_income"] for m in used)
    if unknown > 0:
        result.warnings.append(
            f"${unknown:,.0f} arrived across those months with no rule to explain it and is "
            "not counted as income here. If any of it is pay, this forecast is pessimistic."
        )
    uncategorised = sum(m["uncategorised"] for m in used)
    spend = sum(m["spend"] for m in used)
    if spend > 0 and uncategorised / spend > 0.10:
        result.warnings.append(
            f"{100 * uncategorised / spend:.0f}% of the spending behind this is uncategorised, "
            "so the split between fixed and variable is rougher than it looks."
        )
    result.ok = True
    return result


# ---------------------------------------------------------------------------

def all_insights(conn: sqlite3.Connection, limit: Optional[int] = None) -> list[Insight]:
    """Every detector, ranked: most serious first, then by the money involved."""
    # Only months whose statements are all in. Two ways this used to go wrong,
    # and they compound: the calendar month in progress counted as a month the
    # moment one row landed in it, and a month counted as finished even when
    # the statements covering it were not. On eight months of real data the
    # second one produced ten findings saying merchants had "stopped charging"
    # in a month that was half imported.
    # Two windows, because two kinds of finding tolerate a partial month
    # differently. A total barely moves over four missing days; a subscription
    # that bills on the 30th looks cancelled after one.
    settled = analytics.complete_months(conn, analytics.SETTLED_TOLERANCE_DAYS)
    strict = analytics.complete_months(conn, analytics.STRICT_TOLERANCE_DAYS)
    covered = set(settled)
    totals = [m for m in analytics.monthly_totals(conn) if m["month"] in covered]
    months = settled
    if len(months) < MIN_HISTORY_MONTHS:
        return []

    found = (
        possible_duplicates(conn, months)
        + price_changes(conn, strict)
        + recurring_started_or_stopped(conn, strict)
        + category_spikes(conn, months)
        + unusual_charges(conn, months)
        + fixed_cost_pressure(conn, totals)
    )
    found = _drop_restatements(found)
    found.sort(key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), -abs(i.amount)))
    return found[:limit] if limit else found


def _drop_restatements(found: list[Insight]) -> list[Insight]:
    """One event should produce one finding.

    A single $640 charge makes its category spike by $580. Reporting both is
    not two insights, it is the same insight twice, and a list that repeats
    itself teaches the reader to skim it.
    """
    outliers = [i for i in found if i.kind == "unusual_charge"]
    keep = []
    for insight in found:
        if insight.kind == "category_spike":
            explained = sum(
                o.amount for o in outliers
                if o.category == insight.category and o.month == insight.month
            )
            if insight.amount > 0 and explained / insight.amount >= 0.7:
                continue
        keep.append(insight)
    return keep
