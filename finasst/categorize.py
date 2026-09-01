"""Deterministic categorisation.

No model, no API call, no guessing you cannot audit: a transaction gets a
category because a rule you can read matched it. When you recategorise
something by hand, that correction is stored as a user rule with top priority,
so the same merchant is right forever after.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Optional

from .config import CATEGORIES

# Seed rules. Tuned for Toronto spending and a Canada-India money flow.
# priority: lower wins. User-taught rules are inserted at priority 10.
SEED_RULES: list[tuple[str, str, str, int]] = [
    # (pattern, match_type, category, priority)
    ("loblaw", "contains", "Groceries", 50),
    ("no frills", "contains", "Groceries", 50),
    ("nofrills", "contains", "Groceries", 50),
    (r"\bmetro\b", "regex", "Groceries", 60),        # not METROLINX
    ("sobeys", "contains", "Groceries", 50),
    ("farm boy", "contains", "Groceries", 50),
    ("freshco", "contains", "Groceries", 50),
    ("food basics", "contains", "Groceries", 50),
    ("costco", "contains", "Groceries", 55),
    ("t&t supermarket", "contains", "Groceries", 50),
    ("iqbal", "contains", "Groceries", 50),
    ("longo", "contains", "Groceries", 50),

    ("starbucks", "contains", "Eats & Drinks", 50),
    ("tim hortons", "contains", "Eats & Drinks", 50),
    ("uber eats", "contains", "Eats & Drinks", 40),
    ("ubereats", "contains", "Eats & Drinks", 40),
    ("doordash", "contains", "Eats & Drinks", 40),
    ("skipthedishes", "contains", "Eats & Drinks", 40),
    (r"\brestaurant\b", "regex", "Eats & Drinks", 70),
    ("pizza", "contains", "Eats & Drinks", 60),
    ("cafe", "contains", "Eats & Drinks", 65),
    ("coffee", "contains", "Eats & Drinks", 65),
    (r"\bbar\b", "regex", "Eats & Drinks", 70),      # not BARBER
    ("lcbo", "contains", "Eats & Drinks", 50),
    ("beer store", "contains", "Eats & Drinks", 50),

    (r"\buber\b", "regex", "Transport", 60),   # after uber eats, so it loses to it
    ("lyft", "contains", "Transport", 50),
    ("presto", "contains", "Transport", 50),
    ("metrolinx", "contains", "Transport", 50),
    ("go transit", "contains", "Transport", 50),
    # Car hire before the rent rule: "AVIS RENT A CAR" is not housing.
    ("rent a car", "contains", "Travel", 25),
    ("car rental", "contains", "Travel", 25),
    ("avis ", "contains", "Travel", 25),
    ("hertz", "contains", "Travel", 25),
    ("ttc", "contains", "Transport", 50),
    ("via rail", "contains", "Transport", 50),
    (r"\bpetro.?canada\b", "regex", "Transport", 50),
    (r"\besso\b", "regex", "Transport", 50),
    (r"\bshell\b", "regex", "Transport", 55),        # not SHELLEY\'S
    ("green p", "contains", "Transport", 50),
    (r"\bparking\b", "regex", "Transport", 60),

    (r"\brent\b", "regex", "Housing", 30),
    ("property tax", "contains", "Housing", 40),
    ("condo fee", "contains", "Housing", 40),
    # Rent: paid by a monthly cheque of a constant amount. Inferred from the
    # pattern rather than stated -- move it if these cheques are something else.
    (r"^cheque\s*#", "regex", "Housing", 40),
    ("provident energy", "contains", "Utilities", 35),

    ("toronto hydro", "contains", "Utilities", 40),
    ("enbridge", "contains", "Utilities", 40),
    ("alectra", "contains", "Utilities", 40),

    ("rogers", "contains", "Phone & Internet", 40),
    ("bell canada", "contains", "Phone & Internet", 40),
    ("telus", "contains", "Phone & Internet", 40),
    ("freedom mobile", "contains", "Phone & Internet", 40),
    ("koodo", "contains", "Phone & Internet", 40),
    (r"\bfizz\b", "regex", "Phone & Internet", 45),
    ("beanfield", "contains", "Phone & Internet", 40),

    ("shoppers drug", "contains", "Health", 45),
    ("rexall", "contains", "Health", 45),
    (r"\bdental\b", "regex", "Health", 50),
    (r"\bphysio\b", "regex", "Health", 50),
    ("goodlife", "contains", "Health", 45),
    (r"\bfitness\b", "regex", "Health", 60),

    ("amazon", "contains", "Shopping", 55),
    ("amzn", "contains", "Shopping", 55),
    ("canadian tire", "contains", "Shopping", 50),
    ("ikea", "contains", "Shopping", 50),
    ("best buy", "contains", "Shopping", 50),
    ("indigo", "contains", "Shopping", 50),
    ("uniqlo", "contains", "Shopping", 50),
    ("winners", "contains", "Shopping", 50),
    ("dollarama", "contains", "Shopping", 50),

    ("netflix", "contains", "Subscriptions", 40),
    ("spotify", "contains", "Subscriptions", 40),
    ("apple.com/bill", "contains", "Subscriptions", 40),
    ("icloud", "contains", "Subscriptions", 40),
    ("google storage", "contains", "Subscriptions", 40),
    ("openai", "contains", "Subscriptions", 40),
    ("anthropic", "contains", "Subscriptions", 40),
    ("claude", "contains", "Subscriptions", 40),
    ("github", "contains", "Subscriptions", 45),
    (r"\bcrave\b", "regex", "Subscriptions", 45),
    ("disney", "contains", "Subscriptions", 45),
    ("substack", "contains", "Subscriptions", 45),

    ("air canada", "contains", "Travel", 40),
    ("etihad", "contains", "Travel", 40),
    ("emirates", "contains", "Travel", 40),
    ("westjet", "contains", "Travel", 40),
    ("porter airlines", "contains", "Travel", 40),
    ("airbnb", "contains", "Travel", 40),
    ("booking.com", "contains", "Travel", 40),
    ("expedia", "contains", "Travel", 40),
    (r"\bhotel\b", "regex", "Travel", 60),
    ("flighthub", "contains", "Travel", 40),

    ("cineplex", "contains", "Entertainment", 45),
    (r"\btiff\b", "regex", "Entertainment", 45),     # not TIFFANY
    ("ticketmaster", "contains", "Entertainment", 45),
    ("dice.fm", "contains", "Entertainment", 40),
    ("eventbrite", "contains", "Entertainment", 45),
    (r"\bsteam\b", "regex", "Entertainment", 50),
    ("playstation", "contains", "Entertainment", 45),
    ("nintendo", "contains", "Entertainment", 45),

    ("international transfer", "contains", "Remittance", 25),
    (r"\bwise\b", "regex", "Remittance", 35),        # not LIKEWISE
    ("remitly", "contains", "Remittance", 35),
    ("western union", "contains", "Remittance", 35),
    ("xe money", "contains", "Remittance", 35),
    ("moneygram", "contains", "Remittance", 35),
    ("instarem", "contains", "Remittance", 35),

    # Interest RECEIVED is income; interest CHARGED is a cost. Order matters.
    ("interest received", "contains", "Income", 25),
    ("interest earned", "contains", "Income", 25),
    (r"\binterest\b", "regex", "Fees & Interest", 60),
    ("annual fee", "contains", "Fees & Interest", 40),
    ("foreign transaction fee", "contains", "Fees & Interest", 40),
    ("nsf fee", "contains", "Fees & Interest", 40),

    # --- debt servicing: not discretionary spending, and not a transfer either ---
    ("line of cr", "contains", "Debt Payment", 25),
    ("auto-withdrawal by nbc", "contains", "Debt Payment", 25),
    ("loan payment", "contains", "Debt Payment", 30),
    ("student loan", "contains", "Debt Payment", 25),
    ("nslsc", "contains", "Debt Payment", 25),
    ("osap", "contains", "Debt Payment", 25),
    ("overlimit", "contains", "Fees & Interest", 40),

    ("payroll", "contains", "Income", 30),
    ("direct deposit", "contains", "Income", 35),
    ("polyai", "contains", "Income", 30),
    # Employers, by the string the deposit actually arrives as. Simplii
    # truncates the payer name, so "Nexxt Intelligence" lands as
    # "Nexxt Intellige" and no generic rule catches it -- $14,543 of pay sat
    # uncategorised, which made the surplus read negative.
    ("nexxt intellige", "contains", "Income", 28),
    ("people center", "contains", "Income", 28),
    # A refund from the CRA is income, not a mystery deposit.
    ("tax refund", "contains", "Income", 28),
    ("rembours", "contains", "Income", 28),

    ("thank you", "contains", "Transfer", 25),      # card payment, not income
    ("payment received", "contains", "Transfer", 25),
    # --- paying your own cards from your own chequing account ---
    # These settle transactions the app has ALREADY imported from the card
    # statement. Counting them as spending would double every card purchase.
    ("american express", "contains", "Transfer", 25),
    ("visa simplii", "contains", "Transfer", 25),
    ("simplii financial visa", "contains", "Transfer", 25),
    ("transfer out", "contains", "Transfer", 30),
    ("transfer in", "contains", "Transfer", 30),

    ("card load", "contains", "Transfer", 30),
    ("transfer from card", "contains", "Transfer", 30),
    ("e-transfer", "contains", "Transfer", 40),
    ("etransfer", "contains", "Transfer", 40),
    ("transfer to", "contains", "Transfer", 40),
    ("transfer from", "contains", "Transfer", 40),

    # --- meal-kit and delivery services ---
    # Filed as Groceries because they replace a grocery shop rather than a meal
    # out. One line to move to "Eats & Drinks" if you disagree.
    ("chefs plate", "contains", "Groceries", 35),
    ("chef's plate", "contains", "Groceries", 35),
    (r"\bfactor\b", "regex", "Groceries", 45),       # not BENEFACTOR / FACTORY
    ("hellofresh", "contains", "Groceries", 35),
    ("goodfood", "contains", "Groceries", 35),

    # --- telecom ---
    ("fido", "contains", "Phone & Internet", 35),
    ("virgin plus", "contains", "Phone & Internet", 35),
    ("chatr", "contains", "Phone & Internet", 35),

    # --- insurance ---
    ("square one insurance", "contains", "Insurance", 30),
    (r"\binsurance\b", "regex", "Insurance", 60),
    (r"\bsonnet\b", "regex", "Insurance", 45),
    (r"\bintact\b", "regex", "Insurance", 45),

    # --- personal care ---
    (r"\bbarber\b", "regex", "Personal Care", 45),
    (r"\bsalon\b", "regex", "Personal Care", 50),
    (r"\bspa\b", "regex", "Personal Care", 55),

    # --- entertainment ---
    ("toronto international f", "contains", "Entertainment", 35),  # TIFF
    ("ultimate challenge", "contains", "Entertainment", 45),
    ("escape room", "contains", "Entertainment", 45),

    # --- health and supplements ---
    ("naked nutrition", "contains", "Health", 40),
    ("myprotein", "contains", "Health", 40),
    (r"\bvitamin\b", "regex", "Health", 55),

    # --- housing and home services ---
    ("condos", "contains", "Housing", 45),
    ("taskrabbit", "contains", "Housing", 40),
    ("struc-tube", "contains", "Shopping", 40),
    ("structube", "contains", "Shopping", 40),
    ("wayfair", "contains", "Shopping", 45),
    ("home depot", "contains", "Shopping", 45),
    (r"\brona\b", "regex", "Shopping", 50),          # not CORONA

    # --- retail ---
    ("holt renfrew", "contains", "Shopping", 45),
    ("apple store", "contains", "Shopping", 40),
    ("adidas", "contains", "Shopping", 45),
    ("decathlon", "contains", "Shopping", 45),
    (r"\bnike\b", "regex", "Shopping", 45),
    (r"\bzara\b", "regex", "Shopping", 50),
    ("h&m", "contains", "Shopping", 50),
    ("sephora", "contains", "Personal Care", 45),
    ("fab india", "contains", "Shopping", 45),
    ("fabindia", "contains", "Shopping", 45),
    ("gift shop", "contains", "Shopping", 60),
    ("stag shop", "contains", "Shopping", 45),

    # --- eats ---
    ("the keg", "contains", "Eats & Drinks", 45),
    (r"\bkeg\b", "regex", "Eats & Drinks", 45),
    ("chipotle", "contains", "Eats & Drinks", 45),
    ("mcdonald", "contains", "Eats & Drinks", 45),
    (r"\bsubway\b", "regex", "Eats & Drinks", 50),
    (r"\bsushi\b", "regex", "Eats & Drinks", 55),
    ("hot pot", "contains", "Eats & Drinks", 55),
    ("momos", "contains", "Eats & Drinks", 55),
    (r"\bthai\b", "regex", "Eats & Drinks", 60),
    (r"\bbakery\b", "regex", "Eats & Drinks", 55),
    (r"\bbrewery\b", "regex", "Eats & Drinks", 55),

    # --- fees ---
    ("membership fee", "contains", "Fees & Interest", 30),
    ("returned payment", "contains", "Transfer", 25),
    ("balance transfer", "contains", "Transfer", 30),

    ("wealthsimple", "contains", "Savings & Investments", 35),
    ("questrade", "contains", "Savings & Investments", 35),
    ("tfsa", "contains", "Savings & Investments", 35),
    ("rrsp", "contains", "Savings & Investments", 35),
    ("fhsa", "contains", "Savings & Investments", 35),
]


def normalise(text: str) -> str:
    """Fold punctuation to spaces so word matching is not defeated by a hyphen.

    "PETRO-CANADA 04512" and "PETRO CANADA" have to look the same to a rule,
    or a merchant you corrected once never matches again.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9&']+", " ", (text or "").lower())).strip()


