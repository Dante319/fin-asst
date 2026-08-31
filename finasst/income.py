"""Income regimes: what you are actually paid now, as distinct from what you
were paid before.

A surplus averaged over the last few months is a good baseline right up until
your income changes. Then it is a fact about a job you no longer have. The app
already *warned* about that (coverage.income_regime_warning), but a warning
does not fix the number underneath it, and the number is what the goal engine
spends.

A regime is a declaration: this source, starting this date, pays this much this
often. Two things follow from it.

  1. The surplus baseline stops averaging across the boundary. Only whole
     months that fall entirely inside the current regime are eligible.
  2. When there are not yet enough such months to average -- which is the
     normal situation immediately after a job change, and the whole reason
     this exists -- the baseline is built from the declared pay minus your
     observed spending, and says so. Spending habits carry across a job change;
     income does not.

Declared pay is never silently trusted over the statements. Once whole months
inside the regime exist, `drift()` compares what you said you would be paid
against what actually landed, and the app reports the gap rather than picking
a winner.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Optional

# Pay periods per year. Biweekly is 26, not 24: two extra cheques a year is
# roughly 8% of income, and calling it "twice a month" quietly loses them.
PERIODS_PER_YEAR = {
    "weekly": 52,
    "biweekly": 26,
    "semimonthly": 24,
    "monthly": 12,
    "quarterly": 4,
    "annual": 1,
}

FREQUENCY_LABELS = {
    "weekly": "every week",
    "biweekly": "every two weeks",
    "semimonthly": "twice a month",
    "monthly": "monthly",
    "quarterly": "quarterly",
    "annual": "once a year",
}

# How many whole months inside a regime it takes before the observed surplus is
# preferred to the declared one. One month is a sample, not an average, and a
# single unusual month would set the baseline for every projection.
MIN_REGIME_MONTHS = 2


def normalise_frequency(value: str) -> str:
    key = (value or "").strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    aliases = {
        "everytwoweeks": "biweekly", "fortnightly": "biweekly", "biweekly": "biweekly",
        "everyweek": "weekly", "weekly": "weekly",
        "twicemonthly": "semimonthly", "semimonthly": "semimonthly", "bimonthly": "semimonthly",
        "monthly": "monthly", "permonth": "monthly",
        "quarterly": "quarterly",
        "annual": "annual", "annually": "annual", "yearly": "annual",
    }
    if key not in aliases:
        raise ValueError(
            f"Unknown pay frequency {value!r}. Use one of: "
            + ", ".join(sorted(PERIODS_PER_YEAR))
        )
    return aliases[key]


def monthly_equivalent(amount: float, frequency: str) -> float:
    """What a per-period amount works out to per month, on average.

    Averaged across the year on purpose: a biweekly earner gets three cheques
    in some months and two in others, and a baseline that swings with the
    calendar would make every projection depend on which month you ran it.
    """
    return round(float(amount) * PERIODS_PER_YEAR[normalise_frequency(frequency)] / 12.0, 2)


@dataclass
class Regime:
    name: str
    started_on: date
    amount: float                 # net pay per period, as it lands in the account
    frequency: str = "biweekly"
    ended_on: Optional[date] = None
    id: Optional[int] = None
    notes: str = ""

    @property
    def monthly(self) -> float:
        return monthly_equivalent(self.amount, self.frequency)

    @property
    def annual(self) -> float:
        return round(float(self.amount) * PERIODS_PER_YEAR[normalise_frequency(self.frequency)], 2)

    @property
    def label(self) -> str:
        return FREQUENCY_LABELS[normalise_frequency(self.frequency)]

    def covers(self, day: date) -> bool:
        return self.started_on <= day and (self.ended_on is None or day <= self.ended_on)

    @property
    def first_whole_month(self) -> str:
        """The first month this regime paid for from the 1st onwards.

        A regime starting mid-month leaves a partial month behind it: some of
        that month's income came from the old job. Including it would drag the
        new baseline back towards the old one, which is exactly the error this
        module exists to prevent.
        """
        d = self.started_on
        if d.day == 1:
            return d.strftime("%Y-%m")
        year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        return f"{year:04d}-{month:02d}"

    @property
    def last_whole_month(self) -> Optional[str]:
        """The last month fully inside this regime, or None if it is current."""
        if self.ended_on is None:
            return None
        d = self.ended_on
        # Only complete if it ran to the final day of the month.
        nxt = date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
        if (nxt - d).days == 1:
            return d.strftime("%Y-%m")
        year, month = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
        return f"{year:04d}-{month:02d}"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Regime":
        return cls(
            id=int(row["id"]),
            name=row["name"],
            started_on=date.fromisoformat(row["started_on"]),
            ended_on=date.fromisoformat(row["ended_on"]) if row["ended_on"] else None,
            amount=float(row["amount"]),
            frequency=row["frequency"],
            notes=row["notes"] or "",
        )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def add_regime(conn: sqlite3.Connection, regime: Regime, close_previous: bool = True) -> int:
    """Record a regime. By default the one it replaces is closed the day before.

    Two open-ended regimes at once would mean the app believes you hold two
    jobs, and would add both into one baseline. If that is actually true, pass
    close_previous=False.
    """
    freq = normalise_frequency(regime.frequency)
    if close_previous:
        conn.execute(
            "UPDATE income_regimes SET ended_on = date(?, '-1 day') "
            "WHERE ended_on IS NULL AND started_on < ?",
            (regime.started_on.isoformat(), regime.started_on.isoformat()),
        )
    cur = conn.execute(
        """INSERT INTO income_regimes(name, started_on, ended_on, amount, frequency, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (regime.name.strip(), regime.started_on.isoformat(),
         regime.ended_on.isoformat() if regime.ended_on else None,
         float(regime.amount), freq, regime.notes),
    )
    conn.commit()
    return int(cur.lastrowid)


