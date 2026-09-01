"""The insight detectors, and the cases where they must stay quiet."""
import pytest

from finasst import coverage, db, insights
from finasst.categorize import categorize_all, seed_rules

MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]


def setup(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    seed_rules(conn)
    db.get_or_create_account(conn, "Amex", "amex")
    return conn


def tx(conn, day, desc, amount, sign=-1, i=[0]):
    i[0] += 1
    conn.execute(
        "INSERT INTO transactions(account_id,date,description,raw_description,amount,fingerprint)"
        " VALUES (1,?,?,?,?,?)", (day, desc, desc, sign * amount, f"fp{i[0]}"))
    conn.commit()


def close_months(conn, months=MONTHS):
    """Put a charge on the last day of each month.

    Without this the fixture's newest month is only covered to the day of its
    last transaction, and the app -- correctly -- refuses to treat a
    half-covered month as finished. Real statements run to the end of a period;
    the fixtures have to as well or they are testing a shape that does not occur.
    """
    import calendar

    for m in months:
        last = calendar.monthrange(int(m[:4]), int(m[5:7]))[1]
        tx(conn, f"{m}-{last:02d}", "TTC FARE", 3.35)


def finish(conn, months=MONTHS):
    close_months(conn, months)
    categorize_all(conn)
    coverage.match_internal_transfers(conn)


def steady(conn, desc, amount, months=MONTHS, day="07"):
    for m in months:
        tx(conn, f"{m}-{day}", desc, amount)


def kinds(found):
    return {(f.kind, f.merchant) for f in found}


def test_nothing_is_reported_without_enough_history(tmp_path):
    conn = setup(tmp_path)
    tx(conn, "2026-06-01", "NETFLIX.COM", 20.99)
    finish(conn, ["2026-06"])
    assert insights.all_insights(conn) == []


def test_a_subscription_price_rise_is_found(tmp_path):
    conn = setup(tmp_path)
    steady(conn, "NETFLIX.COM", 20.99, MONTHS[:-1])
    tx(conn, "2026-06-07", "NETFLIX.COM", 24.99)
    finish(conn)
    hit = next(f for f in insights.all_insights(conn) if f.kind == "price_change")
    assert "NETFLIX" in hit.merchant
    assert round(hit.amount, 2) == 4.00
    assert "20.99" in hit.basis and "24.99" in hit.basis


def test_a_ten_fold_jump_is_an_outlier_not_a_repricing(tmp_path):
    """Nobody reprices by ten times. That is one strange charge."""
    conn = setup(tmp_path)
    steady(conn, "BAR RAVAL TORONTO", 60.0, MONTHS[:-1], day="15")
    tx(conn, "2026-06-15", "BAR RAVAL TORONTO", 640.0)
    finish(conn)
    found = kinds(insights.all_insights(conn))
    assert ("unusual_charge", "BAR RAVAL TORONTO") in found
    assert ("price_change", "BAR RAVAL TORONTO") not in found


def test_a_category_spike_caused_by_one_charge_is_not_reported_twice(tmp_path):
    conn = setup(tmp_path)
    steady(conn, "BAR RAVAL TORONTO", 60.0, MONTHS[:-1], day="15")
    tx(conn, "2026-06-15", "BAR RAVAL TORONTO", 640.0)
    finish(conn)
    found = insights.all_insights(conn)
    assert any(f.kind == "unusual_charge" for f in found)
    assert not any(f.kind == "category_spike" for f in found)


def test_a_new_monthly_charge_is_noticed(tmp_path):
    conn = setup(tmp_path)
    steady(conn, "NETFLIX.COM", 20.99)
    steady(conn, "SPOTIFY P0A4B", 11.99, ["2026-04", "2026-05", "2026-06"], day="20")
    finish(conn)
    hit = next(f for f in insights.all_insights(conn) if f.kind == "recurring_started")
    assert "SPOTIFY" in hit.merchant
    assert hit.month == "2026-04"


def test_a_recurring_charge_that_stops_is_noticed(tmp_path):
    conn = setup(tmp_path)
    steady(conn, "NETFLIX.COM", 20.99)
    steady(conn, "CRAVE TV", 19.99, MONTHS[:-1], day="22")
    finish(conn)
    hit = next(f for f in insights.all_insights(conn) if f.kind == "recurring_stopped")
    assert "CRAVE" in hit.merchant


def test_the_same_amount_days_apart_is_flagged(tmp_path):
    conn = setup(tmp_path)
    steady(conn, "NETFLIX.COM", 20.99)
    tx(conn, "2026-06-03", "APPLE STORE TORONTO", 899.00)
    tx(conn, "2026-06-05", "APPLE STORE TORONTO", 899.00)
    finish(conn)
    hit = next(f for f in insights.all_insights(conn) if f.kind == "possible_duplicate")
    assert hit.severity == "flag"
    assert "2026-06-03" in hit.basis and "2026-06-05" in hit.basis


def test_small_repeats_are_not_flagged_as_duplicates(tmp_path):
    """Two coffees in a week is not a double charge."""
    conn = setup(tmp_path)
    steady(conn, "NETFLIX.COM", 20.99)
    tx(conn, "2026-06-03", "STARBUCKS #4471", 6.25)
    tx(conn, "2026-06-04", "STARBUCKS #4471", 6.25)
    finish(conn)
    assert not any(f.kind == "possible_duplicate" for f in insights.all_insights(conn))


def test_every_insight_carries_its_arithmetic(tmp_path):
    conn = setup(tmp_path)
    steady(conn, "NETFLIX.COM", 20.99, MONTHS[:-1])
    tx(conn, "2026-06-07", "NETFLIX.COM", 24.99)
    tx(conn, "2026-06-03", "APPLE STORE TORONTO", 899.00)
    tx(conn, "2026-06-05", "APPLE STORE TORONTO", 899.00)
    finish(conn)
    found = insights.all_insights(conn)
    assert found
    for f in found:
        assert f.basis.strip(), f"{f.kind} has no basis"
        assert f.headline.strip() and f.detail.strip()
        assert f.severity in insights.SEVERITY_ORDER


# ------------------------------------------------------------------ forecast --

def test_a_forecast_needs_history(tmp_path):
    conn = setup(tmp_path)
    tx(conn, "2026-06-01", "PAYROLL", 5000, sign=1)
    finish(conn, ["2026-06"])
    f = insights.forecast(conn)
    assert f.ok is False
    assert f.months == []
    assert "at least" in f.warnings[0]


def test_a_forecast_shows_a_range_not_just_a_number(tmp_path):
    conn = setup(tmp_path)
    for m in MONTHS:
        tx(conn, f"{m}-01", "PAYROLL DEPOSIT POLYAI", 5000, sign=1)
        tx(conn, f"{m}-07", "NETFLIX.COM", 20.99)
    tx(conn, "2026-05-20", "AIR CANADA", 1400)      # one expensive month
    finish(conn)
    f = insights.forecast(conn)
    assert f.ok is True
    assert len(f.months) == insights.FORECAST_MONTHS
    first = f.months[0]
    assert first["worst"] < first["surplus"] <= first["best"]
    assert "median of" in f.basis


def test_a_stale_forecast_says_so(tmp_path):
    """History that stops months ago must not be projected forward silently."""
    conn = setup(tmp_path)
    window = ["2025-01", "2025-02", "2025-03", "2025-04"]
    for m in window:
        tx(conn, f"{m}-01", "PAYROLL DEPOSIT POLYAI", 5000, sign=1)
        tx(conn, f"{m}-07", "NETFLIX.COM", 20.99)
    finish(conn, window)
    f = insights.forecast(conn)
    assert any("months back" in w for w in f.warnings)


def test_a_sporadic_merchant_is_not_a_cancelled_subscription(tmp_path):
    """Charging in four of eight months is a restaurant you like, not a bill."""
    conn = setup(tmp_path)
    steady(conn, "NETFLIX.COM", 20.99)
    for m in ["2026-01", "2026-03", "2026-05"]:
        tx(conn, f"{m}-14", "PAI NORTHERN THAI", 48.0)
    finish(conn)
    found = kinds(insights.all_insights(conn))
    assert ("recurring_stopped", "PAI NORTHERN THAI") not in found


def test_a_sporadic_merchant_is_not_a_new_subscription(tmp_path):
    conn = setup(tmp_path)
    steady(conn, "NETFLIX.COM", 20.99)
    for m in ["2026-04", "2026-06"]:
        tx(conn, f"{m}-14", "PAI NORTHERN THAI", 48.0)
    finish(conn)
    found = kinds(insights.all_insights(conn))
    assert ("recurring_started", "PAI NORTHERN THAI") not in found


def test_a_genuinely_consecutive_run_that_stops_is_still_found(tmp_path):
    conn = setup(tmp_path)
    steady(conn, "NETFLIX.COM", 20.99)
    steady(conn, "CRAVE TV", 19.99, MONTHS[:-1], day="22")
    finish(conn)
    assert ("recurring_stopped", "CRAVE TV") in kinds(insights.all_insights(conn))


# ------------------------------------------------- statement coverage --------

def test_a_half_imported_month_does_not_cancel_every_subscription(tmp_path):
    """The bug this window exists for.

    On eight months of real statements, one account reaching only the 27th of
    the newest month produced ten findings announcing that Netflix, the phone
    bill, the internet and the gym had all stopped charging. They had not; the
    statements had.
    """
    conn = setup(tmp_path)
    import calendar
    for m in MONTHS:
        last = calendar.monthrange(int(m[:4]), int(m[5:7]))[1]
        tx(conn, f"{m}-05", "NETFLIX.COM", 20.99)
        tx(conn, f"{m}-06", "FIDO MOBILE", 45.20)
        tx(conn, f"{m}-{last:02d}", "TTC FARE", 3.35)
    # A second account whose statements stop mid-month, as a real one does.
    db.get_or_create_account(conn, "Chequing", "simplii", "chequing")
    for m in MONTHS[:-1]:
        conn.execute(
            "INSERT INTO transactions(account_id,date,description,raw_description,amount,fingerprint)"
            " VALUES (2,?,?,?,?,?)", (f"{m}-15", "PAYROLL", "PAYROLL", 4000.0, f"c{m}"))
    conn.execute(
        "INSERT INTO transactions(account_id,date,description,raw_description,amount,fingerprint)"
        " VALUES (2,?,?,?,?,?)", (f"{MONTHS[-1]}-12", "PAYROLL", "PAYROLL", 4000.0, "clast"))
    conn.commit()
    categorize_all(conn)
    coverage.match_internal_transfers(conn)

    found = kinds(insights.all_insights(conn))
    assert ("recurring_stopped", "NETFLIX.COM") not in found
    assert ("recurring_stopped", "FIDO MOBILE") not in found


def test_the_lagging_account_is_named_rather_than_left_a_mystery(tmp_path):
    conn = setup(tmp_path)
    import calendar
    for m in MONTHS:
        last = calendar.monthrange(int(m[:4]), int(m[5:7]))[1]
        tx(conn, f"{m}-{last:02d}", "NETFLIX.COM", 20.99)
    db.get_or_create_account(conn, "Simplii Chequing", "simplii", "chequing")
    for m in MONTHS:
        conn.execute(
            "INSERT INTO transactions(account_id,date,description,raw_description,amount,fingerprint)"
            " VALUES (2,?,?,?,?,?)", (f"{m}-10", "PAYROLL", "PAYROLL", 4000.0, f"c{m}"))
    conn.commit()
    categorize_all(conn)
    note = insights.analytics.coverage_gap(conn)
    assert note and "Simplii Chequing" in note


def test_the_two_windows_differ_only_when_the_tail_is_missing(tmp_path):
    conn = setup(tmp_path)
    import calendar
    for m in MONTHS:
        last = calendar.monthrange(int(m[:4]), int(m[5:7]))[1]
        tx(conn, f"{m}-{last:02d}", "NETFLIX.COM", 20.99)
    categorize_all(conn)
    a = insights.analytics
    assert a.complete_months(conn, a.STRICT_TOLERANCE_DAYS) == \
           a.complete_months(conn, a.SETTLED_TOLERANCE_DAYS)
