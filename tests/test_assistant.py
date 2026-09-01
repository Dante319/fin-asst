"""The assistant's tool layer and the MCP transport.

The contract being tested is mostly about what the assistant CANNOT do: write
to the database, hand back a total that is really a page, or report a figure
without the caveats that qualify it.
"""
import io
import json
import sqlite3

import pytest

from finasst import assistant, db
from finasst.categorize import categorize_all, seed_rules
from finasst.mcp_server import Server


@pytest.fixture()
def populated(tmp_path):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    db.init_db(conn)
    seed_rules(conn)
    db.get_or_create_account(conn, "Amex", "amex")
    db.get_or_create_account(conn, "Chequing", "simplii", "chequing")
    import calendar
    n = 0
    for month in ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"):
        last = calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
        for account, day, desc, amount in (
            (2, "01", "PAYROLL DEPOSIT POLYAI", 4000.0),
            (1, "05", "NETFLIX.COM", -20.99),
            (1, "09", "LOBLAWS #1032 TORONTO", -140.0),
            (1, "12", "UBER EATS TORONTO", -46.0),
            (1, "18", "MYSTERY VENDOR LTD", -75.0),
            (1, f"{last:02d}", "TTC FARE", -3.35),
        ):
            n += 1
            conn.execute(
                "INSERT INTO transactions(account_id,date,description,raw_description,"
                "amount,fingerprint) VALUES (?,?,?,?,?,?)",
                (account, f"{month}-{day}", desc, desc, amount, f"f{n}"))
    conn.commit()
    categorize_all(conn)
    conn.close()
    return path


# ---- read-only is a guarantee, not a convention -------------------------

def test_the_connection_cannot_write_even_if_asked(populated):
    conn = assistant.open_db(populated)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM transactions")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE transactions SET category = 'Travel'")


def test_a_missing_database_says_what_to_do(tmp_path):
    with pytest.raises(FileNotFoundError, match="finasst init"):
        assistant.open_db(tmp_path / "nope.db")


def test_a_database_without_the_current_schema_is_reported_not_half_used(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, date TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(assistant.DataNotReady, match="finasst init"):
        assistant.open_db(path)


# ---- the catalogue -------------------------------------------------------

def test_every_tool_declares_a_usable_schema():
    for tool in assistant.TOOLS:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert tool["description"].strip()
        for name, spec in schema["properties"].items():
            assert "type" in spec, f"{tool['name']}.{name} has no type"
        for required in schema["required"]:
            assert required in schema["properties"]


def test_every_tool_runs_on_real_data(populated):
    conn = assistant.open_db(populated)
    for tool in assistant.TOOLS:
        args = {"description": "NETFLIX.COM"} if tool["name"] == "explain_category" else {}
        result = assistant.call(conn, tool["name"], args)
        assert isinstance(result, dict)
        json.dumps(result, default=str)          # must survive the wire


def test_unknown_arguments_are_dropped_rather_than_crashing(populated):
    conn = assistant.open_db(populated)
    assert assistant.call(conn, "monthly_totals", {"nonsense": 1, "month": "2026-01"})


def test_an_unknown_tool_names_the_ones_that_exist(populated):
    conn = assistant.open_db(populated)
    with pytest.raises(KeyError, match="category_breakdown"):
        assistant.call(conn, "totally_made_up", {})


# ---- caveats travel with the numbers ------------------------------------

def test_figures_come_back_with_their_qualifications(populated):
    conn = assistant.open_db(populated)
    described = assistant.call(conn, "describe_data", {})
    assert described["caveats"], "uncategorised spending should have been flagged"
    assert any("uncategorised" in c for c in described["caveats"])
    for name in ("monthly_totals", "category_breakdown", "top_merchants", "findings"):
        assert "caveats" in assistant.call(conn, name, {})


def test_partly_imported_months_are_marked_as_such(populated):
    conn = assistant.open_db(populated)
    result = assistant.call(conn, "monthly_totals", {})
    assert all("statements_complete" in m for m in result["months"])
    assert "do not compare" in result["note"].lower()


def test_a_page_of_rows_says_it_is_not_a_total(populated):
    conn = assistant.open_db(populated)
    result = assistant.call(conn, "search_transactions", {"limit": 2})
    assert result["returned"] == 2
    assert result["matched"] > 2
    assert "not a total" in result["note"]


def test_the_whole_result_set_says_so(populated):
    conn = assistant.open_db(populated)
    result = assistant.call(conn, "search_transactions", {"query": "NETFLIX", "limit": 100})
    assert result["matched"] == result["returned"] == 6
    assert "Every matching row" in result["note"]


def test_a_search_wildcard_is_a_search_not_a_match_all(populated):
    conn = assistant.open_db(populated)
    assert assistant.call(conn, "search_transactions", {"query": "%"})["matched"] == 0


def test_the_instructions_forbid_the_two_things_that_go_wrong():
    text = assistant.INSTRUCTIONS.lower()
    assert "describe_data" in text
    assert "arithmetic" in text
    assert "read-only" in text


# ---- MCP transport -------------------------------------------------------

def drive(populated, *messages):
    out = io.StringIO()
    server = Server(db_path=populated,
                    stdin=io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n"),
                    stdout=out)
    server.run()
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_initialize_echoes_a_protocol_it_knows(populated):
    [reply] = drive(populated, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2024-11-05"}})
    assert reply["result"]["protocolVersion"] == "2024-11-05"
    assert reply["result"]["serverInfo"]["name"] == "fin-asst"
    assert "describe_data" in reply["result"]["instructions"]


def test_an_unknown_protocol_gets_our_newest(populated):
    [reply] = drive(populated, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "1999-01-01"}})
    assert reply["result"]["protocolVersion"] == "2025-06-18"


def test_notifications_get_no_reply(populated):
    replies = drive(populated, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert replies == []


def test_tools_are_listed_with_their_schemas(populated):
    [reply] = drive(populated, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = reply["result"]["tools"]
    assert len(tools) == len(assistant.TOOLS)
    assert all("inputSchema" in t and t["description"] for t in tools)


def test_a_tool_call_returns_json_text(populated):
    [reply] = drive(populated, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "describe_data", "arguments": {}}})
    assert reply["result"]["isError"] is False
    payload = json.loads(reply["result"]["content"][0]["text"])
    assert payload["accounts"]


def test_a_bad_tool_call_is_an_answer_not_a_crash(populated):
    replies = drive(
        populated,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "no_such_tool", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "explain_category", "arguments": {}}},
    )
    assert all(r["result"]["isError"] for r in replies)
    # ...and the server is still going afterwards
    assert "No such tool" in replies[0]["result"]["content"][0]["text"]


def test_malformed_json_does_not_take_the_pipe_down(populated):
    out = io.StringIO()
    Server(db_path=populated,
           stdin=io.StringIO('not json\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n'),
           stdout=out).run()
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == -32700
    assert replies[1]["result"] == {}


def test_an_unsupported_method_is_reported_properly(populated):
    [reply] = drive(populated, {"jsonrpc": "2.0", "id": 1, "method": "sampling/createMessage"})
    assert reply["error"]["code"] == -32601


def test_a_missing_database_comes_back_as_a_tool_error(tmp_path):
    out = io.StringIO()
    Server(db_path=tmp_path / "absent.db",
           stdin=io.StringIO(json.dumps(
               {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "describe_data", "arguments": {}}}) + "\n"),
           stdout=out).run()
    reply = json.loads(out.getvalue())
    assert reply["result"]["isError"] is True
    assert "finasst init" in reply["result"]["content"][0]["text"]