def seed_rules(conn: sqlite3.Connection) -> int:
    """Install the built-in rules, and retire ones this version no longer ships.

    Retiring matters: several seed patterns were plain substrings that caught
    the wrong merchants ("metro" matched METROLINX, "tiff" matched TIFFANY).
    Replacing them adds the corrected pattern but leaves the broken one in an
    existing database forever unless it is removed. Rules you taught (is_user)
    are never touched.
    """
    wanted = {(pattern, match_type) for pattern, match_type, _, _ in SEED_RULES}
    added = 0
    for pattern, match_type, category, priority in SEED_RULES:
        cur = conn.execute(
            "INSERT OR IGNORE INTO rules(pattern, match_type, category, priority, is_user) "
            "VALUES (?, ?, ?, ?, 0)",
            (pattern, match_type, category, priority),
        )
        added += cur.rowcount

    stale = [
        r["id"] for r in conn.execute("SELECT id, pattern, match_type FROM rules WHERE is_user = 0")
        if (r["pattern"], r["match_type"]) not in wanted
    ]
    if stale:
        conn.executemany("DELETE FROM rules WHERE id = ?", [(i,) for i in stale])
    conn.commit()
    return added


def load_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM rules ORDER BY priority ASC, length(pattern) DESC, id ASC"
    ).fetchall()


