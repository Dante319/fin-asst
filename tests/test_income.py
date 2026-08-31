"""Income regimes: the surplus must not average across a change of job."""
from datetime import date

import pytest

from finasst import analytics, db, income


def setup(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    return conn


def add(conn, when, desc, amount, category=None, account="Chequing"):
    acc = db.get_or_create_account(conn, account, "generic")
    conn.execute(
        "INSERT INTO transactions(account_id,date,description,raw_description,amount,category,fingerprint)"
        " VALUES (?,?,?,?,?,?,?)",
        (acc, when, desc, desc, amount, category, f"{account}-{when}-{desc}-{amount}"),
    )
    conn.commit()


def month(conn, ym, pay, spend, pay_desc="direct deposit"):
    add(conn, f"{ym}-05", pay_desc, pay, "Income")
    add(conn, f"{ym}-12", "groceries", -spend, "Groceries")


def freeze(monkeypatch, when):
    """Pin "today" for both modules that ask.

    datetime.date is immutable, so the class is replaced rather than patched.
    Every projection here depends on which months count as complete, and a
    test whose answer changes with the calendar is not a test."""
    class Frozen(date):
        @classmethod
        def today(cls):
            return when
    monkeypatch.setattr(analytics, "date", Frozen)
    monkeypatch.setattr(income, "date", Frozen)


# ---------------------------------------------------------------- arithmetic

def test_biweekly_is_twenty_six_cheques_not_twenty_four():
    """The bug this constant exists to prevent: 4300 every two weeks is
    9316.67 a month, not 8600. Treating it as twice a month loses two pay
    cheques a year -- about 8% of income -- and every projection inherits it."""
    assert income.monthly_equivalent(4300, "biweekly") == 9316.67
    assert income.monthly_equivalent(4300, "semimonthly") == 8600.0
    assert income.monthly_equivalent(4300, "biweekly") > income.monthly_equivalent(4300, "semimonthly")


def test_frequency_aliases_and_rejection():
    assert income.normalise_frequency("Every two weeks") == "biweekly"
    assert income.normalise_frequency("fortnightly") == "biweekly"
    assert income.normalise_frequency("YEARLY") == "annual"
    with pytest.raises(ValueError):
        income.normalise_frequency("whenever they feel like it")


def test_a_regime_starting_mid_month_does_not_claim_that_month():
    """July's income is part old job, part new. Counting it would drag the new
    baseline back towards the old one."""
    r = income.Regime("New job", date(2026, 7, 10), 4300, "biweekly")
    assert r.first_whole_month == "2026-08"
    assert income.Regime("X", date(2026, 7, 1), 100, "monthly").first_whole_month == "2026-07"
    assert income.Regime("X", date(2026, 12, 15), 100, "monthly").first_whole_month == "2027-01"


def test_last_whole_month_only_counts_a_month_run_to_its_end():
    r = income.Regime("Old", date(2025, 1, 1), 3000, "monthly", ended_on=date(2026, 6, 30))
    assert r.last_whole_month == "2026-06"
    r2 = income.Regime("Old", date(2025, 1, 1), 3000, "monthly", ended_on=date(2026, 6, 14))
    assert r2.last_whole_month == "2026-05"


# ------------------------------------------------------------------- storage

def test_recording_a_new_regime_closes_the_one_it_replaces(tmp_path):
    conn = setup(tmp_path)
    income.add_regime(conn, income.Regime("Old job", date(2025, 3, 3), 2600, "biweekly"))
    income.add_regime(conn, income.Regime("New job", date(2026, 7, 10), 4300, "biweekly"))
    live = income.all_current(conn, as_of=date(2026, 8, 20))
    assert [r.name for r in live] == ["New job"]
    assert income.declared_monthly(conn, as_of=date(2026, 8, 20)) == 9316.67


def test_two_jobs_at_once_are_added_when_you_say_so(tmp_path):
    conn = setup(tmp_path)
    income.add_regime(conn, income.Regime("Day job", date(2026, 1, 1), 4000, "monthly"))
    income.add_regime(conn, income.Regime("Evenings", date(2026, 7, 1), 500, "monthly"),
                      close_previous=False)
    assert income.declared_monthly(conn, as_of=date(2026, 8, 1)) == 4500.0


# ------------------------------------------------------------------- surplus

def test_surplus_ignores_months_before_the_new_job(tmp_path, monkeypatch):
    """The whole point. Four months at the old pay, three whole months at the
    new one: the baseline must describe the new pay alone."""
    conn = setup(tmp_path)
    for ym in ("2026-03", "2026-04", "2026-05", "2026-06"):
        month(conn, ym, 5000, 3000)
    for ym in ("2026-07", "2026-08", "2026-09"):
        month(conn, ym, 9300, 3000)
    income.add_regime(conn, income.Regime("New job", date(2026, 7, 1), 4300, "biweekly"))

    freeze(monkeypatch, date(2026, 10, 15))
    avg = analytics.average_surplus(conn, lookback=6)
    assert avg["method"] == "observed"
    assert avg["basis"] == ["2026-07", "2026-08", "2026-09"]
    assert avg["surplus"] == pytest.approx(6300.0)
    assert "2026-07" in avg["warning"]


def test_without_a_regime_nothing_changes(tmp_path, monkeypatch):
    """Regression guard: declaring nothing must leave the old behaviour intact."""
    conn = setup(tmp_path)
    for ym in ("2026-05", "2026-06", "2026-07"):
        month(conn, ym, 5000, 3000)
    freeze(monkeypatch, date(2026, 8, 15))
    avg = analytics.average_surplus(conn, lookback=3)
    assert avg["method"] == "observed"
    assert avg["basis"] == ["2026-05", "2026-06", "2026-07"]
    assert avg["surplus"] == pytest.approx(2000.0)


def test_a_brand_new_job_uses_declared_pay_and_says_so(tmp_path, monkeypatch):
    """One whole month under the new regime is a sample, not an average. The
    baseline switches to declared income minus observed spending, and the
    method has to be labelled so no one reads it as measured."""
    conn = setup(tmp_path)
    for ym in ("2026-04", "2026-05", "2026-06"):
        month(conn, ym, 5000, 3000)
    month(conn, "2026-07", 9300, 3000)
    income.add_regime(conn, income.Regime("New job", date(2026, 7, 1), 4300, "biweekly"))

    freeze(monkeypatch, date(2026, 8, 15))
    avg = analytics.average_surplus(conn, lookback=3)
    assert avg["method"] == "declared"
    assert avg["regime"] == "New job"
    # 9316.67 declared, less the 3000/month actually spent in May-July.
    assert avg["surplus"] == pytest.approx(6316.67)
    assert "declared pay" in avg["basis_label"]
    assert "too few to average" in avg["warning"]


def test_the_declared_baseline_still_reports_a_dirty_sample(tmp_path, monkeypatch):
    """Switching method must not silence the data-quality warnings: the
    spending half is still measured, and can still be half-categorised."""
    conn = setup(tmp_path)
    month(conn, "2026-06", 5000, 3000)
    add(conn, "2026-06-20", "mystery card charge", -900)      # uncategorised spend
    month(conn, "2026-07", 9300, 3000)
    income.add_regime(conn, income.Regime("New job", date(2026, 7, 15), 4300, "biweekly"))
    freeze(monkeypatch, date(2026, 8, 10))
    avg = analytics.average_surplus(conn, lookback=2)
    assert avg["method"] == "declared"
    assert "uncategorised" in avg["warning"]


def test_drift_reports_the_gap_without_picking_a_winner(tmp_path):
    """Declared 9316.67/month, statements show 4650: a pay deposit is missing
    or the declaration is wrong. The app must say so, not choose."""
    conn = setup(tmp_path)
    month(conn, "2026-07", 4650, 3000)
    month(conn, "2026-08", 4650, 3000)
    r = income.Regime("New job", date(2026, 7, 1), 4300, "biweekly")
    income.add_regime(conn, r)
    note = income.drift(conn, income.current_regime(conn, as_of=date(2026, 8, 20)),
                        analytics.monthly_totals(conn))
    assert note and "less" in note
    assert "9,317" in note


def test_no_drift_when_the_statements_agree(tmp_path):
    conn = setup(tmp_path)
    month(conn, "2026-07", 9300, 3000)
    month(conn, "2026-08", 9350, 3000)
    income.add_regime(conn, income.Regime("New job", date(2026, 7, 1), 4300, "biweekly"))
    assert income.drift(conn, income.current_regime(conn, as_of=date(2026, 9, 5)),
                        analytics.monthly_totals(conn)) is None


def test_uncategorised_pay_is_named_as_the_likely_cause_of_a_gap(tmp_path):
    conn = setup(tmp_path)
    add(conn, "2026-07-05", "unknown deposit", 4650)   # not categorised as Income
    add(conn, "2026-07-19", "unknown deposit 2", 4650)
    add(conn, "2026-07-12", "groceries", -3000, "Groceries")
    income.add_regime(conn, income.Regime("New job", date(2026, 7, 1), 4300, "biweekly"))
    note = income.drift(conn, income.current_regime(conn, as_of=date(2026, 8, 20)),
                        analytics.monthly_totals(conn))
    assert note and "categorise it as Income" in note
