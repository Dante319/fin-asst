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

Most of that gap is not a mystery to the person using the app -- it is the
account at another bank they never exported. `destinations.py` is how they say
so. A declaration never proves an outflow the way a matched statement does,
and the verdict text says so every time -- but as of 2026-09-08, an own_account
or debt declaration IS subtracted from the reported gap, by Dante's own choice,
because a total that never moves no matter how much you explain reads as if
nothing you say makes a difference. Only kind=external ever counted as
resolved before that; now own_account and debt do too, just still labelled as
your word rather than a verified pair.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import destinations

MATCH_WINDOW_DAYS = 4
MATCH_TOLERANCE = 0.01


@dataclass
class MatchResult:
    matched_pairs: int
    matched_amount: float


def _candidates(rows, amount: float, when: date, exclude_account: int, window_days: int, used: set) -> list:
    """Unused rows on the other side, of the same amount, within the window."""
    return [
        r for r in rows
        if r["id"] not in used
        and r["account_id"] != exclude_account
        and abs(abs(r["amount"]) - amount) <= MATCH_TOLERANCE
        and abs((date.fromisoformat(r["date"]) - when).days) <= window_days
    ]


def match_internal_transfers(
    conn: sqlite3.Connection, window_days: int = MATCH_WINDOW_DAYS
) -> MatchResult:
    """Pair outgoing transfers with the incoming side in another account.

    Deliberately conservative: near-identical amount (within MATCH_TOLERANCE),
    opposite sign, different accounts, within a few days, and each transaction
    used at most once.

    The ambiguity test runs in BOTH directions, which it did not used to. Only
    checking that an outflow had exactly one candidate inflow meant that two
    $500 outflows on the same day -- one a real transfer, one a rent cheque --
    competed for a single $500 deposit, and whichever had the lower row id won.
    That silently reclassified a real expense as an internal movement, which is
    precisely the failure this module exists to prevent. A pair is now made
    only when each side is the other's only candidate.
    """
    conn.execute("UPDATE transactions SET transfer_peer_id = NULL")

    outs = conn.execute(
        "SELECT id, account_id, date, amount FROM transactions WHERE amount < 0 ORDER BY date, id"
    ).fetchall()
    ins = conn.execute(
        "SELECT id, account_id, date, amount FROM transactions WHERE amount > 0 ORDER BY date, id"
    ).fetchall()

    used: set[int] = set()
    pairs = 0
    total = 0.0

    for out in outs:
        if out["id"] in used:
            continue
        amount = abs(float(out["amount"]))
        out_date = date.fromisoformat(out["date"])

        near_ins = _candidates(ins, amount, out_date, out["account_id"], window_days, used)
        if len(near_ins) != 1:
            continue                      # zero or ambiguous -- do not guess
        peer = near_ins[0]

        # And the same question from the inflow's side: is this outflow the
        # only one that could have funded it?
        peer_date = date.fromisoformat(peer["date"])
        rival_outs = _candidates(
            outs, abs(float(peer["amount"])), peer_date, peer["account_id"], window_days,
            used | {out["id"]},
        )
        if rival_outs:
            continue                      # more than one plausible source

        used.add(peer["id"])
        used.add(out["id"])
        conn.execute("UPDATE transactions SET transfer_peer_id = ? WHERE id = ?", (peer["id"], out["id"]))
        conn.execute("UPDATE transactions SET transfer_peer_id = ? WHERE id = ?", (out["id"], peer["id"]))
        pairs += 1
        total += amount

    conn.commit()
    return MatchResult(pairs, round(total, 2))