def rule_matches(rule: sqlite3.Row, text: str, normalised: str) -> bool:
    """Does one rule match this description?

    Three kinds:
      exact     -- the whole cleaned description equals the pattern
      regex     -- the pattern is a regular expression (seed rules use these
                   for word boundaries: \bmetro\b must not catch METROLINX)
      words     -- a whole-word sequence, matched against the NORMALISED text.
                   This is what rules you teach use, so "petro canada" still
                   matches "PETRO-CANADA 04512".
      contains  -- plain substring, matched against the raw lowered text.
    """
    kind = rule["match_type"]
    pattern = rule["pattern"]
    if kind == "exact":
        return text.strip().lower() == pattern.lower()
    if kind == "regex":
        return bool(re.search(pattern, text or "", re.I))
    if kind == "words":
        key = normalise(pattern)
        return bool(key) and bool(re.search(rf"\b{re.escape(key)}\b", normalised))
    return pattern.lower() in text.lower()


def match_category(text: str, rules: Iterable[sqlite3.Row]) -> Optional[tuple[str, int]]:
    """First matching rule wins, and rules arrive sorted by priority."""
    text = text or ""
    normalised = normalise(text)
    for rule in rules:
        if rule_matches(rule, text, normalised):
            return rule["category"], int(rule["id"])
    return None


