"""End-to-end checks over the HTTP layer.

These exist because the defects that reached the user were not in the maths --
they were a 500 from a query string, a page that silently stopped at row 200,
and an upload filename that escaped its directory.
"""
import pytest
from fastapi.testclient import TestClient

from finasst import db
from finasst.web import app as webapp


@pytest.fixture()
def client(tmp_path, monkeypatch):
    dbfile = tmp_path / "t.db"
    monkeypatch.setattr(webapp.db.config, "DB_PATH", dbfile)
    conn = db.connect(dbfile)
    db.init_db(conn)
    db.get_or_create_account(conn, "Amex", "amex")
    for i in range(250):
        conn.execute(
            "INSERT INTO transactions(account_id,date,description,raw_description,amount,fingerprint)"
            " VALUES (1,?,?,?,?,?)",
            (f"2026-0{1 + i % 6}-1{i % 9}", f"MERCHANT {i}", f"MERCHANT {i}", -10.0 - i, f"fp{i}"),
        )
    conn.execute("UPDATE transactions SET category='Groceries', category_source='rule' WHERE id <= 40")
    conn.commit()
    conn.close()
    with TestClient(webapp.app) as c:
        yield c


def test_every_page_renders(client):
    for path in ("/", "/transactions", "/goals", "/import", "/remittances", "/rules"):
        assert client.get(path).status_code == 200, path


def test_a_nonsense_month_is_not_a_500(client):
    assert client.get("/?month=abc").status_code == 200
    assert client.get("/?months=0").status_code == 200
    assert client.get("/?months=-4").status_code == 200
    assert client.get("/transactions?month=not-a-month").status_code == 200


def test_pagination_covers_every_row(client):
    body = client.get("/transactions").text
    assert "of</strong> <strong>250</strong>" in body.replace("\n", " ") or "250" in body
    assert "Page 1 of 3" in body
    page3 = client.get("/transactions?page=3").text
    assert "Page 3 of 3" in page3
    # A page past the end clamps rather than showing an empty table.
    assert "Page 3 of 3" in client.get("/transactions?page=99").text


def test_csv_export_returns_every_matching_row(client):
    resp = client.get("/transactions.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert len(resp.text.strip().splitlines()) == 251     # header + 250
    filtered = client.get("/transactions.csv?category=Groceries")
    assert len(filtered.text.strip().splitlines()) == 41


def test_bulk_categorise_needs_a_filter(client):
    """Refusing to relabel the whole database on an empty filter."""
    resp = client.post("/transactions/bulk", data={"category": "Travel"}, follow_redirects=False)
    assert resp.status_code == 400


def test_bulk_categorise_applies_to_the_filtered_set(client):
    resp = client.post(
        "/transactions/bulk",
        data={"category": "Travel", "category_filter": "Groceries"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "bulk=40" in resp.headers["location"]
    assert len(client.get("/transactions.csv?category=Travel").text.strip().splitlines()) == 41


def test_a_surplus_override_that_is_not_a_number_is_refused(client):
    resp = client.post("/settings", data={
        "annual_return_rate": 0.04, "annual_inflation": 0.025,
        "surplus_lookback_months": 3, "manual_monthly_surplus": "two thousand",
    }, follow_redirects=False)
    assert resp.status_code == 400
    assert client.get("/goals").status_code == 200


def test_an_upload_filename_cannot_escape_its_directory(client, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("important user data")
    csv = b"Date,Description,Amount\n2026-07-01,LOBLAWS,-42.00\n"
    client.post(
        "/import",
        files={"files": (str(victim), csv, "text/csv")},
        data={"account": "Test", "issuer": "generic", "kind": "credit"},
    )
    assert victim.read_text() == "important user data"
    assert victim.exists()


def test_an_import_can_be_undone(client):
    csv = b"Date,Description,Amount\n2026-07-01,ZORBLAX CAFE,-42.00\n"
    client.post("/import", files={"files": ("oops.csv", csv, "text/csv")},
                data={"account": "Oops", "issuer": "generic", "kind": "credit"})
    assert "ZORBLAX CAFE" in client.get("/transactions?q=ZORBLAX").text
    resp = client.post("/import/undo", data={"source_file": "oops.csv"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "ZORBLAX CAFE" not in client.get("/transactions?q=ZORBLAX").text
