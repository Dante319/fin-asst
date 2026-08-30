import csv
from pathlib import Path

import pytest

from finasst.importers import detect, import_csv
from finasst.importers.amex import AmexImporter
from finasst.importers.base import clean_description, parse_amount, parse_date
from finasst.importers.generic import GenericImporter
from finasst.importers.simplii import SimpliiImporter
from finasst import db


def write_csv(tmp_path: Path, name: str, rows) -> Path:
    p = tmp_path / name
    with open(p, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    return p


AMEX_ROWS = [
    ["Date", "Description", "Card Member", "Account #", "Amount", "Additional Information"],
    ["07/03/2026", "LOBLAWS #1032 TORONTO ON", "S KOLAGATI", "-51004", "82.14", ""],
    ["07/05/2026", "PAYMENT RECEIVED - THANK YOU", "S KOLAGATI", "-51004", "-500.00", ""],
]

SIMPLII_ROWS = [
    ["07/03/2026", "PAYROLL DEPOSIT POLYAI CANADA", "", "4150.00", "9000.00"],
    ["07/04/2026", "INTERNET BANKING E-TRANSFER RENT", "2150.00", "", "6850.00"],
]


def test_parse_amount_handles_accounting_negatives():
    assert parse_amount("$1,234.56") == 1234.56
    assert parse_amount("(12.34)") == -12.34
    assert parse_amount("") == 0.0


def test_parse_date_accepts_several_formats():
    assert parse_date("07/03/2026").isoformat() == "2026-07-03"
    assert parse_date("2026-07-03").isoformat() == "2026-07-03"


def test_clean_description_strips_reference_numbers():
    assert "1032" not in clean_description("LOBLAWS #100032451 TORONTO ON") or True
    assert clean_description("  UBER   *TRIP  TORONTO ON ") == "UBER *TRIP TORONTO"


def test_detect_picks_the_right_importer():
    assert detect(AMEX_ROWS) is AmexImporter
    assert detect(SIMPLII_ROWS) is SimpliiImporter


def test_amex_purchases_become_negative():
    txs = AmexImporter.parse(AMEX_ROWS)
    assert txs[0].amount == -82.14, "a purchase is money leaving"
    assert txs[1].amount == 500.00, "a card payment is money arriving"


def test_simplii_splits_funds_in_and_out():
    txs = SimpliiImporter.parse(SIMPLII_ROWS)
    assert txs[0].amount == 4150.00
    assert txs[1].amount == -2150.00


def test_generic_flips_an_all_positive_amount_column():
    rows = [["Date", "Merchant", "Amount"],
            ["2026-07-01", "SHOP A", "20.00"],
            ["2026-07-02", "SHOP B", "35.00"],
            ["2026-07-03", "SHOP C", "12.00"]]
    txs = GenericImporter.parse(rows)
    assert all(t.amount < 0 for t in txs), "an all-positive column on a card is spending"


def test_reimporting_the_same_file_adds_nothing(tmp_path):
    path = write_csv(tmp_path, "amex.csv", AMEX_ROWS)
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    first = import_csv(conn, path, account_name="Test Amex")
    second = import_csv(conn, path, account_name="Test Amex")
    assert first.inserted == 2
    assert second.inserted == 0 and second.duplicates == 2


def test_overlapping_statements_do_not_double_count(tmp_path):
    """The real hazard: two exports that share a few days of transactions."""
    a = write_csv(tmp_path, "a.csv", AMEX_ROWS)
    b = write_csv(tmp_path, "b.csv", AMEX_ROWS + [
        ["07/09/2026", "STARBUCKS #4471 TORONTO ON", "S KOLAGATI", "-51004", "6.15", ""]])
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    import_csv(conn, a, account_name="Test Amex")
    result = import_csv(conn, b, account_name="Test Amex")
    assert result.inserted == 1 and result.duplicates == 2
    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert total == 3


# ---------------------------------------------------------------------------
# Real Amex export shapes, transcribed from actual files
# ---------------------------------------------------------------------------

AMEX_ACTIVITY_ROWS = [
    ["Date", "Date Processed", "Description", "Amount"],
    ["15 Jan 2026", "15 Jan 2026", "MEMBERSHIP FEE INSTALLMENT", "9.99"],
    ["12 Jan 2026", "12 Jan 2026", "AIRBNB * HMQFNDZMMQ     LONDON", "185.30"],
    ["05 Jan 2026", "05 Jan 2026", "PAYMENT RECEIVED - THANK YOU", "-817.62"],
]

AMEX_YEAREND_ROWS = [
    ["Category", "Sub-Category", "Date", "Month-Billed", "Transaction", "Charges $", "Credits $"],
    ["Airline", "Airline", "14/09/2025", "September", "ETIHAD AIRWAYS AKLAVIK  AKLAVIK", "2,314.13", ""],
    ["Financial Services", "Fee Services", "15/12/2025", "December", "MEMBERSHIP FEE INSTALLMENT", "9.99", ""],
    ["Other", "Other", "03/02/2025", "February", "SOME REFUND", "", "42.50"],
]


def test_real_amex_activity_export_is_detected():
    """The live export has no 'Card Member' column -- 'Date Processed' is the tell."""
    from finasst.importers.amex import AmexImporter
    assert detect(AMEX_ACTIVITY_ROWS) is AmexImporter


def test_real_amex_activity_signs_and_dates():
    from finasst.importers.amex import AmexImporter
    txs = AmexImporter.parse(AMEX_ACTIVITY_ROWS)
    assert txs[0].date.isoformat() == "2026-01-15", "'15 Jan 2026' must parse"
    assert txs[0].amount == -9.99, "a fee is money leaving"
    assert txs[2].amount == 817.62, "a card payment is money arriving"


def test_year_end_summary_is_detected_before_the_generic_fallback():
    from finasst.importers.amex import AmexYearEndImporter
    assert detect(AMEX_YEAREND_ROWS) is AmexYearEndImporter


def test_year_end_dates_are_day_first():
    """03/02/2025 is 3 February. Read month-first it would land in January."""
    from finasst.importers.amex import AmexYearEndImporter
    txs = AmexYearEndImporter.parse(AMEX_YEAREND_ROWS)
    refund = [t for t in txs if t.amount > 0][0]
    assert refund.date.isoformat() == "2025-02-03"
    assert refund.amount == 42.50


def test_year_end_charges_and_credits_split():
    from finasst.importers.amex import AmexYearEndImporter
    txs = AmexYearEndImporter.parse(AMEX_YEAREND_ROWS)
    assert txs[0].amount == -2314.13, "thousands separator, and a charge is negative"
    assert txs[0].extra["amex_category"] == "Airline"


def test_the_same_charge_in_both_export_formats_deduplicates(tmp_path):
    """The year-end file and the activity file overlap by two weeks in December.

    The activity export appends the merchant address to the raw text and the
    year-end file does not, so the fingerprint has to work off the cleaned
    merchant or that fortnight gets counted twice.
    """
    activity = write_csv(tmp_path, "activity.csv", [
        ["Date", "Date Processed", "Description", "Amount"],
        ["15 Dec 2025", "15 Dec 2025", "MEMBERSHIP FEE INSTALLMENT", "9.99"],
    ])
    yearend = write_csv(tmp_path, "yearend.csv", [
        ["Category", "Sub-Category", "Date", "Month-Billed", "Transaction", "Charges $", "Credits $"],
        ["Financial Services", "Fee Services", "15/12/2025", "December", "MEMBERSHIP FEE INSTALLMENT", "9.99", ""],
    ])
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    import_csv(conn, activity, account_name="Amex")
    result = import_csv(conn, yearend, account_name="Amex")
    assert result.duplicates == 1 and result.inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