def categorize_all(conn: sqlite3.Connection, recategorize: bool = False) -> dict:
    """Apply rules to transactions.

    Manual categorisations are never overwritten -- your correction outranks
    any rule, including one added later.

    An inflow that no rule explains is left UNCATEGORISED. An earlier version
    filed it as Income on the reasoning that most inflows are, which is true
    and still the wrong thing to do: a friend repaying $4,000 became a
    permanent $1,333/month raise in the surplus the goal engine spends, and
    because the row was no longer NULL nothing on the dashboard said so.
    """
    rules = load_rules(conn)
    where = (
        "WHERE category_source IS NULL OR category_source != 'manual'"
        if recategorize else "WHERE category IS NULL"
    )
    rows = conn.execute(
        f"SELECT id, description, raw_description, amount FROM transactions {where}"
    ).fetchall()

    matched = unmatched = 0
    for row in rows:
        text = f"{row['description']} {row['raw_description']}"
        result = match_category(text, rules)
        if result:
            conn.execute(
                "UPDATE transactions SET category = ?, category_source = 'rule' WHERE id = ?",
                (result[0], row["id"]),
            )
            matched += 1
        else:
            if recategorize:
                conn.execute(
                    "UPDATE transactions SET category = NULL, category_source = NULL WHERE id = ?",
                    (row["id"],),
                )
            unmatched += 1
    conn.commit()
    return {"matched": matched, "unmatched": unmatched, "considered": len(rows)}


