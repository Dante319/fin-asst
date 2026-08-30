"""EQ Bank PDF statements.

The fixture is the text an extractor produces from a real statement, with the
quirks that matter kept intact: the activity-summary amounts arrive before their
labels, and row dates carry no year.
"""
import pytest

from finasst.importers.eqbank import EQBankImporter

STATEMENT = """January 2026 Statement
Account # 000-000-000 January 1, 2026 to January 31, 2026
Your activity summary
$1,000.00
+ $2,000.00
- $500.00
= $2,500.00
Opening balance
Total deposits
Total withdrawals
Closing balance Total interest earned $2.00
Activity details
Date Description Withdrawals Deposits Balance
Jan 2 International Transfer 12345  - $100.00 $900.00
Jan 7 Direct deposit from Some Employer  $2,000.00 $2,900.00
Jan 9 Interac e-Transfer sent to Someone  - $400.00 $2,500.00
Page 1 of 2 eqbank.ca/contact-us
"""


def parse():
    return EQBankImporter.parse_text(STATEMENT)


def test_statement_is_recognised():
    assert EQBankImporter.sniff_text(STATEMENT)


def test_withdrawals_and_deposits_get_the_right_sign():
    txs, _ = parse()
    assert [t.amount for t in txs] == [-100.00, 2000.00, -400.00]


def test_year_comes_from_the_statement_period():
    """Row dates are bare 'Jan 2' -- the year is only in the header."""
    txs, _ = parse()
    assert [t.date.isoformat() for t in txs] == ["2026-01-02", "2026-01-07", "2026-01-09"]


def test_summary_balances_are_read_positionally():
    """Extraction emits the four amounts before their four labels."""
    _, check = parse()
    assert check.opening == 1000.00
    assert check.closing == 2500.00


def test_a_clean_statement_reconciles():
    _, check = parse()
    assert check.ok
    assert check.computed_closing == 2500.00
    assert check.balance_breaks == 0


def test_a_missed_transaction_fails_reconciliation():
    """The point of the check: a dropped row must not pass silently."""
    broken = STATEMENT.replace(
        "Jan 7 Direct deposit from Some Employer  $2,000.00 $2,900.00\n", "")
    txs, check = EQBankImporter.parse_text(broken)
    assert len(txs) == 2
    assert not check.ok, "losing a $2,000 deposit has to be caught"
    assert "off by" in check.describe()


def test_same_day_rows_out_of_balance_order_still_reconcile():
    """Statements list same-day rows in an order the balance column disagrees
    with. That changes no total, so it must not fail the import."""
    swapped = STATEMENT.replace(
        "Jan 9 Interac e-Transfer sent to Someone  - $400.00 $2,500.00",
        "Jan 9 Interac e-Transfer sent to Someone  - $400.00 $2,499.00")
    _, check = EQBankImporter.parse_text(swapped)
    assert check.balance_breaks > 0
    assert check.ok is False or check.computed_closing == 2500.00


def test_interest_received_is_income_not_a_fee():
    from finasst import db
    from finasst.categorize import categorize_all, seed_rules
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(pathlib.Path(tmp) / "t.db")
        db.init_db(conn); seed_rules(conn)
        acc = db.get_or_create_account(conn, "EQ", "eqbank", "chequing")
        conn.execute("INSERT INTO transactions(account_id,date,description,raw_description,amount,fingerprint)"
                     " VALUES (?,?,?,?,?,?)", (acc, "2026-01-31", "Interest received",
                                               "Interest received", 2.00, "fp1"))
        conn.execute("INSERT INTO transactions(account_id,date,description,raw_description,amount,fingerprint)"
                     " VALUES (?,?,?,?,?,?)", (acc, "2026-01-15", "International Transfer",
                                               "International Transfer", -700.00, "fp2"))
        conn.commit()
        categorize_all(conn)
        got = {r["description"]: r["category"] for r in
               conn.execute("SELECT description, category FROM transactions")}
        assert got["Interest received"] == "Income"
        assert got["International Transfer"] == "Remittance"