def regimes(conn: sqlite3.Connection) -> list[Regime]:
    rows = conn.execute(
        "SELECT * FROM income_regimes ORDER BY started_on DESC, id DESC"
    ).fetchall()
    return [Regime.from_row(r) for r in rows]


def delete_regime(conn: sqlite3.Connection, regime_id: int) -> bool:
    cur = conn.execute("DELETE FROM income_regimes WHERE id = ?", (int(regime_id),))
    conn.commit()
    return cur.rowcount > 0


def current_regime(conn: sqlite3.Connection, as_of: Optional[date] = None) -> Optional[Regime]:
    """The regime in force on a given day (default: today).

    If several overlap -- you told the app about a second job -- the most
    recently started one is returned, and `all_current` gives the full set.
    """
    live = all_current(conn, as_of)
    return live[0] if live else None


def all_current(conn: sqlite3.Connection, as_of: Optional[date] = None) -> list[Regime]:
    day = as_of or date.today()
    return [r for r in regimes(conn) if r.covers(day)]


def declared_monthly(conn: sqlite3.Connection, as_of: Optional[date] = None) -> float:
    """Every regime running today, added up."""
    return round(sum(r.monthly for r in all_current(conn, as_of)), 2)


# ---------------------------------------------------------------------------
# Declared vs observed
# ---------------------------------------------------------------------------

def whole_months_in_regime(months: list[str], regime: Regime) -> list[str]:
    """Filter a list of YYYY-MM to those falling entirely inside the regime."""
    first, last = regime.first_whole_month, regime.last_whole_month
    return [m for m in months if m >= first and (last is None or m <= last)]


def drift(conn: sqlite3.Connection, regime: Regime, monthly_totals: list[dict],
          tolerance: float = 0.10) -> Optional[str]:
    """Compare declared pay against what the statements actually show.

    Returns None when they agree within tolerance, or when there is not yet a
    whole month of statements inside the regime to compare against. The app
    never picks a winner here: a gap can mean the declaration is wrong, or that
    a pay deposit is still uncategorised, and only you know which.
    """
    inside = [m for m in monthly_totals
              if m["month"] in whole_months_in_regime([t["month"] for t in monthly_totals], regime)]
    today = date.today().strftime("%Y-%m")
    inside = [m for m in inside if m["month"] < today]
    if not inside:
        return None
    observed = sum(m["income"] for m in inside) / len(inside)
    expected = regime.monthly
    if expected <= 0:
        return None
    gap = observed - expected
    if abs(gap) / expected <= tolerance:
        return None
    span = f"{inside[0]['month']}" if len(inside) == 1 else f"{inside[0]['month']}-{inside[-1]['month']}"
    direction = "less" if gap < 0 else "more"
    note = (
        f"You declared {regime.name} at {expected:,.0f}/month "
        f"({regime.amount:,.0f} {regime.label}), but {span} shows "
        f"{observed:,.0f}/month of categorised income -- {abs(gap):,.0f} {direction}."
    )
    unknown = sum(m.get("unknown_income", 0.0) for m in inside) / len(inside)
    if gap < 0 and unknown > 0:
        note += (f" {unknown:,.0f}/month arrived in those months with no rule to explain it;"
                 " if that is your pay, categorise it as Income and the gap closes.")
    else:
        note += " Check the declaration or the categorisation before trusting either figure."
    return note