@dataclass
class Taught:
    """What teaching a merchant actually did, so the UI can say so."""
    pattern: Optional[str] = None
    applied_to: int = 0
    refused: Optional[str] = None


def _would_shadow(conn: sqlite3.Connection, key: str, category: str) -> Optional[str]:
    """Is `key` a broader form of an existing rule for a DIFFERENT category?

    Teaching "uber" from a taxi ride would silently refile every Uber Eats
    order as Transport, because a taught rule outranks every seed rule. When
    the key is contained in a longer pattern that means something else, the
    honest move is to categorise this one transaction and say why nothing was
    learned.
    """
    for r in conn.execute("SELECT pattern, match_type, category FROM rules"):
        if r["category"] == category:
            continue
        other = normalise(r["pattern"])
        if not other or other == key:
            continue
        if len(other) > len(key) and re.search(rf"\b{re.escape(key)}\b", other):
            return r["pattern"]
    return None


def set_manual_category(
    conn: sqlite3.Connection, tx_id: int, category: str, teach: bool = True
) -> Taught:
    """Recategorise one transaction and, optionally, learn the merchant."""
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category: {category}")
    row = conn.execute("SELECT description FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if row is None:
        raise ValueError(f"No transaction with id {tx_id}")

    conn.execute(
        "UPDATE transactions SET category = ?, category_source = 'manual' WHERE id = ?",
        (category, tx_id),
    )

    result = Taught()
    if teach:
        key = merchant_key(row["description"])
        if not key or len(key) < 4:
            result.refused = "the merchant name is too short to make a reliable rule from"
        else:
            shadowed = _would_shadow(conn, key, category)
            if shadowed:
                result.refused = (
                    f"“{key}” would also have changed “{shadowed}” "
                    "transactions, so only this one was recategorised"
                )
            else:
                conn.execute(
                    "INSERT INTO rules(pattern, match_type, category, priority, is_user) "
                    "VALUES (?, 'words', ?, 10, 1) "
                    "ON CONFLICT(pattern, match_type) DO UPDATE SET "
                    "category = excluded.category, priority = 10, is_user = 1",
                    (key, category),
                )
                result.pattern = key
                result.applied_to = _apply_key(conn, key, category, skip_tx_id=tx_id)
    conn.commit()
    return result


def _apply_key(conn: sqlite3.Connection, key: str, category: str, skip_tx_id: int) -> int:
    """Re-file every other transaction from this merchant you have not corrected.

    Done in Python rather than with SQL LIKE because the match is word-bounded
    on a normalised description, and SQLite's LIKE has no word boundaries --
    a '%uber%' LIKE would sweep up UBER EATS.
    """
    pattern = re.compile(rf"\b{re.escape(key)}\b")
    ids = [
        r["id"] for r in conn.execute(
            "SELECT id, description FROM transactions "
            "WHERE id != ? AND (category_source IS NULL OR category_source != 'manual')",
            (skip_tx_id,),
        )
        if pattern.search(normalise(r["description"]))
    ]
    if ids:
        conn.executemany(
            "UPDATE transactions SET category = ?, category_source = 'rule' WHERE id = ?",
            [(category, i) for i in ids],
        )
    return len(ids)


def merchant_key(description: str) -> str:
    """Reduce a merchant string to the stable part worth making a rule from."""
    words = [w for w in normalise(description).split() if not w.isdigit()]
    return " ".join(words[:2]).strip()
