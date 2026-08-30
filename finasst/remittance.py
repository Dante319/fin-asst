"""Remittance and FX ledger.

Money sent to India is the largest single category in this data, and a bank
statement records only half of it: the CAD that left. What actually arrived, and
what the conversion cost, is nowhere in any file the app can import.

So the ledger works from both ends. The CAD side is populated automatically from
categorised transactions. The received amount is something you enter once per
transfer, from the provider's confirmation. With both, the effective rate is
arithmetic rather than guesswork, and comparing it to a reference mid-market
rate turns "the fee was $0" into the real cost of the spread.

Reference rates are cached locally and fetched ONLY when you explicitly ask.
Nothing here touches the network on its own.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

RATE_API = "https://api.frankfurter.app/{d}?from={base}&to={quote}"
DEFAULT_QUOTE = "INR"


# ---------------------------------------------------------------------------
# Building the ledger from imported transactions
# ---------------------------------------------------------------------------

def sync_from_transactions(conn: sqlite3.Connection, category: str = "Remittance") -> int:
    """Create a ledger row for every remittance transaction that lacks one."""
    rows = conn.execute(
        """SELECT t.id, t.amount FROM transactions t
           LEFT JOIN remittances r ON r.tx_id = t.id
           WHERE t.category = ? AND t.amount < 0 AND r.id IS NULL""",
        (category,),
    ).fetchall()
    for row in rows:
        conn.execute(
            "INSERT INTO remittances(tx_id, sent_amount) VALUES (?, ?)",
            (row["id"], round(-row["amount"], 2)),
        )
    conn.commit()
    return len(rows)


def set_received(
    conn: sqlite3.Connection,
    tx_id: int,
    received: float,
    fee: Optional[float] = None,
    provider: Optional[str] = None,
    currency: str = DEFAULT_QUOTE,
) -> None:
    row = conn.execute("SELECT id FROM remittances WHERE tx_id = ?", (tx_id,)).fetchone()
    if row is None:
        tx = conn.execute("SELECT amount FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if tx is None:
            raise ValueError(f"No transaction with id {tx_id}")
        conn.execute("INSERT INTO remittances(tx_id, sent_amount) VALUES (?, ?)",
                     (tx_id, round(-tx["amount"], 2)))
    conn.execute(
        """UPDATE remittances SET received = ?, received_ccy = ?,
             fee = COALESCE(?, fee), provider = COALESCE(?, provider),
             updated_at = datetime('now')
           WHERE tx_id = ?""",
        (received, currency, fee, provider, tx_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Reference rates
# ---------------------------------------------------------------------------

def cached_rate(conn: sqlite3.Connection, on: str, base: str = "CAD", quote: str = DEFAULT_QUOTE) -> Optional[float]:
    row = conn.execute(
        "SELECT rate FROM fx_rates WHERE date <= ? AND base = ? AND quote = ? ORDER BY date DESC LIMIT 1",
        (on, base, quote),
    ).fetchone()
    return float(row["rate"]) if row else None


def fetch_rates(
    conn: sqlite3.Connection,
    dates: Iterable[str],
    base: str = "CAD",
    quote: str = DEFAULT_QUOTE,
    timeout: int = 15,
) -> tuple[int, list[str]]:
    """Fetch reference rates for specific dates and cache them.

    Uses the European Central Bank's published rates via frankfurter.app: free,
    no account, no key. Called only from `finasst fx rates`, never implicitly.
    Returns (rates stored, errors).
    """
    stored, errors = 0, []
    for d in sorted(set(dates)):
        if conn.execute(
            "SELECT 1 FROM fx_rates WHERE date = ? AND base = ? AND quote = ?", (d, base, quote)
        ).fetchone():
            continue
        try:
            with urllib.request.urlopen(RATE_API.format(d=d, base=base, quote=quote), timeout=timeout) as resp:
                payload = json.load(resp)
            rate = payload.get("rates", {}).get(quote)
            if rate is None:
                errors.append(f"{d}: no {quote} rate published")
                continue
            conn.execute(
                "INSERT OR REPLACE INTO fx_rates(date, base, quote, rate, source) VALUES (?,?,?,?,'ecb')",
                (payload.get("date", d), base, quote, float(rate)),
            )
            stored += 1
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{d}: {exc}")
    conn.commit()
    return stored, errors


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

@dataclass
class Transfer:
    tx_id: int
    date: str
    description: str
    sent: float
    received: Optional[float]
    currency: str
    provider: Optional[str]
    reference_rate: Optional[float]

    @property
    def effective_rate(self) -> Optional[float]:
        if not self.received or not self.sent:
            return None
        return self.received / self.sent

    @property
    def spread_pct(self) -> Optional[float]:
        """How far below the reference rate you actually got, as a percentage."""
        if self.effective_rate is None or not self.reference_rate:
            return None
        return 100 * (self.reference_rate - self.effective_rate) / self.reference_rate

    @property
    def cost_cad(self) -> Optional[float]:
        """What the spread cost, expressed in CAD."""
        if self.spread_pct is None:
            return None
        return self.sent * self.spread_pct / 100


@dataclass
class Report:
    transfers: list[Transfer]

    @property
    def total_sent(self) -> float:
        return round(sum(t.sent for t in self.transfers), 2)

    @property
    def priced(self) -> list[Transfer]:
        return [t for t in self.transfers if t.effective_rate is not None]

    @property
    def missing_received(self) -> list[Transfer]:
        return [t for t in self.transfers if t.received is None]

    @property
    def total_received(self) -> float:
        return round(sum(t.received for t in self.priced), 2)

    @property
    def blended_rate(self) -> Optional[float]:
        sent = sum(t.sent for t in self.priced)
        return (self.total_received / sent) if sent else None

    @property
    def total_cost(self) -> Optional[float]:
        costs = [t.cost_cad for t in self.priced if t.cost_cad is not None]
        return round(sum(costs), 2) if costs else None

    def coverage_note(self) -> str:
        if not self.transfers:
            return "No remittances found. Categorise a transaction as Remittance first."
        if not self.priced:
            return (
                f"{len(self.transfers)} transfers totalling ${self.total_sent:,.2f} sent, but none "
                "have a received amount recorded yet, so the real cost is unknown. "
                "Add one with: finasst fx set <tx_id> --received <INR>"
            )
        pct = 100 * len(self.priced) / len(self.transfers)
        return f"{len(self.priced)} of {len(self.transfers)} transfers priced ({pct:.0f}%)."


def report(conn: sqlite3.Connection, base: str = "CAD") -> Report:
    rows = conn.execute(
        """SELECT r.*, t.date, t.description FROM remittances r
           JOIN transactions t ON t.id = r.tx_id ORDER BY t.date"""
    ).fetchall()
    transfers = [
        Transfer(
            tx_id=r["tx_id"], date=r["date"], description=r["description"],
            sent=float(r["sent_amount"]),
            received=float(r["received"]) if r["received"] is not None else None,
            currency=r["received_ccy"], provider=r["provider"],
            reference_rate=cached_rate(conn, r["date"], base, r["received_ccy"]),
        )
        for r in rows
    ]
    return Report(transfers)
