from finasst import db
from finasst.categorize import (
    categorize_all, load_rules, match_category, merchant_key, seed_rules, set_manual_category,
)


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
    assert learned.pattern == "zorblax kitchen"
    assert learned.applied_to == 1
    assert learned.refused is None
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


# --------------------------------------------------------- category groups --

def test_every_spend_category_belongs_to_a_group():
    """A new category with no group would silently render neutral grey.

    Colour on the dashboard encodes the group, so an ungrouped category is a
    bar that looks like "we could not classify this" when in fact we could.
    """
    from finasst import config

    ungrouped = [
        c for c in config.CATEGORIES
        if c not in config.NON_SPEND_CATEGORIES and c not in config.CATEGORY_GROUPS
    ]
    assert ungrouped == [], f"no group for: {ungrouped}"


def test_groups_used_are_the_ones_declared_in_group_order():
    from finasst import config

    assert set(config.CATEGORY_GROUPS.values()) == set(config.GROUP_ORDER)


def test_non_spend_categories_are_not_grouped():
    """Income and transfers are not spending and must never colour a spend bar."""
    from finasst import config

    for category in config.NON_SPEND_CATEGORIES:
        assert config.group_of(category) == config.UNGROUPED


def test_group_of_handles_unknown_and_none():
    from finasst import config

    assert config.group_of(None) == config.UNGROUPED
    assert config.group_of("Something new") == config.UNGROUPED


# ------------------------------------------------- rule-matching regressions --

def test_seed_rules_do_not_catch_merchants_that_merely_contain_them(tmp_path):
    """Plain substring rules were filing the wrong merchants.

    METROLINX became Groceries via "metro", TIFFANY became Entertainment via
    "tiff", BENEFACTOR became Groceries via "factor". All of them now carry
    word boundaries.
    """
    conn = setup_conn(tmp_path)
    rules = load_rules(conn)

    def cat(text):
        hit = match_category(text, rules)
        return hit[0] if hit else None

    assert cat("METROLINX GO TRANSIT") == "Transport"
    assert cat("METRO #742 TORONTO") == "Groceries"
    assert cat("TIFFANY & CO TORONTO ON") != "Entertainment"
    assert cat("BENEFACTOR CLOTHING") != "Groceries"
    assert cat("SHELLEY'S FLOWERS") != "Transport"
    assert cat("BARBER SHOP QUEEN W") == "Personal Care"
    assert cat("AVIS RENT A CAR") == "Travel"
    # ...while the rules they replaced still do their job
    assert cat("SHELL 4471 TORONTO") == "Transport"
    assert cat("BAR RAVAL TORONTO") == "Eats & Drinks"


def test_a_taught_rule_matches_through_punctuation(tmp_path):
    """merchant_key strips punctuation; matching did not, so nothing matched."""
    conn = setup_conn(tmp_path)
    a = add_tx(conn, "ZORBLAX-KITCHEN 04512")
    b = add_tx(conn, "ZORBLAX KITCHEN GEORGETOWN", d="2026-07-20")
    categorize_all(conn)
    taught = set_manual_category(conn, a, "Eats & Drinks")
    assert taught.pattern == "zorblax kitchen"
    assert taught.applied_to == 1
    row = conn.execute("SELECT category FROM transactions WHERE id = ?", (b,)).fetchone()
    assert row["category"] == "Eats & Drinks"


def test_teaching_declines_a_key_that_would_shadow_another_category(tmp_path):
    """Correcting one Uber ride must not refile every Uber Eats order."""
    conn = setup_conn(tmp_path)
    ride = add_tx(conn, "UBER 9042")
    eats = add_tx(conn, "UBER EATS TORONTO", d="2026-07-09")
    categorize_all(conn)
    taught = set_manual_category(conn, ride, "Travel")
    assert taught.pattern is None
    assert taught.refused and "uber eats" in taught.refused
    assert conn.execute("SELECT category FROM transactions WHERE id = ?", (ride,)).fetchone()["category"] == "Travel"
    assert conn.execute("SELECT category FROM transactions WHERE id = ?", (eats,)).fetchone()["category"] == "Eats & Drinks"


def test_an_unexplained_inflow_is_not_income(tmp_path):
    """A friend repaying $4,000 is not a raise.

    Defaulting every unmatched inflow to Income also hid it from the
    "still uncategorised" warning, because the row was no longer NULL.
    """
    conn = setup_conn(tmp_path)
    tx = add_tx(conn, "MOM SENT BACK THE CAR MONEY", amount=4000.0)
    categorize_all(conn)
    row = conn.execute("SELECT category FROM transactions WHERE id = ?", (tx,)).fetchone()
    assert row["category"] is None


def test_retired_seed_rules_are_removed_from_an_existing_database(tmp_path):
    conn = setup_conn(tmp_path)
    conn.execute("INSERT INTO rules(pattern, match_type, category, priority, is_user)"
                 " VALUES ('metro', 'contains', 'Groceries', 60, 0)")
    conn.execute("INSERT INTO rules(pattern, match_type, category, priority, is_user)"
                 " VALUES ('my own rule', 'words', 'Travel', 10, 1)")
    conn.commit()
    seed_rules(conn)
    patterns = {r["pattern"] for r in conn.execute("SELECT pattern FROM rules")}
    assert "metro" not in patterns          # retired
    assert "my own rule" in patterns        # yours, never touched