@dataclass
class Coverage:
    visible_spend: float
    unmatched_outflow: float
    matched_internal: float
    to_untracked_savings: float = 0.0
    total_outflow: float = 0.0
    destinations: list[dict] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    # own_account/debt declarations have already been subtracted out of
    # `invisible` below -- these two fields are what was taken out, kept
    # separately so the verdict can still say it is your word, not a matched
    # statement, even though the headline number has already moved.
    declared_still_yours: float = 0.0    # kind=own_account: moved, not spent
    declared_debt: float = 0.0           # kind=debt: gone, settles a balance elsewhere
    resolved_as_spending: float = 0.0    # kind=external: now counted as spending
    unexplained: float = 0.0
    declared: list = field(default_factory=list)

    @property
    def invisible(self) -> float:
        """Money that left for somewhere with no statements in this app."""
        return round(self.unmatched_outflow + self.to_untracked_savings, 2)

    @property
    def gap_ratio(self) -> float:
        """The share of ALL money out that landed somewhere unseen.

        The denominator is every dollar that left, not just spending plus
        unmatched transfers. An earlier version omitted savings outflows from
        both terms, so $5,000 into a TFSA the app has no statements for was
        absent from the numerator AND the denominator, and the headline
        understated the gap.
        """
        return (self.invisible / self.total_outflow) if self.total_outflow else 0.0

    @property
    def declared_total(self) -> float:
        """The part of the gap you have explained, however you explained it."""
        return round(self.declared_still_yours + self.declared_debt, 2)

    def verdict(self) -> str:
        if self.invisible < 0.01 and self.declared_total < 0.01:
            return "Every outflow lands in an account this app can see."

        if self.invisible >= 0.01:
            note = (
                f"${self.invisible:,.0f} left your accounts for somewhere this app "
                f"cannot see -- {100 * self.gap_ratio:.0f}% of all money out. Spending "
                f"figures below cover only the rest."
            )
        else:
            note = (
                "Nothing is left unexplained -- every outflow this app has no "
                "statement for, you have told it about."
            )

        if self.to_untracked_savings > 0.01:
            note += (
                f" ${self.to_untracked_savings:,.0f} of that went to savings or "
                "investments, so it is not lost -- just not visible here."
            )

        # What you have said about it. Deliberately worded as your account of
        # it, not the app's finding: nothing here was checked against a
        # statement, and a declaration that reads like evidence is worse than
        # no declaration at all. It has, however, already been subtracted from
        # the number above (2026-09-08) -- that is the one place a declaration
        # is allowed to move a figure rather than just annotate it.
        if self.declared_still_yours > 0.01:
            note += (
                f" ${self.declared_still_yours:,.0f} more has already been taken "
                "out of that number because you told the app it is still yours, "
                "in an account elsewhere -- that is your word, not a matched "
                "statement."
            )
        if self.declared_debt > 0.01:
            note += (
                f" ${self.declared_debt:,.0f} more pays balances at institutions "
                "this app has no statements for -- taken out of the number above "
                "because you said so, but the spending behind it is not."
            )
        if self.unexplained > 0.01:
            note += f" ${self.unexplained:,.0f} is still unexplained."
        return note


def coverage(conn: sqlite3.Connection, since: str | None = None) -> Coverage:
    """What share of outgoing money the app can follow."""
    where = "AND date >= ?" if since else ""
    args = (since,) if since else ()

    def total(extra: str, params=()) -> float:
        return float(conn.execute(
            f"""SELECT COALESCE(SUM(-amount), 0) FROM transactions
                WHERE amount < 0 {where} {extra}""",
            (*args, *params),
        ).fetchone()[0])

    all_out = total("")
    visible = total("AND COALESCE(category, '') NOT IN ('Transfer', 'Savings & Investments')")
    matched = total("AND transfer_peer_id IS NOT NULL")
    savings = total("AND transfer_peer_id IS NULL AND category = 'Savings & Investments'")

    rows = conn.execute(
        f"""SELECT description, raw_description, COUNT(*) n, SUM(-amount) s FROM transactions
            WHERE amount < 0 AND transfer_peer_id IS NULL
            AND category = 'Transfer' {where}
            GROUP BY lower(description) ORDER BY s DESC""",
        args,
    ).fetchall()

    # A destination of kind own_account/debt vouches for where the money went
    # without a matching statement. It is still not verified -- but at Dante's
    # explicit direction (2026-09-08) it is taken out of the reported gap
    # rather than sitting alongside it unchanged, so the headline number moves
    # once he has explained something instead of staying stuck at the raw
    # total forever. kind=external never reaches here: apply_all() has already
    # moved those rows out of category='Transfer' by the time this runs.
    non_spending_dests = [d for d in destinations.all_destinations(conn) if not d.spends]

    # Named dest_rows, not `destinations`: the module of that name is imported
    # here, and shadowing it made every coverage call raise.
    dest_rows = [
        {"destination": r["description"], "count": r["n"], "amount": round(r["s"], 2)}
        for r in rows if r["s"] > 0
        and destinations.match(f"{r['description']} {r['raw_description']}", non_spending_dests) is None
    ]
    unmatched = round(sum(d["amount"] for d in dest_rows), 2)

    accounts = [r["name"] for r in conn.execute("SELECT name FROM accounts ORDER BY name")]
    res = destinations.resolution(conn, since)
    return Coverage(
        declared_still_yours=res.by_kind("own_account"),
        declared_debt=res.by_kind("debt"),
        resolved_as_spending=res.by_kind("external"),
        unexplained=res.unexplained_total,
        declared=res.declared,
        visible_spend=round(visible, 2),
        unmatched_outflow=unmatched,
        matched_internal=round(matched, 2),
        to_untracked_savings=round(savings, 2),
        total_outflow=round(all_out, 2),
        destinations=dest_rows,
        accounts=accounts,
    )


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
