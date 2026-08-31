"""Aggregates that used to report the wrong number."""
import pytest

from finasst import analytics, coverage, db


def setup(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    return conn


def add(conn, account, date, desc, amount, category=None):
    acc = db.get_or_create_account(conn, account, "generic")
    conn.execute(
        "INSERT INTO transactions(account_id,date,description,raw_description,amount,category,fingerprint)"
        " VALUES (?,?,?,?,?,?,?)",
        (acc, date, desc, desc, amount, category, f"{account}-{date}-{desc}-{amount}"),
    )
    conn.commit()


def test_an_uncategorised_deposit_does_not_cancel_real_spending(tmp_path):
    """Netting inflow against outflow before classifying understated spending.

    A $500 deposit with no category subtracted $500 from the month's spending
    and added it to the surplus, and because the netted figure went negative
    the "% uncategorised" warning could not fire either.
    """
    conn = setup(tmp_path)
    add(conn, "Chequing", "2026-06-01", "PAYROLL", 4000, "Income")
    add(conn, "Chequing", "2026-06-02", "LOBLAWS", -3000, "Groceries")
    add(conn, "Chequing", "2026-06-03", "MYSTERY DEPOSIT", 500)
    add(conn, "Chequing", "2026-06-04", "MYSTERY SPEND", -100)
    m = analytics.monthly_totals(conn)[0]
    assert m["spend"] == 3100
    assert m["surplus"] == 900
    assert m["unknown_income"] == 500
    assert m["income"] == 4000


def test_a_matched_internal_transfer_leaves_the_totals(tmp_path):
    """The matcher found it; the totals have to act on that."""
    conn = setup(tmp_path)
    add(conn, "Chequing", "2026-06-01", "PAYROLL", 4000, "Income")
    add(conn, "Chequing", "2026-06-02", "LOBLAWS", -1000, "Groceries")
    add(conn, "Chequing", "2026-06-03", "AMEX PAYMENT", -1500)
    add(conn, "Amex", "2026-06-03", "PAYMENT RECEIVED", 1500)
    coverage.match_internal_transfers(conn)
    m = analytics.monthly_totals(conn)[0]
    assert m["income"] == 4000
    assert m["spend"] == 1000


def test_a_refund_reduces_that_category(tmp_path):
    conn = setup(tmp_path)
    add(conn, "Amex", "2026-06-01", "UNIQLO", -120, "Shopping")
    add(conn, "Amex", "2026-06-02", "UNIQLO REFUND", 40, "Shopping")
    m = analytics.monthly_totals(conn)[0]
    assert m["spend"] == 80


def test_recurring_charges_report_a_monthly_cost_not_a_per_charge_average(tmp_path):
    """Four $50 visits a month is a $200/month habit, not a $50 one."""
    conn = setup(tmp_path)
    for month in ("04", "05", "06"):
        for day in ("02", "09", "16", "23"):
            add(conn, "Amex", f"2026-{month}-{day}", "LOBLAWS #1032", -50, "Groceries")
    rows = analytics.recurring_charges(conn)
    assert rows[0]["merchant"] == "LOBLAWS #1032"
    assert rows[0]["monthly"] == 200.0
    assert rows[0]["charges"] == 12


def test_a_wildly_variable_merchant_is_not_a_fixed_cost(tmp_path):
    conn = setup(tmp_path)
    for month, amount in (("04", 5), ("05", 500), ("06", 2000)):
        add(conn, "Amex", f"2026-{month}-10", "RANDOM VENDOR", -amount, "Shopping")
    assert [r for r in analytics.recurring_charges(conn) if r["merchant"] == "RANDOM VENDOR"] == []


def test_a_cancelled_subscription_stops_counting(tmp_path):
    conn = setup(tmp_path)
    for month in ("01", "02", "03"):
        add(conn, "Amex", f"2026-{month}-05", "DEAD SUBSCRIPTION", -20, "Subscriptions")
    for month in ("04", "05", "06", "07"):
        add(conn, "Amex", f"2026-{month}-05", "NETFLIX.COM", -20, "Subscriptions")
    names = [r["merchant"] for r in analytics.recurring_charges(conn)]
    assert "NETFLIX.COM" in names
    assert "DEAD SUBSCRIPTION" not in names


@pytest.mark.parametrize("bad", ["", "abc", "2026", "2026-13", "20260-1", None, "2026-1"])
def test_valid_month_rejects_anything_that_is_not_yyyy_mm(bad):
    assert analytics.valid_month(bad) is None


def test_valid_month_accepts_a_real_month():
    assert analytics.valid_month("2026-07") == "2026-07"


def test_a_nonsense_month_does_not_break_the_breakdown(tmp_path):
    """It arrives from a query string; `?month=abc` used to be a 500."""
    conn = setup(tmp_path)
    add(conn, "Amex", "2026-06-01", "LOBLAWS", -100, "Groceries")
    assert analytics.category_breakdown(conn, "abc", 1)[0]["category"] == "Groceries"
    assert analytics.category_breakdown(conn, "2026-06", 0)[0]["category"] == "Groceries"
    assert analytics.category_breakdown(conn, "2026-06", -5)[0]["category"] == "Groceries"


def test_uncategorised_count_is_not_capped_by_a_page_size(tmp_path):
    conn = setup(tmp_path)
    for i in range(30):
        add(conn, "Amex", "2026-06-01", f"UNKNOWN {i}", -10)
    assert analytics.uncategorised_count(conn) == 30
