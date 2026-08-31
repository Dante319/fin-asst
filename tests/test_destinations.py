"""Resolving the coverage gap by hand, without pretending it was verified."""
import pytest

from finasst import coverage, db, destinations


def setup(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    return conn


def add(conn, account, when, desc, amount, category=None, source="rule"):
    acc = db.get_or_create_account(conn, account, "generic")
    conn.execute(
        "INSERT INTO transactions(account_id,date,description,raw_description,amount,"
        "category,category_source,fingerprint) VALUES (?,?,?,?,?,?,?,?)",
        (acc, when, desc, desc, amount, category, source, f"{account}-{when}-{desc}-{amount}"),
    )
    conn.commit()


def gap(conn):
    coverage.match_internal_transfers(conn)
    destinations.apply_all(conn)
    return coverage.coverage(conn)


# ------------------------------------------------------------ the basic split

def test_an_undeclared_transfer_is_unexplained(tmp_path):
    conn = setup(tmp_path)
    add(conn, "Chequing", "2026-07-02", "e-transfer to cibc", -900, "Transfer")
    add(conn, "Chequing", "2026-07-05", "groceries", -100, "Groceries")
    cov = gap(conn)
    assert cov.invisible == 900.0
    assert cov.unexplained == 900.0
    assert cov.declared_total == 0.0


def test_declaring_your_own_account_explains_it_without_claiming_it_was_seen(tmp_path):
    """The gap total must NOT shrink -- the money is still invisible. What
    changes is that it is no longer unexplained."""
    conn = setup(tmp_path)
    add(conn, "Chequing", "2026-07-02", "e-transfer to cibc", -900, "Transfer")
    destinations.add(conn, destinations.Destination(
        pattern="cibc", label="CIBC chequing", kind="own_account"))
    cov = gap(conn)
    assert cov.invisible == 900.0            # still not visible
    assert cov.unexplained == 0.0            # but no longer a mystery
    assert cov.declared_still_yours == 900.0
    assert "still yours" in cov.verdict()
    assert "your word, not a matched statement" in cov.verdict()


def test_money_that_genuinely_left_becomes_spending(tmp_path):
    """Money sent to family is not an internal movement, and leaving it in the
    transfer bucket understates spending by exactly that much."""
    conn = setup(tmp_path)
    add(conn, "Chequing", "2026-07-02", "e-transfer to amma", -1200, "Transfer")
    destinations.add(conn, destinations.Destination(
        pattern="amma", label="Family support", kind="external", category="Remittance"))
    cov = gap(conn)
    assert cov.invisible == 0.0
    assert cov.visible_spend == 1200.0
    row = conn.execute("SELECT category, category_source FROM transactions").fetchone()
    assert row["category"] == "Remittance"
    assert row["category_source"] == "destination"


def test_external_without_a_category_is_refused(tmp_path):
    """Explaining the outflow while still hiding the spending is the worst of
    both worlds, so the app will not accept it."""
    conn = setup(tmp_path)
    with pytest.raises(ValueError, match="needs a category"):
        destinations.add(conn, destinations.Destination(
            pattern="amma", label="Family support", kind="external"))


def test_debt_explains_the_outflow_and_says_what_it_does_not_explain(tmp_path):
    conn = setup(tmp_path)
    add(conn, "Chequing", "2026-07-02", "payment to rbc visa", -2000, "Transfer")
    destinations.add(conn, destinations.Destination(
        pattern="rbc visa", label="RBC Visa", kind="debt"))
    cov = gap(conn)
    assert cov.declared_debt == 2000.0
    assert cov.unexplained == 0.0
    assert "spending behind it is not" in cov.verdict()


# ------------------------------------------------------------------- guardrails

def test_declaring_an_account_the_app_imports_raises_rather_than_hides(tmp_path):
    """If the app HAS those statements, a declaration papers over a matching
    failure. It must say so instead."""
    conn = setup(tmp_path)
    add(conn, "Simplii Chequing", "2026-07-01", "opening", 10, "Income")
    add(conn, "Amex", "2026-07-02", "transfer to simplii", -500, "Transfer")
    acc = conn.execute("SELECT id FROM accounts WHERE name = 'Simplii Chequing'").fetchone()["id"]
    destinations.add(conn, destinations.Destination(
        pattern="transfer to simplii", label="Simplii chequing",
        kind="own_account", account_id=acc))
    res = destinations.resolution(conn)
    assert res.warnings and "HAS statements for that account" in res.warnings[0]


def test_a_manual_category_outranks_a_declaration(tmp_path):
    conn = setup(tmp_path)
    add(conn, "Chequing", "2026-07-02", "e-transfer to amma", -1200, "Gifts", source="manual")
    destinations.add(conn, destinations.Destination(
        pattern="amma", label="Family support", kind="external", category="Remittance"))
    destinations.apply_all(conn)
    row = conn.execute("SELECT category, category_source FROM transactions").fetchone()
    assert row["category"] == "Gifts" and row["category_source"] == "manual"


def test_deleting_a_destination_undoes_what_it_filed(tmp_path):
    """A verdict must not outlive the declaration that justified it."""
    conn = setup(tmp_path)
    add(conn, "Chequing", "2026-07-02", "e-transfer to amma", -1200, "Transfer")
    dest_id = destinations.add(conn, destinations.Destination(
        pattern="amma", label="Family support", kind="external", category="Remittance"))
    destinations.apply_all(conn)
    assert gap(conn).visible_spend == 1200.0

    destinations.delete(conn, dest_id)
    row = conn.execute("SELECT category, category_source FROM transactions").fetchone()
    assert row["category"] == "Transfer"
    cov = gap(conn)
    assert cov.visible_spend == 0.0
    assert cov.unexplained == 1200.0


def test_a_matched_transfer_is_never_touched_by_a_declaration(tmp_path):
    """The matcher's verified pair outranks a text declaration, or a careless
    pattern could turn a real internal movement back into spending."""
    conn = setup(tmp_path)
    add(conn, "Amex", "2026-07-02", "transfer to simplii", -500, "Transfer")
    add(conn, "Simplii", "2026-07-03", "transfer from amex", 500, "Transfer")
    destinations.add(conn, destinations.Destination(
        pattern="transfer to simplii", label="Wrongly declared",
        kind="external", category="Shopping"))
    cov = gap(conn)
    assert cov.matched_internal == 500.0
    assert cov.visible_spend == 0.0
    assert destinations.resolution(conn).declared == []


def test_the_window_applies_to_declarations_too(tmp_path):
    """A windowed gap beside an all-time declaration would show more explained
    than there was gap."""
    conn = setup(tmp_path)
    add(conn, "Chequing", "2026-01-05", "e-transfer to cibc", -400, "Transfer")
    add(conn, "Chequing", "2026-07-05", "e-transfer to cibc", -600, "Transfer")
    destinations.add(conn, destinations.Destination(
        pattern="cibc", label="CIBC chequing", kind="own_account"))
    cov = coverage.coverage(conn, since="2026-07-01")
    assert cov.invisible == 600.0
    assert cov.declared_still_yours == 600.0
    assert cov.unexplained == 0.0
