"""The rule manager: what a rule is actually doing, and what happens when you
add or remove one."""
import pytest

from finasst import db, rules
from finasst.categorize import categorize_all, seed_rules, set_manual_category


def setup(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    seed_rules(conn)
    db.get_or_create_account(conn, "Amex", "amex")
    return conn


def add(conn, desc, amount=-40.0, d="2026-07-01", i=[0]):
    i[0] += 1
    cur = conn.execute(
        "INSERT INTO transactions(account_id,date,description,raw_description,amount,fingerprint)"
        " VALUES (1,?,?,?,?,?)", (d, desc, desc, amount, f"fp{i[0]}"))
    conn.commit()
    return cur.lastrowid


def test_inventory_counts_the_rules_that_win_not_the_ones_that_merely_match(tmp_path):
    conn = setup(tmp_path)
    add(conn, "UBER EATS TORONTO")
    add(conn, "UBER EATS TORONTO", d="2026-07-02")
    categorize_all(conn)
    by_pattern = {r.pattern: r for r in rules.inventory(conn)}
    assert by_pattern["uber eats"].matches == 2
    # \buber\b matches the same text but ranks lower, so it decides nothing.
    assert by_pattern[r"\buber\b"].matches == 0


def test_explain_names_the_deciding_rule_and_the_ones_it_beat(tmp_path):
    conn = setup(tmp_path)
    result = rules.explain(conn, "UBER EATS TORONTO ON")
    assert result.category == "Eats & Drinks"
    assert result.winner.pattern == "uber eats"
    assert any(r.pattern == r"\buber\b" for r in result.also_matched)


def test_explain_says_so_when_nothing_matches(tmp_path):
    conn = setup(tmp_path)
    result = rules.explain(conn, "ZORBLAX HOLDINGS LLC")
    assert result.winner is None and result.category is None


def test_adding_a_rule_files_the_transactions_it_covers(tmp_path):
    conn = setup(tmp_path)
    a = add(conn, "ZORBLAX KITCHEN TORONTO")
    categorize_all(conn)
    assert conn.execute("SELECT category FROM transactions WHERE id=?", (a,)).fetchone()["category"] is None
    rules.add_rule(conn, "zorblax kitchen", "Eats & Drinks")
    rules.recategorise(conn)
    assert conn.execute("SELECT category FROM transactions WHERE id=?", (a,)).fetchone()["category"] == "Eats & Drinks"


def test_removing_a_rule_unfiles_what_only_it_covered(tmp_path):
    conn = setup(tmp_path)
    a = add(conn, "ZORBLAX KITCHEN TORONTO")
    rule_id = rules.add_rule(conn, "zorblax kitchen", "Eats & Drinks")
    rules.recategorise(conn)
    rules.remove_rule(conn, rule_id)
    rules.recategorise(conn)
    assert conn.execute("SELECT category FROM transactions WHERE id=?", (a,)).fetchone()["category"] is None


def test_a_manual_correction_survives_removing_the_rule_it_taught(tmp_path):
    """Deleting the rule learned from a correction must not undo the correction."""
    conn = setup(tmp_path)
    a = add(conn, "ZORBLAX KITCHEN TORONTO")
    categorize_all(conn)
    taught = set_manual_category(conn, a, "Eats & Drinks")
    rule = next(r for r in rules.inventory(conn) if r.pattern == taught.pattern)
    rules.remove_rule(conn, rule.id)
    rules.recategorise(conn)
    row = conn.execute("SELECT category, category_source FROM transactions WHERE id=?", (a,)).fetchone()
    assert row["category"] == "Eats & Drinks" and row["category_source"] == "manual"


def test_built_in_rules_cannot_be_deleted(tmp_path):
    conn = setup(tmp_path)
    builtin = next(r for r in rules.inventory(conn) if not r.is_user)
    with pytest.raises(rules.RuleError, match="built-in"):
        rules.remove_rule(conn, builtin.id)


@pytest.mark.parametrize("pattern,message", [("ab", "three characters"), ("", "three characters")])
def test_a_pattern_too_short_to_be_safe_is_refused(tmp_path, pattern, message):
    conn = setup(tmp_path)
    with pytest.raises(rules.RuleError, match=message):
        rules.add_rule(conn, pattern, "Groceries")


def test_a_broken_regex_is_refused_in_words_not_a_traceback(tmp_path):
    conn = setup(tmp_path)
    with pytest.raises(rules.RuleError, match="regular expression"):
        rules.add_rule(conn, "([unclosed", "Groceries", match_type="regex")


def test_an_unknown_category_is_refused(tmp_path):
    conn = setup(tmp_path)
    with pytest.raises(rules.RuleError, match="not one of the categories"):
        rules.add_rule(conn, "zorblax", "Snacks")
