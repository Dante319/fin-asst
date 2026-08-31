"""Paths and tunable defaults.

Everything here is local to the machine. No network calls, no API keys,
no third-party services -- that is a design constraint, not an accident.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEGACY_DATA_DIR = PROJECT_ROOT / "data"


def _default_data_dir() -> Path:
    """Where the database lives when nothing overrides it.

    Deliberately NOT inside the repository. A project folder is very often
    inside iCloud Drive, Dropbox or OneDrive, and a SQLite file in a
    continuously-synced folder is a real corruption risk: the sync daemon can
    upload a half-written database mid-transaction, and two machines can
    resurrect each other's stale copies. The user data directory is never
    synced.

    An existing data/finasst.db from an earlier version still wins, so upgrading
    never silently orphans a database somebody has already been using.
    """
    if (LEGACY_DATA_DIR / "finasst.db").exists():
        return LEGACY_DATA_DIR
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "fin-asst"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "fin-asst"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "fin-asst"


DATA_DIR = Path(os.environ["FINASST_DATA_DIR"]) if os.environ.get("FINASST_DATA_DIR") else _default_data_dir()
DB_PATH = Path(os.environ["FINASST_DB"]) if os.environ.get("FINASST_DB") else DATA_DIR / "finasst.db"

DEFAULT_CURRENCY = "CAD"

# Sign convention used everywhere in this codebase:
#   amount < 0  -> money left the account (spending)
#   amount > 0  -> money entered the account (income, refunds, transfers in)
# Importers are responsible for normalising each issuer's format to this.

CATEGORIES = [
    "Groceries",
    "Eats & Drinks",
    "Transport",
    "Housing",
    "Utilities",
    "Phone & Internet",
    "Health",
    "Personal Care",
    "Insurance",
    "Shopping",
    "Subscriptions",
    "Travel",
    "Entertainment",
    "Remittance",
    "Fees & Interest",
    "Debt Payment",
    "Income",
    "Transfer",
    "Savings & Investments",
    "Other",
]

# Categories that are not really "spending" and must be excluded from
# surplus and burn-rate maths, or the numbers lie.
NON_SPEND_CATEGORIES = {"Income", "Transfer", "Savings & Investments"}


# ---------------------------------------------------------------------------
# Category groups
# ---------------------------------------------------------------------------
# Twenty categories is the right granularity for a transaction, and far too many
# for a chart: a categorical palette runs out of hues that stay distinguishable
# (to colour-blind readers especially) at about four when any two can end up side
# by side. So spending categories roll up into four groups, and colour encodes
# the GROUP while the category name is always written out beside the bar. The
# four hues were chosen by running the palette validator over every subset of the
# reference palette and keeping only sets that pass on all pairs in both light
# and dark; these four were the largest that did.
#
# "Uncategorised" is deliberately not a group. It renders neutral grey, because
# the honest thing for money we cannot classify is to look unclassified.

CATEGORY_GROUPS: dict[str, str] = {
    # Essentials -- the bills that arrive whether or not you do anything.
    "Housing": "Essentials",
    "Utilities": "Essentials",
    "Phone & Internet": "Essentials",
    "Insurance": "Essentials",
    "Transport": "Essentials",
    "Health": "Essentials",
    # Food -- split out from Essentials because it is the biggest controllable
    # line in most months, and burying it in "Essentials" hides that.
    "Groceries": "Food & drink",
    "Eats & Drinks": "Food & drink",
    # Lifestyle -- discretionary. The part a budget can actually move.
    "Shopping": "Lifestyle",
    "Subscriptions": "Lifestyle",
    "Entertainment": "Lifestyle",
    "Travel": "Lifestyle",
    "Personal Care": "Lifestyle",
    # Commitments -- money out that is neither a bill nor a choice this month.
    "Remittance": "Commitments",
    "Debt Payment": "Commitments",
    "Fees & Interest": "Commitments",
    "Other": "Commitments",
}

# Fixed order. Colour follows the group, never its rank in a given month, so a
# filter that drops a group never repaints the others.
GROUP_ORDER = ["Essentials", "Food & drink", "Lifestyle", "Commitments"]
UNGROUPED = "Uncategorised"


def group_of(category: str | None) -> str:
    """Which spend group a category rolls up into."""
    return CATEGORY_GROUPS.get(category or "", UNGROUPED)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
