"""Deterministic categorisation.

No model, no API call, no guessing you cannot audit: a transaction gets a
category because a rule you can read matched it. When you recategorise
something by hand, that correction is stored as a user rule with top priority,
so the same merchant is right forever after.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Iterable, Optional

from .config import CATEGORIES

# Seed rules. Tuned for Toronto spending and a Canada-India money flow.
# priority: lower wins. User-taught rules are inserted at priority 10.
SEED_RULES: list[tuple[str, str, str, int]] = [
    # (pattern, match_type, category, priority)
    ("loblaw", "contains", "Groceries", 50),
    ("no frills", "contains", "Groceries", 50),
    ("nofrills", "contains", "Groceries", 50),
    ("metro", "contains", "Groceries", 60),
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
    ("restaurant", "contains", "Eats & Drinks", 70),
    ("pizza", "contains", "Eats & Drinks", 60),
    ("cafe", "contains", "Eats & Drinks", 65),
    ("coffee", "contains", "Eats & Drinks", 65),
    ("bar ", "contains", "Eats & Drinks", 70),
    ("lcbo", "contains", "Eats & Drinks", 50),
    ("beer store", "contains", "Eats & Drinks", 50),

    ("uber", "contains", "Transport", 60),   # after uber eats, so it loses to it
    ("lyft", "contains", "Transport", 50),
    ("presto", "contains", "Transport", 50),
    ("ttc", "contains", "Transport", 50),
    ("via rail", "contains", "Transport", 50),
    ("petro-canada", "contains", "Transport", 50),
    ("esso", "contains", "Transport", 50),
    ("shell", "contains", "Transport", 55),
    ("green p", "contains", "Transport", 50),
    ("parking", "contains", "Transport", 60),

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
    ("fizz", "contains", "Phone & Internet", 45),
    ("beanfield", "contains", "Phone & Internet", 40),

    ("shoppers drug", "contains", "Health", 45),
    ("rexall", "contains", "Health", 45),
    ("dental", "contains", "Health", 50),
    ("physio", "contains", "Health", 50),
    ("goodlife", "contains", "Health", 45),
    ("fitness", "contains", "Health", 60),

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
    ("crave", "contains", "Subscriptions", 45),
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
    ("hotel", "contains", "Travel", 60),
    ("flighthub", "contains", "Travel", 40),

    ("cineplex", "contains", "Entertainment", 45),
    ("tiff", "contains", "Entertainment", 45),
    ("ticketmaster", "contains", "Entertainment", 45),
    ("dice.fm", "contains", "Entertainment", 40),
    ("eventbrite", "contains", "Entertainment", 45),
    ("steam", "contains", "Entertainment", 50),
    ("playstation", "contains", "Entertainment", 45),
    ("nintendo", "contains", "Entertainment", 45),

    ("international transfer", "contains", "Remittance", 25),
    ("wise", "contains", "Remittance", 35),
    ("remitly", "contains", "Remittance", 35),
    ("western union", "contains", "Remittance", 35),
    ("xe money", "contains", "Remittance", 35),
    ("moneygram", "contains", "Remittance", 35),
    ("instarem", "contains", "Remittance", 35),

    # Interest RECEIVED is income; interest CHARGED is a cost. Order matters.
    ("interest received", "contains", "Income", 25),
    ("interest earned", "contains", "Income", 25),
    ("interest", "contains", "Fees & Interest", 60),
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
    ("factor", "contains", "Groceries", 45),
    ("hellofresh", "contains", "Groceries", 35),
    ("goodfood", "contains", "Groceries", 35),

    # --- telecom ---
    ("fido", "contains", "Phone & Internet", 35),
    ("virgin plus", "contains", "Phone & Internet", 35),
    ("chatr", "contains", "Phone & Internet", 35),

    # --- insurance ---
    ("square one insurance", "contains", "Insurance", 30),
    ("insurance", "contains", "Insurance", 60),
    ("sonnet", "contains", "Insurance", 45),
    ("intact", "contains", "Insurance", 45),

    # --- personal care ---
    ("barber", "contains", "Personal Care", 45),
    ("salon", "contains", "Personal Care", 50),
    ("spa ", "contains", "Personal Care", 55),

    # --- entertainment ---
    ("toronto international f", "contains", "Entertainment", 35),  # TIFF
    ("ultimate challenge", "contains", "Entertainment", 45),
    ("escape room", "contains", "Entertainment", 45),

    # --- health and supplements ---
    ("naked nutrition", "contains", "Health", 40),
    ("myprotein", "contains", "Health", 40),
    ("vitamin", "contains", "Health", 55),

    # --- housing and home services ---
    ("condos", "contains", "Housing", 45),
    ("taskrabbit", "contains", "Housing", 40),
    ("struc-tube", "contains", "Shopping", 40),
    ("structube", "contains", "Shopping", 40),
    ("wayfair", "contains", "Shopping", 45),
    ("home depot", "contains", "Shopping", 45),
    ("rona", "contains", "Shopping", 50),

    # --- retail ---
    ("holt renfrew", "contains", "Shopping", 45),
    ("apple store", "contains", "Shopping", 40),
    ("adidas", "contains", "Shopping", 45),
    ("decathlon", "contains", "Shopping", 45),
    ("nike", "contains", "Shopping", 45),
    ("zara", "contains", "Shopping", 50),
    ("h&m", "contains", "Shopping", 50),
    ("sephora", "contains", "Personal Care", 45),
    ("fab india", "contains", "Shopping", 45),
    ("fabindia", "contains", "Shopping", 45),
    ("gift shop", "contains", "Shopping", 60),
    ("stag shop", "contains", "Shopping", 45),

    # --- eats ---
    ("the keg", "contains", "Eats & Drinks", 45),
    ("keg -", "contains", "Eats & Drinks", 45),
    ("chipotle", "contains", "Eats & Drinks", 45),
    ("mcdonald", "contains", "Eats & Drinks", 45),
    ("subway", "contains", "Eats & Drinks", 50),
    ("sushi", "contains", "Eats & Drinks", 55),
    ("hot pot", "contains", "Eats & Drinks", 55),
    ("momos", "contains", "Eats & Drinks", 55),
    ("thai", "contains", "Eats & Drinks", 60),
    ("bakery", "contains", "Eats & Drinks", 55),
    ("brewery", "contains", "Eats & Drinks", 55),
    ("bar &", "contains", "Eats & Drinks", 60),

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


def seed_rules(conn: sqlite3.Connection) -> int:
    added = 0
    for pattern, match_type, category, priority in SEED_RULES:
        cur = conn.execute(
            "INSERT OR IGNORE INTO rules(pattern, match_type, category, priority, is_user) "
            "VALUES (?, ?, ?, ?, 0)",
            (pattern, match_type, category, priority),
        )
        added += cur.rowcount
    conn.commit()
    return added


def load_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM rules ORDER BY priority ASC, length(pattern) DESC, id ASC"
    ).fetchall()


def match_category(text: str, rules: Iterable[sqlite3.Row]) -> Optional[tuple[str, int]]:
    """First matching rule wins, and rules arrive sorted by priority."""
    haystack = (text or "").lower()
    for rule in rules:
        pattern = rule["pattern"].lower()
        kind = rule["match_type"]
        hit = (
            haystack == pattern if kind == "exact"
            else bool(re.search(rule["pattern"], text or "", re.I)) if kind == "regex"
            else pattern in haystack
        )
        if hit:
            return rule["category"], int(rule["id"])
    return None


def categorize_all(conn: sqlite3.Connection, recategorize: bool = False) -> dict:
    """Apply rules to transactions.

    Manual categorisations are never overwritten -- your correction outranks
    any rule, including one added later.
    """
    rules = load_rules(conn)
    where = "" if recategorize else "WHERE category IS NULL"
    if recategorize:
        where = "WHERE category_source IS NULL OR category_source != 'manual'"
    rows = conn.execute(f"SELECT id, description, raw_description, amount FROM transactions {where}").fetchall()

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
            # An inflow with no rule is far more likely income than "Other".
            fallback = "Income" if row["amount"] > 0 else None
            if fallback:
                conn.execute(
                    "UPDATE transactions SET category = ?, category_source = 'default' WHERE id = ?",
                    (fallback, row["id"]),
                )
                matched += 1
            else:
                unmatched += 1
    conn.commit()
    return {"matched": matched, "unmatched": unmatched, "considered": len(rows)}


def set_manual_category(
    conn: sqlite3.Connection, tx_id: int, category: str, teach: bool = True
) -> Optional[str]:
    """Recategorise one transaction and, optionally, learn the merchant.

    Returns the pattern that was learned, if any.
    """
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category: {category}")
    row = conn.execute("SELECT description FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if row is None:
        raise ValueError(f"No transaction with id {tx_id}")

    conn.execute(
        "UPDATE transactions SET category = ?, category_source = 'manual' WHERE id = ?",
        (category, tx_id),
    )

    learned = None
    if teach:
        pattern = merchant_key(row["description"])
        if pattern and len(pattern) >= 4:
            conn.execute(
                "INSERT INTO rules(pattern, match_type, category, priority, is_user) "
                "VALUES (?, 'contains', ?, 10, 1) "
                "ON CONFLICT(pattern, match_type) DO UPDATE SET category = excluded.category, priority = 10, is_user = 1",
                (pattern, category),
            )
            learned = pattern
            # Apply the new rule to every other transaction from this merchant
            # that you have not already corrected by hand.
            conn.execute(
                "UPDATE transactions SET category = ?, category_source = 'rule' "
                "WHERE lower(description) LIKE ? AND id != ? "
                "AND (category_source IS NULL OR category_source != 'manual')",
                (category, f"%{pattern}%", tx_id),
            )
    conn.commit()
    return learned


def merchant_key(description: str) -> str:
    """Reduce a merchant string to the stable part worth making a rule from."""
    s = re.sub(r"[^a-z0-9 &']", " ", (description or "").lower())
    s = re.sub(r"\s+", " ", s).strip()
    words = [w for w in s.split() if not w.isdigit()]
    return " ".join(words[:2]).strip()
