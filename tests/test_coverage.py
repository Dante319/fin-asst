"""Coverage, transfer matching, and the remittance ledger."""
import pytest

from finasst import coverage, db, remittance


def setup(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    return conn


def add(conn, account, date, desc, amount, category=None):
    acc = db.get_or_create_account(conn, account, "generic")
    cur = conn.execute(
        "INSERT INTO transactions(account_id,date,description,raw_description,amount,category,fingerprint)"
        " VALUES (?,?,?,?,?,?,?)",
        (acc, date, desc, desc, amount, category, f"{account}-{date}-{desc}-{amount}"),
    )
    conn.commit()
    return cur.lastrowid


def test_an_outflow_and_its_matching_inflow_become_one_internal_transfer(tmp_path):
    conn = setup(tmp_path)
    add(conn, "EQ", "2026-03-01", "Transfer to CIBC", -2500.0, "Transfer")
    add(conn, "CIBC", "2026-03-02", "Transfer from EQ", 2500.0, "Transfer")
    result = coverage.match_internal_transfers(conn)
    assert result.matched_pairs == 1 and result.matched_amount == 2500.0
    assert coverage.coverage(conn).unmatched_outflow == 0.0


def test_an_unmatched_transfer_is_reported_as_a_gap(tmp_path):
    """The receiving account has not been imported, so the money vanishes."""
    conn = setup(tmp_path)
    add(conn, "EQ", "2026-03-01", "Transfer to CIBC", -2500.0, "Transfer")
    coverage.match_internal_transfers(conn)
    cov = coverage.coverage(conn)
    assert cov.unmatched_outflow == 2500.0
    assert "cannot see" in cov.verdict()


def test_matching_never_guesses_between_two_candidates(tmp_path):
    """Two identical inflows are ambiguous; a wrong pairing would hide money."""
    conn = setup(tmp_path)
    add(conn, "EQ", "2026-03-01", "Transfer out", -500.0, "Transfer")
    add(conn, "CIBC", "2026-03-01", "Transfer in", 500.0, "Transfer")
    add(conn, "Other", "2026-03-02", "Transfer in", 500.0, "Transfer")
    assert coverage.match_internal_transfers(conn).matched_pairs == 0


def test_matching_ignores_movements_inside_one_account(tmp_path):
    conn = setup(tmp_path)
    add(conn, "EQ", "2026-03-01", "Out", -100.0, "Transfer")
    add(conn, "EQ", "2026-03-01", "In", 100.0, "Transfer")
    assert coverage.match_internal_transfers(conn).matched_pairs == 0


def test_matching_respects_the_date_window(tmp_path):
    conn = setup(tmp_path)
    add(conn, "EQ", "2026-03-01", "Out", -100.0, "Transfer")
    add(conn, "CIBC", "2026-04-20", "In", 100.0, "Transfer")
    assert coverage.match_internal_transfers(conn).matched_pairs == 0


def test_gap_ratio_is_measured_against_all_money_out(tmp_path):
    conn = setup(tmp_path)
    add(conn, "EQ", "2026-03-01", "Groceries", -1000.0, "Groceries")
    add(conn, "EQ", "2026-03-02", "Transfer out", -1000.0, "Transfer")
    coverage.match_internal_transfers(conn)
    assert coverage.coverage(conn).gap_ratio == pytest.approx(0.5)


def test_an_ended_income_stream_invalidates_the_surplus_baseline(tmp_path):
    conn = setup(tmp_path)
    for d in ("2026-01-07", "2026-02-07", "2026-03-07"):
        add(conn, "EQ", d, "Direct deposit from OldCo", 2000.0, "Income")
    add(conn, "EQ", "2026-07-30", "Direct deposit from NewCo", 500.0, "Income")
    warning = coverage.income_regime_warning(conn)
    assert warning and "OldCo" in warning and "NewCo" in warning


def test_no_warning_when_income_is_stable(tmp_path):
    conn = setup(tmp_path)
    for d in ("2026-05-07", "2026-06-07", "2026-07-07"):
        add(conn, "EQ", d, "Direct deposit from SameCo", 2000.0, "Income")
    assert coverage.income_regime_warning(conn) is None


# ---------------------------------------------------------------------------
# Remittance ledger
# ---------------------------------------------------------------------------

def test_ledger_is_built_from_categorised_transactions(tmp_path):
    conn = setup(tmp_path)
    add(conn, "EQ", "2026-01-08", "International Transfer", -466.98, "Remittance")
    add(conn, "EQ", "2026-01-09", "Groceries", -50.0, "Groceries")
    assert remittance.sync_from_transactions(conn) == 1
    assert remittance.sync_from_transactions(conn) == 0, "must not duplicate on re-run"
    assert remittance.report(conn).total_sent == 466.98


def test_effective_rate_needs_both_sides(tmp_path):
    conn = setup(tmp_path)
    tx = add(conn, "EQ", "2026-01-08", "International Transfer", -500.0, "Remittance")
    remittance.sync_from_transactions(conn)
    rep = remittance.report(conn)
    assert rep.transfers[0].effective_rate is None, "no received amount, no rate"
    assert "none have a received amount" in rep.coverage_note()

    remittance.set_received(conn, tx, received=30000.0, provider="EQ Bank")
    rep = remittance.report(conn)
    assert rep.transfers[0].effective_rate == pytest.approx(60.0)


def test_spread_against_the_reference_rate_is_priced_in_cad(tmp_path):
    """A transfer with no stated fee still costs you the spread."""
    conn = setup(tmp_path)
    tx = add(conn, "EQ", "2026-01-08", "International Transfer", -1000.0, "Remittance")
    remittance.sync_from_transactions(conn)
    remittance.set_received(conn, tx, received=60000.0)   # 60.00 INR/CAD
    conn.execute("INSERT INTO fx_rates(date,base,quote,rate) VALUES ('2026-01-08','CAD','INR',61.2)")
    conn.commit()

    t = remittance.report(conn).transfers[0]
    assert t.reference_rate == 61.2
    assert t.spread_pct == pytest.approx(1.96, abs=0.01)
    assert t.cost_cad == pytest.approx(19.6, abs=0.1)


def test_blended_rate_weights_by_amount_sent(tmp_path):
    conn = setup(tmp_path)
    a = add(conn, "EQ", "2026-01-08", "International Transfer", -100.0, "Remittance")
    b = add(conn, "EQ", "2026-02-08", "International Transfer", -900.0, "Remittance")
    remittance.sync_from_transactions(conn)
    remittance.set_received(conn, a, received=5000.0)     # 50/CAD on a small transfer
    remittance.set_received(conn, b, received=54000.0)    # 60/CAD on a large one
    rep = remittance.report(conn)
    assert rep.blended_rate == pytest.approx(59.0), "weighted by CAD sent, not a plain average"


def test_rate_lookup_falls_back_to_the_most_recent_published_rate(tmp_path):
    """Markets close at weekends; a Sunday transfer uses Friday's rate."""
    conn = setup(tmp_path)
    conn.execute("INSERT INTO fx_rates(date,base,quote,rate) VALUES ('2026-01-02','CAD','INR',61.0)")
    conn.commit()
    assert remittance.cached_rate(conn, "2026-01-04") == 61.0
    assert remittance.cached_rate(conn, "2026-01-01") is None


def test_migration_adds_the_peer_column_to_an_older_database(tmp_path):
    """Upgrading must not require rebuilding a database that already has data."""
    conn = db.connect(tmp_path / "old.db")
    conn.executescript("""
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT UNIQUE, issuer TEXT,
                               kind TEXT, currency TEXT, created_at TEXT);
        CREATE TABLE transactions (id INTEGER PRIMARY KEY, account_id INTEGER, date TEXT,
                                   description TEXT, raw_description TEXT, amount REAL,
                                   currency TEXT, category TEXT, category_source TEXT,
                                   fingerprint TEXT UNIQUE, source_file TEXT, imported_at TEXT);
    """)
    conn.commit()
    db.init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    assert "transfer_peer_id" in cols


# --------------------------------------------------------- regression tests --

def test_ambiguity_is_checked_from_both_sides(tmp_path):
    """Two identical outflows and one deposit must produce no pair at all.

    One of those outflows is a real expense. Pairing the deposit with whichever
    row happened to have the lower id removed a genuine expense from the
    coverage gap -- the exact failure this module exists to prevent.
    """
    conn = setup(tmp_path)
    add(conn, "Chequing", "2026-05-04", "MOVE MONEY", -500, "Transfer")
    add(conn, "Chequing", "2026-05-04", "RENT CHEQUE", -500, "Housing")
    add(conn, "Savings", "2026-05-04", "DEPOSIT", 500, "Transfer")
    assert coverage.match_internal_transfers(conn).matched_pairs == 0


def test_a_one_cent_difference_still_matches(tmp_path):
    """MATCH_TOLERANCE existed but was never applied to anything."""
    conn = setup(tmp_path)
    add(conn, "Chequing", "2026-06-01", "TRANSFER OUT", -500.00, "Transfer")
    add(conn, "Card", "2026-06-02", "TRANSFER IN", 499.99, "Transfer")
    assert coverage.match_internal_transfers(conn).matched_pairs == 1


def test_savings_outflows_count_towards_the_gap(tmp_path):
    """Money into an account with no statements here is money we cannot see.

    It used to appear in neither the numerator nor the denominator, so a $5,000
    TFSA contribution simply vanished from the headline percentage.
    """
    conn = setup(tmp_path)
    add(conn, "Chequing", "2026-06-01", "LOBLAWS", -1000, "Groceries")
    add(conn, "Chequing", "2026-06-02", "WEALTHSIMPLE TFSA", -5000, "Savings & Investments")
    add(conn, "Chequing", "2026-06-03", "E-TRANSFER TO MOM", -2000, "Transfer")
    coverage.match_internal_transfers(conn)
    cov = coverage.coverage(conn)
    assert cov.total_outflow == 8000
    assert cov.invisible == 7000
    assert cov.gap_ratio == pytest.approx(0.875)
    assert "savings or investments" in cov.verdict()
