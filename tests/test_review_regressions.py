"""Regressions for ten defects an adversarial review found in the insights,
rules, model and web layers. Each test is the reviewer's scenario."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from finasst import coverage, db, insights, llm, rules
from finasst.categorize import categorize_all, seed_rules, set_manual_category
from finasst.web import app as webapp


def conn_at(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    seed_rules(conn)
    db.get_or_create_account(conn, "Amex", "amex")
    return conn


def tx(conn, day, desc, amount, sign=-1, i=[0]):
    i[0] += 1
    conn.execute(
        "INSERT INTO transactions(account_id,date,description,raw_description,amount,fingerprint)"
        " VALUES (1,?,?,?,?,?)", (day, desc, desc, sign * amount, f"r{i[0]}"))
    conn.commit()


def months_back(n):
    """The n complete months ending with last month, so tests never depend on
    where in the calendar they run."""
    today = date.today()
    total = today.year * 12 + today.month - 2
    out = []
    for k in range(n - 1, -1, -1):
        t = total - k
        out.append(f"{t // 12:04d}-{t % 12 + 1:02d}")
    return out


# ---- 1. stored XSS on /rules --------------------------------------------

def test_a_merchant_name_is_never_rendered_as_markup(tmp_path, monkeypatch):
    dbfile = tmp_path / "t.db"
    monkeypatch.setattr(webapp.db.config, "DB_PATH", dbfile)
    conn = db.connect(dbfile)
    db.init_db(conn)
    db.get_or_create_account(conn, "Amex", "amex")
    payload = "<img src=x onerror=alert(1)>"
    tx(conn, "2026-06-01", f"ZORBLAX {payload}", 40.0)
    conn.commit()
    rules.add_rule(conn, "zorblax", "Shopping")
    rules.recategorise(conn)
    conn.close()
    with TestClient(webapp.app) as client:
        body = client.get("/rules").text
    assert payload not in body
    assert "&lt;img" in body


# ---- 2. bulk categorise bypassed by a LIKE wildcard ----------------------

def test_a_percent_sign_is_a_search_not_a_way_past_the_bulk_guard(tmp_path, monkeypatch):
    dbfile = tmp_path / "t.db"
    monkeypatch.setattr(webapp.db.config, "DB_PATH", dbfile)
    conn = db.connect(dbfile)
    db.init_db(conn)
    db.get_or_create_account(conn, "Amex", "amex")
    for n in range(7):
        tx(conn, "2026-06-01", f"MERCHANT {n}", 10.0 + n)
    conn.execute("UPDATE transactions SET category='Groceries', category_source='rule'")
    conn.commit()
    conn.close()
    with TestClient(webapp.app) as client:
        client.post("/transactions/bulk", data={"category": "Travel", "q": "%"},
                    follow_redirects=False)
        after = client.get("/transactions.csv?category=Travel").text
    assert len(after.strip().splitlines()) == 1      # header only: nothing relabelled


# ---- 3. the month in progress is not "last month" -----------------------

def test_one_current_month_row_does_not_rewrite_every_finding(tmp_path):
    conn = conn_at(tmp_path)
    window = months_back(6)
    for m in window:
        tx(conn, f"{m}-05", "NETFLIX.COM", 20.99)
        for d in ("07", "14", "21", "28"):
            tx(conn, f"{m}-{d}", "LOBLAWS #1032", 61.0)
    tx(conn, f"{window[-1]}-28", "LOBLAWS #1032", 900.0)
    categorize_all(conn); coverage.match_internal_transfers(conn)
    before = {(f.kind, f.merchant) for f in insights.all_insights(conn)}
    assert ("unusual_charge", "LOBLAWS #1032") in before

    tx(conn, date.today().replace(day=1).isoformat(), "PAYROLL DEPOSIT", 4000, sign=1)
    categorize_all(conn); coverage.match_internal_transfers(conn)
    after = {(f.kind, f.merchant) for f in insights.all_insights(conn)}
    assert ("unusual_charge", "LOBLAWS #1032") in after
    assert ("recurring_stopped", "LOBLAWS #1032") not in after
    assert ("recurring_stopped", "NETFLIX.COM") not in after


# ---- 4 + 9. what "fixed" means, and a forecast range that contains reality --

def test_a_household_that_buys_groceries_does_not_have_zero_variable_spend(tmp_path):
    conn = conn_at(tmp_path)
    window = months_back(8)
    for n, m in enumerate(window):
        tx(conn, f"{m}-01", "PAYROLL DEPOSIT POLYAI", 4000, sign=1)
        tx(conn, f"{m}-02", "INTERNET BANKING E-TRANSFER RENT", 2000)
        tx(conn, f"{m}-05", "NETFLIX.COM", 20.99)
        for d in ("08", "15", "22"):
            tx(conn, f"{m}-{d}", "LOBLAWS #1032", 130 + n * 3)      # varies month to month
    categorize_all(conn); coverage.match_internal_transfers(conn)

    f = insights.forecast(conn)
    assert f.ok
    assert f.variable_typical > 300, f"groceries vanished into 'fixed': {f.basis}"
    truth = [m["surplus"] for m in insights.analytics.monthly_totals(conn)
             if m["month"] in window]
    midpoint = f.months[0]["surplus"]
    assert min(truth) - 1 <= midpoint <= max(truth) + 1, (midpoint, min(truth), max(truth))


def test_a_weekly_grocery_shop_is_not_a_committed_cost(tmp_path):
    """It can total a steady amount every month and still be discretionary."""
    conn = conn_at(tmp_path)
    window = months_back(6)
    for m in window:
        tx(conn, f"{m}-02", "INTERNET BANKING E-TRANSFER RENT", 2000)
        for d in ("08", "15", "22"):
            tx(conn, f"{m}-{d}", "LOBLAWS #1032", 140)
    categorize_all(conn); coverage.match_internal_transfers(conn)
    committed = insights.fixed_costs(conn)
    assert any("RENT" in f["merchant"] for f in committed.values())
    assert not any("LOBLAWS" in f["merchant"] for f in committed.values())


def test_a_committed_cost_is_one_that_charges_every_month(tmp_path):
    """A bi-monthly hydro bill is not committed monthly spending."""
    conn = conn_at(tmp_path)
    window = months_back(6)
    for m in window:
        tx(conn, f"{m}-02", "INTERNET BANKING E-TRANSFER RENT", 2000)
    for m in window[::2]:
        tx(conn, f"{m}-11", "TORONTO HYDRO ELEC", 200)
    categorize_all(conn); coverage.match_internal_transfers(conn)
    committed = insights.fixed_costs(conn)
    assert any("RENT" in f["merchant"] for f in committed.values())
    assert not any("HYDRO" in f["merchant"] for f in committed.values())


# ---- 6. a bi-monthly bill must not be annualised as monthly --------------

def test_a_bi_monthly_bill_is_not_reported_as_a_monthly_price_rise(tmp_path):
    conn = conn_at(tmp_path)
    window = months_back(9)
    for m in window:
        tx(conn, f"{m}-05", "NETFLIX.COM", 20.99)
    for m in window[::2]:
        tx(conn, f"{m}-11", "TORONTO HYDRO ELEC", 200 if m != window[-1] else 215)
    categorize_all(conn); coverage.match_internal_transfers(conn)
    found = {(f.kind, f.merchant) for f in insights.all_insights(conn)}
    assert ("price_change", "TORONTO HYDRO ELEC") not in found


# ---- 7. a rule that would hang the app ----------------------------------

@pytest.mark.parametrize("pattern", ["(a+)+$", "(x*)*y", r"(\d+)+z"])
def test_a_catastrophically_backtracking_regex_is_refused(tmp_path, pattern):
    conn = conn_at(tmp_path)
    with pytest.raises(rules.RuleError, match="repeat inside another"):
        rules.add_rule(conn, pattern, "Shopping", match_type="regex")


def test_a_normal_regex_is_still_allowed(tmp_path):
    conn = conn_at(tmp_path)
    assert rules.add_rule(conn, r"\bzorblax\b", "Shopping", match_type="regex")


# ---- 8. "files" counts what the rule decides ----------------------------

def test_a_rule_gets_no_credit_for_rows_you_corrected_yourself(tmp_path):
    conn = conn_at(tmp_path)
    for n in range(6):
        tx(conn, "2026-06-01", "AMAZON.CA*RT4YZ", 20.0 + n)
    categorize_all(conn)
    for row in conn.execute("SELECT id FROM transactions").fetchall():
        set_manual_category(conn, row["id"], "Groceries", teach=False)
    amazon = next(r for r in rules.inventory(conn) if r.pattern == "amazon")
    assert amazon.matches == 0


# ---- built-in rules cannot be hijacked by an add ------------------------

def test_adding_over_a_built_in_rule_is_refused(tmp_path):
    conn = conn_at(tmp_path)
    with pytest.raises(rules.RuleError, match="built-in rule"):
        rules.add_rule(conn, "netflix", "Groceries", match_type="contains")
    row = conn.execute("SELECT category, is_user FROM rules WHERE pattern='netflix'").fetchone()
    assert row["category"] == "Subscriptions" and row["is_user"] == 0


# ---- 5. the model layer's contract --------------------------------------

def test_a_shared_terminal_prefix_is_not_accepted_as_one_rule(tmp_path, monkeypatch):
    conn = conn_at(tmp_path)
    for name in ("MARCHE LEO 88231", "DR PATEL DENTISTRY", "SUNNYSIDE PHARMACY",
                 "TORONTO ANIMAL HOSP", "LITTLE ITALY WINE", "CORSO ITALIA CAFE",
                 "QUEEN ST HARDWARE"):
        for _ in range(3):
            tx(conn, "2026-06-01", f"POS PURCHASE {name}", 30.0)
    categorize_all(conn)
    monkeypatch.setenv("FINASST_LLM_URL", "http://127.0.0.1:9/v1/chat/completions")
    monkeypatch.setattr(llm, "_call", lambda *a, **k:
                        '[{"merchant": "POS PURCHASE MARCHE LEO 88231", '
                        '"category": "Groceries", "confidence": "high"}]')
    llm.suggest_categories(conn)
    with pytest.raises(llm.LLMError, match="too broad"):
        llm.accept(conn, "pos purchase")
    # The dentist, vet, pharmacy and wine shop must not have become Groceries.
    # (CORSO ITALIA CAFE legitimately matches the built-in "cafe" rule.)
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category = 'Groceries'").fetchone()[0] == 0
    assert not [r for r in rules.inventory(conn) if r.is_user]


def test_every_distinct_name_behind_a_shared_key_is_shown_to_the_model(tmp_path, monkeypatch):
    conn = conn_at(tmp_path)
    for name in ("MARCHE LEO", "DR PATEL DENTISTRY"):
        tx(conn, "2026-06-01", f"POS PURCHASE {name}", 30.0)
    categorize_all(conn)
    monkeypatch.setenv("FINASST_LLM_URL", "http://127.0.0.1:9/v1/chat/completions")
    sent = {}

    def fake(cfg, messages, max_tokens):
        sent["text"] = messages[-1]["content"]
        return "[]"

    monkeypatch.setattr(llm, "_call", fake)
    llm.suggest_categories(conn)
    assert "MARCHE LEO" in sent["text"] and "DR PATEL DENTISTRY" in sent["text"]


def test_a_tidied_merchant_name_in_the_reply_is_still_matched(tmp_path, monkeypatch):
    conn = conn_at(tmp_path)
    tx(conn, "2026-06-01", "BLUE DOOR BAKESHOP #12", 14.0)
    categorize_all(conn)
    monkeypatch.setenv("FINASST_LLM_URL", "http://127.0.0.1:9/v1/chat/completions")
    monkeypatch.setattr(llm, "_call", lambda *a, **k:
                        '[{"merchant": "Blue Door Bakeshop", "category": "Eats & Drinks", '
                        '"confidence": "high"}]')
    made = llm.suggest_categories(conn)
    assert made[0].category == "Eats & Drinks"


def test_a_hostile_confidence_value_is_discarded(tmp_path, monkeypatch):
    conn = conn_at(tmp_path)
    tx(conn, "2026-06-01", "ZORBLAX KITCHEN", 14.0)
    categorize_all(conn)
    monkeypatch.setenv("FINASST_LLM_URL", "http://127.0.0.1:9/v1/chat/completions")
    monkeypatch.setattr(llm, "_call", lambda *a, **k:
                        '[{"merchant": "ZORBLAX KITCHEN", "category": "Travel", '
                        f'"confidence": "{"A" * 5000}"}}]')
    made = llm.suggest_categories(conn)
    assert made[0].confidence == "low"


# ---- 10. CSV formula injection ------------------------------------------

def test_a_merchant_name_cannot_become_a_spreadsheet_formula(tmp_path, monkeypatch):
    dbfile = tmp_path / "t.db"
    monkeypatch.setattr(webapp.db.config, "DB_PATH", dbfile)
    conn = db.connect(dbfile)
    db.init_db(conn)
    db.get_or_create_account(conn, "Amex", "amex")
    tx(conn, "2026-06-01", "=cmd|'/c calc'!A1", 13.0)
    conn.commit()
    conn.close()
    with TestClient(webapp.app) as client:
        body = client.get("/transactions.csv").text
    assert "=cmd" not in body.replace("'=cmd", "")
    assert "'=cmd" in body or '"\'=cmd' in body
