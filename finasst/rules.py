"""Managing the rules that do the categorising.

Until now the rule table was write-only from the user's point of view: a
correction quietly created a rule, and there was no way to see it, change it,
or find out why a transaction had been filed where it was. That is a bad deal
in an app whose whole argument is that its numbers are auditable -- "you can
read the rule that did this" is not much of a promise if you cannot read it.

Three things live here:

  inventory()  every rule, with how many transactions it is actually
               responsible for right now. A rule matching nothing is either
               a merchant you no longer use or a rule that never worked.
  explain()    given a description, which rule wins and which others would
               have matched. This is the answer to "why is this Groceries?"
  add/remove   user rules only. Seed rules are code, not data: editing one in
               the database would be silently undone the next time the app
               starts, because seeding retires rules it no longer ships.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from .categorize import load_rules, match_category, normalise, rule_matches
from .config import CATEGORIES

USER_PRIORITY = 10
VALID_MATCH_TYPES = ("words", "contains", "regex", "exact")


@dataclass
class RuleInfo:
    id: int
    pattern: str
    match_type: str
    category: str
    priority: int
    is_user: bool
    matches: int = 0          # transactions this rule currently decides
    shadowed_by: Optional[str] = None   # a higher-ranked rule that wins first
    examples: list[str] = field(default_factory=list)


def _ranked(rules) -> list:
    return list(rules)


def inventory(conn: sqlite3.Connection, example_limit: int = 3) -> list[RuleInfo]:
    """Every rule, and what it is doing for its keep.

    `matches` counts transactions this rule WINS, not transactions it merely
    matches -- a rule that always loses to a higher-priority one is doing
    nothing, and saying it matched 400 transactions would be a lie.
    """
    rules = _ranked(load_rules(conn))
    info = {
        int(r["id"]): RuleInfo(
            id=int(r["id"]), pattern=r["pattern"], match_type=r["match_type"],
            category=r["category"], priority=int(r["priority"]), is_user=bool(r["is_user"]),
        )
        for r in rules
    }

    # Rows you corrected by hand are decided by you, not by a rule:
    # categorize_all skips them, so counting them here would credit a rule with
    # work it does not do -- exactly the lie this count exists to avoid.
    rows = conn.execute(
        "SELECT description, raw_description FROM transactions "
        "WHERE category_source IS NULL OR category_source != 'manual'"
    ).fetchall()
    for row in rows:
        text = f"{row['description']} {row['raw_description']}"
        hit = match_category(text, rules)
        if hit is None:
            continue
        entry = info[hit[1]]
        entry.matches += 1
        if len(entry.examples) < example_limit and row["description"] not in entry.examples:
            entry.examples.append(row["description"])

    # A rule that never wins may be permanently shadowed. Say which rule by.
    for rule in rules:
        entry = info[int(rule["id"])]
        if entry.matches:
            continue
        probe = rule["pattern"] if rule["match_type"] != "regex" else normalise(rule["pattern"])
        if not probe:
            continue
        for other in rules:
            if int(other["id"]) == entry.id:
                break            # rules are ranked, so anything after this loses
            if rule_matches(other, probe, normalise(probe)):
                entry.shadowed_by = other["pattern"]
                break
    return list(info.values())


@dataclass
class Explanation:
    text: str
    category: Optional[str] = None
    winner: Optional[RuleInfo] = None
    also_matched: list[RuleInfo] = field(default_factory=list)


def explain(conn: sqlite3.Connection, text: str) -> Explanation:
    """Which rule decides this description, and what else came close."""
    rules = _ranked(load_rules(conn))
    normalised = normalise(text)
    hits = [r for r in rules if rule_matches(r, text, normalised)]
    if not hits:
        return Explanation(text=text)

    def as_info(r) -> RuleInfo:
        return RuleInfo(int(r["id"]), r["pattern"], r["match_type"], r["category"],
                        int(r["priority"]), bool(r["is_user"]))

    return Explanation(
        text=text,
        category=hits[0]["category"],
        winner=as_info(hits[0]),
        also_matched=[as_info(r) for r in hits[1:6]],
    )


# Nested quantifiers -- (a+)+ , (x*)* , (\d+)+ -- are the classic shape that
# makes Python's backtracking engine take exponential time. The rule is stored
# and then run against every description on every page load, so one of these
# does not fail once: it wedges /rules, /insights and every future import,
# including the page you would delete it from.
_NESTED_QUANTIFIER = re.compile(r"\([^()]*[+*][^()]*\)\s*[+*{]")
_SLOW_BUDGET_SECONDS = 0.25


def _refuse_if_slow(compiled, pattern: str) -> None:
    import time

    if _NESTED_QUANTIFIER.search(pattern):
        raise RuleError(
            "That pattern nests one repeat inside another, which can take "
            "exponential time on an unlucky merchant name and would hang every "
            "page in the app. Rewrite it without the inner + or *."
        )
    probe = ("a" * 24) + "!" + ("0-9 " * 6)
    started = time.perf_counter()
    compiled.search(probe)
    if time.perf_counter() - started > _SLOW_BUDGET_SECONDS:
        raise RuleError(
            "That pattern is too slow to run against every transaction. "
            "Try a plainer one -- 'words' handles most merchants."
        )


class RuleError(ValueError):
    """A rule that would not work, explained in words rather than a traceback."""


def add_rule(
    conn: sqlite3.Connection,
    pattern: str,
    category: str,
    match_type: str = "words",
    priority: int = USER_PRIORITY,
) -> int:
    pattern = (pattern or "").strip()
    if len(pattern) < 3:
        raise RuleError("A pattern needs at least three characters, or it will match half your statement.")
    if category not in CATEGORIES:
        raise RuleError(f"{category!r} is not one of the categories.")
    if match_type not in VALID_MATCH_TYPES:
        raise RuleError(f"{match_type!r} is not a kind of match this app knows.")
    if match_type == "regex":
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise RuleError(f"That is not a valid regular expression: {exc}") from exc
        _refuse_if_slow(compiled, pattern)

    existing = conn.execute(
        "SELECT id, is_user, category FROM rules WHERE pattern = ? AND match_type = ?",
        (pattern, match_type),
    ).fetchone()
    if existing is not None and not existing["is_user"]:
        # Overwriting it would flip a built-in rule to is_user=1, at which point
        # remove_rule would happily delete it and the next seed would put it
        # back -- the exact "that would come back" outcome the error message
        # for deleting a built-in promises cannot happen.
        raise RuleError(
            f"There is already a built-in rule for {pattern!r} (currently "
            f"{existing['category']}). Built-in rules live in the code, so this "
            "one cannot be redefined here. Use a slightly more specific pattern "
            "of your own -- yours outranks it."
        )
    cur = conn.execute(
        "INSERT INTO rules(pattern, match_type, category, priority, is_user) "
        "VALUES (?, ?, ?, ?, 1) "
        "ON CONFLICT(pattern, match_type) DO UPDATE SET "
        "category = excluded.category, priority = excluded.priority, is_user = 1",
        (pattern, match_type, category, priority),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM rules WHERE pattern = ? AND match_type = ?", (pattern, match_type)
    ).fetchone()
    return int(row["id"]) if row else int(cur.lastrowid)


def remove_rule(conn: sqlite3.Connection, rule_id: int) -> RuleInfo:
    """Delete a rule you taught, and un-file everything that depended on it.

    Transactions it had categorised revert to uncategorised unless another rule
    covers them, which `recategorise` sorts out. Manual corrections are left
    alone -- deleting the rule you learned from a correction should not undo
    the correction itself.
    """
    row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise RuleError(f"No rule with id {rule_id}.")
    if not row["is_user"]:
        raise RuleError(
            "That is a built-in rule. Built-in rules live in the code, so deleting "
            "one here would come back the next time the app starts. Add your own "
            "rule for the same merchant instead -- yours wins."
        )
    info = RuleInfo(int(row["id"]), row["pattern"], row["match_type"], row["category"],
                    int(row["priority"]), True)
    conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    conn.commit()
    return info


def recategorise(conn: sqlite3.Connection) -> dict:
    """Re-apply every rule to every transaction you have not corrected by hand."""
    from .categorize import categorize_all

    return categorize_all(conn, recategorize=True)
