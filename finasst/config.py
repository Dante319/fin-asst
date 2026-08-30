"""Paths and tunable defaults.

Everything here is local to the machine. No network calls, no API keys,
no third-party services -- that is a design constraint, not an accident.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("FINASST_DATA_DIR", PROJECT_ROOT / "data"))
DB_PATH = Path(os.environ.get("FINASST_DB", DATA_DIR / "finasst.db"))

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
    "Shopping",
    "Subscriptions",
    "Travel",
    "Entertainment",
    "Remittance",
    "Fees & Interest",
    "Income",
    "Transfer",
    "Savings & Investments",
    "Other",
]

# Categories that are not really "spending" and must be excluded from
# surplus and burn-rate maths, or the numbers lie.
NON_SPEND_CATEGORIES = {"Income", "Transfer", "Savings & Investments"}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
