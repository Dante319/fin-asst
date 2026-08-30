"""How much of your money the app can actually account for.

A personal finance tool that silently ignores what it cannot see is worse than
one that reports nothing, because it produces confident numbers built on a
partial picture. This module does the opposite: it works out which outflows
landed in an account the app knows about, and reports the rest as a gap you can
see and act on.

Two jobs:

1. **Transfer matching.** An outflow from one account and an inflow to another
   for the same amount within a few days is almost certainly one internal
   movement, not income plus spending. Matching them prevents double counting.

2. **Coverage.** Everything else that leaves as a transfer goes somewhere the
   app has no statements for. That total is reported next to your spending, so
   you always know how much of the picture is missing.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

MATCH_WINDOW_DAYS = 4
MATCH_TOLERANCE = 0.01


@dataclass
class MatchResult:
    matched_pairs: int
    matched_amount: float


def match_internal_transfers(conn: sqlite3.Connection, window_days: int = MATCH_WINDOW_DAYS) -> MatchResult:
    """Pair outgoing transfers with the incoming side in another account.

    Deliberately conservative: same absolute amount, opposite sign, different
    accounts, within a few days, and each transaction may be used once. A
    wrong pairing would hide real money, so ambiguity resolves to no match.
    """
    conn.execute("UPDATE transactions SET transfer_peer_id = NULL")

    outs = conn.execute(
        """SELECT id, account_id, date, amount FROM transactions
           WHERE amount < 0 ORDER BY date, id"""
    ).fetchall()
    ins = conn.execute(
        """SELECT id, account_id, date, amount FROM transactions
           WHERE amount > 0 ORDER BY date, id"""
    ).fetchall()

    used: set[int] = set()
    pairs = 0
    total = 0.0

    by_amount: dict[float, list[sqlite3.Row]] = {}
    for row in ins:
        by_amount.setdefault(round(row["amount"], 2), []).append(row)

    for out in outs:
        target = round(-out["amount"], 2)
        candidates = [
            r for r in by_amount.get(target, [])
            if r["id"] not in used and r["account_id"] != out["account_id"]
        ]
        if not candidates:
            continue
        out_date = date.fromisoformat(out["date"])
        near = [
            r for r in candidates
            if abs((date.fromisoformat(r["date"]) - out_date).days) <= window_days
        ]
        if len(near) != 1:
            continue  # zero or ambiguous -- leave it unmatched rather than guess
        peer = near[0]
        used.add(peer["id"])
        conn.execute("UPDATE transactions SET transfer_peer_id = ? WHERE id = ?", (peer["id"], out["id"]))
        conn.execute("UPDATE transactions SET transfer_peer_id = ? WHERE id = ?", (out["id"], peer["id"]))
        pairs += 1
        total += target

    conn.commit()
    return MatchResult(pairs, round(total, 2))


@dataclass
class Coverage:
    visible_spend: float
    unmatched_outflow: float
    matched_internal: float
    destinations: list[dict] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)

    @property
    def gap_ratio(self) -> float:
        base = self.visible_spend + self.unmatched_outflow
        return (self.unmatched_outflow / base) if base else 0.0

    def verdict(self) -> str:
        if self.unmatched_outflow < 0.01:
            return "Every outflow lands in an account this app can see."
        return (
            f"${self.unmatched_outflow:,.0f} left your accounts for somewhere this app "
            f"cannot see -- {100 * self.gap_ratio:.0f}% of all money out. Spending "
            f"figures below cover only the rest."
        )


def coverage(conn: sqlite3.Connection, since: str | None = None) -> Coverage:
    """What share of outgoing money the app can follow."""
    where = "AND date >= ?" if since else ""
    args = (since,) if since else ()

    visible = conn.execute(
        f"""SELECT COALESCE(SUM(-amount), 0) FROM transactions
            WHERE amount < 0 {where}
            AND COALESCE(category, '') NOT IN ('Transfer', 'Savings & Investments')""",
        args,
    ).fetchone()[0]

    matched = conn.execute(
        f"""SELECT COALESCE(SUM(-amount), 0) FROM transactions
            WHERE amount < 0 AND transfer_peer_id IS NOT NULL {where}""",
        args,
    ).fetchone()[0]

    rows = conn.execute(
        f"""SELECT description, COUNT(*) n, SUM(-amount) s FROM transactions
            WHERE amount < 0 AND transfer_peer_id IS NULL
            AND category = 'Transfer' {where}
            GROUP BY lower(description) ORDER BY s DESC""",
        args,
    ).fetchall()

    destinations = [
        {"destination": r["description"], "count": r["n"], "amount": round(r["s"], 2)}
        for r in rows if r["s"] > 0
    ]
    unmatched = round(sum(d["amount"] for d in destinations), 2)

    accounts = [r["name"] for r in conn.execute("SELECT name FROM accounts ORDER BY name")]
    return Coverage(round(visible, 2), unmatched, round(matched, 2), destinations, accounts)


# ---------------------------------------------------------------------------
# Income regimes
# ---------------------------------------------------------------------------

@dataclass
class IncomeSource:
    name: str
    total: float
    count: int
    first: str
    last: str
    active: bool


def income_sources(conn: sqlite3.Connection, active_within_days: int = 45) -> list[IncomeSource]:
    rows = conn.execute(
        """SELECT description, SUM(amount) s, COUNT(*) n, MIN(date) f, MAX(date) l
           FROM transactions WHERE category = 'Income' AND amount > 0
           GROUP BY lower(description) ORDER BY s DESC"""
    ).fetchall()
    latest = conn.execute("SELECT MAX(date) FROM transactions").fetchone()[0]
    if not latest:
        return []
    cutoff = (date.fromisoformat(latest) - timedelta(days=active_within_days)).isoformat()
    return [
        IncomeSource(r["description"], round(r["s"], 2), r["n"], r["f"], r["l"], r["l"] >= cutoff)
        for r in rows
    ]


def income_regime_warning(conn: sqlite3.Connection, min_share: float = 0.25) -> str | None:
    """Warn when the surplus baseline is averaging across an income change.

    A surplus computed from months in which a now-ended income stream was still
    paying is a fact about the past, not a basis for projecting the future.
    """
    sources = income_sources(conn)
    if not sources:
        return None
    total = sum(s.total for s in sources) or 1.0
    ended = [s for s in sources if not s.active and s.total / total >= min_share]
    if not ended:
        return None
    biggest = max(ended, key=lambda s: s.total)
    started = [s for s in sources if s.active and s.first > biggest.last]
    note = (
        f"'{biggest.name}' provided {100 * biggest.total / total:.0f}% of recorded income "
        f"but stopped after {biggest.last}."
    )
    if started:
        note += f" '{started[0].name}' began {started[0].first}."
    return note + " A surplus averaged over months spanning that change describes the old regime, not the new one."
