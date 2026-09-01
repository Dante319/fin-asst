"""The optional model layer, exercised without any model.

Everything here runs offline: the HTTP call is stubbed. What is being tested is
the contract around the model, which is the part that matters -- deduplication,
caching, and the rule that a suggestion is a proposal and never a write.
"""
import pytest

from finasst import db, llm, rules
from finasst.categorize import categorize_all, seed_rules


def setup(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    seed_rules(conn)
    db.get_or_create_account(conn, "Amex", "amex")
    return conn


def tx(conn, desc, n=1, i=[0]):
    for _ in range(n):
        i[0] += 1
        conn.execute(
            "INSERT INTO transactions(account_id,date,description,raw_description,amount,fingerprint)"
            " VALUES (1,'2026-06-01',?,?,-42.0,?)", (desc, desc, f"fp{i[0]}"))
    conn.commit()


def test_it_is_off_until_it_is_turned_on(tmp_path, monkeypatch):
    monkeypatch.delenv("FINASST_LLM_URL", raising=False)
    conn = setup(tmp_path)
    assert llm.config(conn).enabled is False
    with pytest.raises(llm.LLMError, match="No model is configured"):
        llm.suggest_categories(conn)


def test_merchants_are_deduplicated_before_anything_is_sent(tmp_path):
    """Two hundred transactions are usually twenty merchants. Send twenty."""
    conn = setup(tmp_path)
    tx(conn, "ZORBLAX KITCHEN TORONTO", n=40)
    tx(conn, "ZORBLAX KITCHEN GEORGETOWN", n=30)   # same merchant key
    tx(conn, "QUUX HOLDINGS LTD", n=12)
    categorize_all(conn)
    pending = llm.pending_merchants(conn)
    assert len(pending) == 2
    assert {p["key"] for p in pending} == {"zorblax kitchen", "quux holdings"}


def test_a_merchant_is_never_sent_twice(tmp_path, monkeypatch):
    conn = setup(tmp_path)
    tx(conn, "ZORBLAX KITCHEN TORONTO", n=3)
    categorize_all(conn)
    monkeypatch.setenv("FINASST_LLM_URL", "http://127.0.0.1:9/v1/chat/completions")

    calls = []

    def fake(cfg, messages, max_tokens):
        calls.append(messages)
        return '[{"merchant": "ZORBLAX KITCHEN TORONTO", "category": "Eats & Drinks", "confidence": "high"}]'

    monkeypatch.setattr(llm, "_call", fake)
    assert len(llm.suggest_categories(conn)) == 1
    assert len(calls) == 1
    # Second run: cached, so nothing is pending and no request is made.
    assert llm.suggest_categories(conn) == []
    assert len(calls) == 1


def test_a_rejected_merchant_is_never_sent_again(tmp_path, monkeypatch):
    conn = setup(tmp_path)
    tx(conn, "ZORBLAX KITCHEN TORONTO", n=2)
    categorize_all(conn)
    monkeypatch.setenv("FINASST_LLM_URL", "http://127.0.0.1:9/v1/chat/completions")
    monkeypatch.setattr(llm, "_call", lambda *a, **k:
                        '[{"merchant": "ZORBLAX KITCHEN TORONTO", "category": "Travel", "confidence": "low"}]')
    llm.suggest_categories(conn)
    llm.reject(conn, "zorblax kitchen")
    assert llm.pending_merchants(conn) == []


def test_a_suggestion_changes_nothing_until_it_is_accepted(tmp_path, monkeypatch):
    """The model proposes. It never writes a category onto a transaction."""
    conn = setup(tmp_path)
    tx(conn, "ZORBLAX KITCHEN TORONTO", n=3)
    categorize_all(conn)
    monkeypatch.setenv("FINASST_LLM_URL", "http://127.0.0.1:9/v1/chat/completions")
    monkeypatch.setattr(llm, "_call", lambda *a, **k:
                        '[{"merchant": "ZORBLAX KITCHEN TORONTO", "category": "Eats & Drinks", "confidence": "high"}]')
    llm.suggest_categories(conn)
    still_null = conn.execute("SELECT COUNT(*) FROM transactions WHERE category IS NULL").fetchone()[0]
    assert still_null == 3

    llm.accept(conn, "zorblax kitchen")
    rules.recategorise(conn)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category IS NULL").fetchone()[0] == 0
    # ...and what it produced is an ordinary, visible, deletable user rule.
    mine = [r for r in rules.inventory(conn) if r.is_user]
    assert [r.pattern for r in mine] == ["zorblax kitchen"]
    assert mine[0].category == "Eats & Drinks"


def test_a_category_outside_the_list_is_discarded(tmp_path, monkeypatch):
    conn = setup(tmp_path)
    tx(conn, "ZORBLAX KITCHEN TORONTO")
    categorize_all(conn)
    monkeypatch.setenv("FINASST_LLM_URL", "http://127.0.0.1:9/v1/chat/completions")
    monkeypatch.setattr(llm, "_call", lambda *a, **k:
                        '[{"merchant": "ZORBLAX KITCHEN TORONTO", "category": "Snacks", "confidence": "high"}]')
    made = llm.suggest_categories(conn)
    assert made[0].category is None
    with pytest.raises(llm.LLMError, match="no category to accept"):
        llm.accept(conn, "zorblax kitchen")


@pytest.mark.parametrize("reply", [
    '```json\n[{"merchant": "X", "category": "Travel", "confidence": "high"}]\n```',
    'Sure! Here you go:\n[{"merchant": "X", "category": "Travel", "confidence": "high"}]',
    '[{"merchant": "X", "category": "Travel", "confidence": "high"}]',
])
def test_json_is_recovered_from_a_chatty_reply(reply):
    assert llm._parse_labels(reply)["X"]["category"] == "Travel"


@pytest.mark.parametrize("reply", ["I cannot help with that.", "", "{}", "[not json"])
def test_an_unparseable_reply_is_not_a_crash(reply):
    assert llm._parse_labels(reply) == {}


def test_a_local_endpoint_is_recognised_as_local(tmp_path, monkeypatch):
    monkeypatch.setenv("FINASST_LLM_URL", "http://127.0.0.1:11434/v1/chat/completions")
    assert llm.config().is_local is True
    monkeypatch.setenv("FINASST_LLM_URL", "https://api.example.com/v1/chat/completions")
    assert llm.config().is_local is False
