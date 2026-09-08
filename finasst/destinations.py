"""Where money goes when it leaves for an account this app cannot see.

`coverage.py` reports the gap: outflows filed as transfers with no matching
inflow anywhere in the database. It is deliberately blunt about it, because a
spending figure computed over a partial picture is worse than no figure.

But most of that gap is not a mystery to YOU. It is the account at another
bank you never exported, the money you send home every month, the card you pay
off somewhere else. This module is how you say so.

A destination is a declaration, and declarations are not evidence -- own_account
and debt are never counted as a verified match, and the app says so every time
it reports them. As of 2026-09-08 they DO shrink the reported gap itself
(coverage.Coverage.invisible), by Dante's own choice: a total that stayed
exactly the same no matter how much he explained made the app feel like it was
ignoring him. What still never happens is a declaration silently becoming
"seen" -- it stays labelled as your word, not a matched statement, in every
place it is shown.

Three kinds, because they mean three different things to the numbers:

  own_account  Still your money, in an account with no statements here. Not
               spending. Your net worth is unchanged -- the app just cannot
               see that side of it.
  external     Genuinely gone: money sent to family, a gift, rent paid to a
               person. This IS spending, and giving the destination a category
               files it as such, so it stops hiding in the transfer bucket.
  debt         Pays down a balance at another institution. The money is gone,
               but -- and this is the one kind that resolving does NOT fix --
               the spending it settles happened on a card this app has never
               seen. Declaring it explains the outflow and hides nothing else.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from .categorize import normalise, rule_matches

KINDS = {
    "own_account": {
        "label": "My own account elsewhere",
        "short": "still mine",
        "help": "Another account of yours with no statements in this app. "
                "The money has not been spent, it has only moved out of view.",
        "spends": False,
    },
    "external": {
        "label": "Money that genuinely left",
        "short": "spent",
        "help": "Sent to someone else -- family, a gift, rent to a person. "
                "Give it a category and it will count as spending instead of "
                "sitting in the transfer bucket.",
        "spends": True,
    },
    "debt": {
        "label": "Pays a balance elsewhere",
        "short": "settles debt",
        "help": "A credit card or loan at another institution. The outflow is "
                "explained, but the spending it settles is still invisible, "
                "because those statements are not in this app.",
        "spends": False,
    },
}

# Written into transactions.category_source so a category set by a destination
# is telling apart from one a rule or you set directly, and can be undone.
SOURCE = "destination"


@dataclass
class Destination:
    pattern: str
    label: str
    kind: str
    match_type: str = "contains"
    account_id: Optional[int] = None
    category: Optional[str] = None
    notes: str = ""
    id: Optional[int] = None

    @property
    def spends(self) -> bool:
        return KINDS[self.kind]["spends"]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Destination":
        return cls(
            id=int(row["id"]),
            pattern=row["pattern"],
            match_type=row["match_type"],
            label=row["label"],
            kind=row["kind"],
            account_id=row["account_id"],
            category=row["category"],
            notes=row["notes"] or "",
        )


def validate(kind: str, category: Optional[str]) -> None:
    if kind not in KINDS:
        raise ValueError(f"Unknown destination kind {kind!r}. Use one of: {', '.join(KINDS)}")
    if KINDS[kind]["spends"] and not (category or "").strip():
        raise ValueError(
            "Money that genuinely left is spending, so it needs a category -- "
            "otherwise it stays in the transfer bucket and the spending figures "
            "still understate what you spent."
        )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def add(conn: sqlite3.Connection, dest: Destination) -> int:
    validate(dest.kind, dest.category)
    cur = conn.execute(
        """INSERT INTO destinations(pattern, match_type, label, kind, account_id, category, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(pattern, match_type) DO UPDATE SET
             label = excluded.label, kind = excluded.kind,
             account_id = excluded.account_id, category = excluded.category,
             notes = excluded.notes""",
        (dest.pattern.strip(), dest.match_type, dest.label.strip(), dest.kind,
         dest.account_id, (dest.category or None), dest.notes),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM destinations WHERE pattern = ? AND match_type = ?",
        (dest.pattern.strip(), dest.match_type),
    ).fetchone()
    return int(row["id"] if row else cur.lastrowid)


def all_destinations(conn: sqlite3.Connection) -> list[Destination]:
    return [Destination.from_row(r) for r in
            conn.execute("SELECT * FROM destinations ORDER BY label, id")]


def delete(conn: sqlite3.Connection, dest_id: int) -> bool:
    row = conn.execute("SELECT * FROM destinations WHERE id = ?", (int(dest_id),)).fetchone()
    if not row:
        return False
    _unapply(conn, Destination.from_row(row))
    conn.execute("DELETE FROM destinations WHERE id = ?", (int(dest_id),))
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Matching and application
# ---------------------------------------------------------------------------

def _row_matches(dest: Destination, text: str) -> bool:
    # rule_matches takes a sqlite3.Row in normal use; a plain dict has the same
    # subscript interface and keeps this module from depending on the table.
    return rule_matches(
        {"pattern": dest.pattern, "match_type": dest.match_type}, text, normalise(text)
    )


def match(text: str, dests: list[Destination]) -> Optional[Destination]:
    for d in dests:
        if _row_matches(d, text):
            return d
    return None


def _unresolved_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Outflows filed as transfers that the matcher could not pair.

    This is exactly the set coverage.py counts as invisible, plus the savings
    outflows that also went somewhere unseen.
    """
    return conn.execute(
        """SELECT id, description, raw_description, amount, category, category_source
           FROM transactions
           WHERE amount < 0 AND transfer_peer_id IS NULL
             AND (COALESCE(category, '') IN ('Transfer', 'Savings & Investments')
                  OR category_source = ?)""",
        (SOURCE,),
    ).fetchall()


def apply_all(conn: sqlite3.Connection) -> int:
    """File every 'external' destination's transactions as real spending.

    Runs where match_internal_transfers runs: at startup and after an import.
    A category you set by hand is never overwritten -- your correction outranks
    a declaration, exactly as it outranks a rule.
    """
    dests = [d for d in all_destinations(conn) if d.spends and d.category]
    if not dests:
        return 0
    changed = 0
    for row in _unresolved_rows(conn):
        if row["category_source"] == "manual":
            continue
        d = match(f"{row['description']} {row['raw_description']}", dests)
        if d and row["category"] != d.category:
            conn.execute(
                "UPDATE transactions SET category = ?, category_source = ? WHERE id = ?",
                (d.category, SOURCE, row["id"]),
            )
            changed += 1
    conn.commit()
    return changed


def _unapply(conn: sqlite3.Connection, dest: Destination) -> int:
    """Put transactions this destination recategorised back in the transfer bucket.

    Without this, deleting a destination would leave its verdict behind
    permanently, and the row would claim a category nothing in the app could
    still justify.
    """
    if not dest.spends:
        return 0
    restored = 0
    for row in conn.execute(
        "SELECT id, description, raw_description FROM transactions WHERE category_source = ?",
        (SOURCE,),
    ).fetchall():
        if _row_matches(dest, f"{row['description']} {row['raw_description']}"):
            conn.execute(
                "UPDATE transactions SET category = 'Transfer', category_source = ? WHERE id = ?",
                ("rule", row["id"]),
            )
            restored += 1
    conn.commit()
    return restored


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

@dataclass
class Declared:
    label: str
    kind: str
    amount: float
    count: int
    destination_id: Optional[int]
    category: Optional[str] = None
    tracked_account: Optional[str] = None   # set when it names an account we DO have

    @property
    def short(self) -> str:
        return KINDS[self.kind]["short"]


@dataclass
class Resolution:
    declared: list[Declared] = field(default_factory=list)
    unexplained: list[dict] = field(default_factory=list)

    @property
    def declared_total(self) -> float:
        return round(sum(d.amount for d in self.declared), 2)

    @property
    def unexplained_total(self) -> float:
        return round(sum(d["amount"] for d in self.unexplained), 2)

    def by_kind(self, kind: str) -> float:
        return round(sum(d.amount for d in self.declared if d.kind == kind), 2)

    @property
    def still_yours(self) -> float:
        return self.by_kind("own_account")

    @property
    def warnings(self) -> list[str]:
        """Declarations that point at something the app should not have missed."""
        out = []
        for d in self.declared:
            if d.tracked_account:
                out.append(
                    f"You have told the app that '{d.label}' lands in "
                    f"{d.tracked_account}, and this app HAS statements for that "
                    "account -- so either the months containing the other side are "
                    "not imported, or the amounts and dates did not line up closely "
                    "enough to pair. Importing them would turn a declaration into a "
                    "verified match."
                )
        return out


def resolution(conn: sqlite3.Connection, since: Optional[str] = None) -> Resolution:
    """Split the coverage gap into what you have explained and what you have not.

    `since` has to be honoured here as well as in coverage(): a windowed gap
    total beside an all-time declaration would show more explained than there
    was gap, which is the sort of arithmetic that makes a user stop trusting
    every other figure on the page.
    """
    dests = all_destinations(conn)
    accounts = {int(r["id"]): r["name"] for r in conn.execute("SELECT id, name FROM accounts")}

    rows = conn.execute(
        f"""SELECT description, raw_description, COUNT(*) n, SUM(-amount) s
           FROM transactions
           WHERE amount < 0 AND transfer_peer_id IS NULL
             {"AND date >= ?" if since else ""}
             AND (COALESCE(category, '') IN ('Transfer', 'Savings & Investments')
                  OR category_source = ?)
           GROUP BY lower(description) ORDER BY s DESC""",
        ((since, SOURCE) if since else (SOURCE,)),
    ).fetchall()

    buckets: dict[int, Declared] = {}
    unexplained: list[dict] = []
    for r in rows:
        amount = round(float(r["s"]), 2)
        if amount <= 0:
            continue
        d = match(f"{r['description']} {r['raw_description']}", dests)
        if d is None:
            unexplained.append({"destination": r["description"], "count": r["n"], "amount": amount})
            continue
        key = int(d.id)
        if key not in buckets:
            buckets[key] = Declared(
                label=d.label, kind=d.kind, amount=0.0, count=0, destination_id=d.id,
                category=d.category,
                tracked_account=accounts.get(d.account_id) if d.account_id else None,
            )
        buckets[key].amount = round(buckets[key].amount + amount, 2)
        buckets[key].count += r["n"]

    return Resolution(
        declared=sorted(buckets.values(), key=lambda d: -d.amount),
        unexplained=unexplained,
    )
