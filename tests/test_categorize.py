from finasst import db
from finasst.categorize import categorize_all, match_category, merchant_key, seed_rules, set_manual_category


def setup_conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    seed_rules(conn)
    return conn


def add_tx(conn, desc, amount=-10.0, d="2026-07-01"):
    account_id = db.get_or_create_account(conn, "Test", "amex")
    cur = conn.execute(
        "INSERT INTO transactions(account_id, date, description, raw_description, amount, fingerprint) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, d, desc, desc, amount, f"fp-{desc}-{amount}-{d}"),
    )
    conn.commit()
    return cur.lastrowid


def test_uber_eats_beats_uber(tmp_path):
    conn = setup_conn(tmp_path)
    add_tx(conn, "UBER EATS TORONTO ON")
    add_tx(conn, "UBER *TRIP TORONTO ON")
    categorize_all(conn)
    rows = {r["description"]: r["category"] for r in conn.execute("SELECT description, category FROM transactions")}
    assert rows["UBER EATS TORONTO ON"] == "Eats & Drinks"
    assert rows["UBER *TRIP TORONTO ON"] == "Transport"


def test_card_payment_is_a_transfer_not_income(tmp_path):
    conn = setup_conn(tmp_path)
    add_tx(conn, "PAYMENT RECEIVED - THANK YOU", amount=500.0)
    categorize_all(conn)
    row = conn.execute("SELECT category FROM transactions").fetchone()
    assert row["category"] == "Transfer"


def test_remittance_is_its_own_category(tmp_path):
    conn = setup_conn(tmp_path)
    add_tx(conn, "WISE PAYMENTS CANADA INR", amount=-700)
    categorize_all(conn)
    assert conn.execute("SELECT category FROM transactions").fetchone()["category"] == "Remittance"


def test_manual_correction_teaches_a_rule_and_back_applies(tmp_path):
    # A merchant no seed rule matches, so the test exercises teaching rather
    # than accidentally passing because a rule already covered it.
    conn = setup_conn(tmp_path)
    a = add_tx(conn, "ZORBLAX KITCHEN TORONTO", d="2026-07-01")
    b = add_tx(conn, "ZORBLAX KITCHEN TORONTO", d="2026-07-15")
    categorize_all(conn)
    assert conn.execute("SELECT category FROM transactions WHERE id = ?", (a,)).fetchone()["category"] is None

    learned = set_manual_category(conn, a, "Eats & Drinks")
    assert learned == "zorblax kitchen"
    assert conn.execute("SELECT category FROM transactions WHERE id = ?", (b,)).fetchone()["category"] == "Eats & Drinks"


def test_manual_categories_survive_a_recategorize(tmp_path):
    conn = setup_conn(tmp_path)
    tx = add_tx(conn, "LOBLAWS #1032 TORONTO ON")
    categorize_all(conn)
    set_manual_category(conn, tx, "Other", teach=False)
    categorize_all(conn, recategorize=True)
    row = conn.execute("SELECT category, category_source FROM transactions WHERE id = ?", (tx,)).fetchone()
    assert row["category"] == "Other" and row["category_source"] == "manual"


def test_merchant_key_takes_the_stable_leading_words():
    assert merchant_key("ZORBLAX KITCHEN TORONTO") == "zorblax kitchen"
    assert merchant_key("STARBUCKS #4471") == "starbucks"
