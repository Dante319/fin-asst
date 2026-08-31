"""Simplii PDF statements: the Cash Back Visa credit card and the no-fee
chequing account. Fixtures below are trimmed, hand-built extraction output
that keeps the two quirks that matter: charge rows split across two physical
lines, and chequing rows where two amount columns land jammed together with
no separator.
"""
import pytest

from finasst.importers.simplii_pdf import SimpliiChequingPdfImporter, SimpliiCreditPdfImporter

CREDIT_STATEMENT = """Simplii Financial Cash Back Visa
Your account at a glance
Previous balance $424.51
Payments $430.00
Other credits 0.00
Total credits - $430.00
Purchases 1,810.51
Cash advances 0.00
Interest 0.00
Fees 0.00
Total charges + $1,810.51
New balance = $1,805.02
August statement period
July 11 to August 10, 2026
Your payments
Trans
date
Post
date Description Amount($)
Jul 30 Jul 31 PAYMENT THANK YOU/PAIEMENT MERCI 430.00
Total payments $430.00
Your new charges and credits
Card number 4525 XXXX XXXX 0201
Jul 10 Jul 13 Ý METRO  759               TORONTO      ON
 Retail and Grocery 1780.51
Jul 12 Jul 13 TST-RUDY Duncan          Toronto      ON
 Restaurants 30.00
If you find an error or irregularity in this statement you must tell us
"""

CHEQUING_STATEMENT = """Simplii Financial
your no fee chequing account
statement period: June 29, 2026 - July 29, 2026
statement date: July 29, 2026
account number: 0112717129
details
trans.
date
eff.
date
transaction funds out funds in balance
Jun 29 Jun 29 BALANCE FORWARD 2,128.18
Jun 29 Jun 29 INTERAC E-TRANSFER RECEIVE Santosh Kolagati 2,628.18500.00
Jul 03 Jul 02 CHEQUE #12 528.182,100.00
Jul 13 Jul 13 INTERAC E-TRANSFER SEND Suzie 468.1860.00
total funds out 2,160.00
total funds in 500.00
closing balance 468.18
end of your no fee chequing account 0112717129 information
"""


def test_credit_statement_is_recognised():
    assert SimpliiCreditPdfImporter.sniff_text(CREDIT_STATEMENT)
    assert not SimpliiChequingPdfImporter.sniff_text(CREDIT_STATEMENT)


def test_credit_payment_is_positive_and_charges_are_negative():
    txs, check = SimpliiCreditPdfImporter.parse_text(CREDIT_STATEMENT)
    by_desc = {t.description: t.amount for t in txs}
    assert by_desc["PAYMENT THANK YOU/PAIEMENT MERCI"] == 430.00
    assert by_desc["METRO 759 TORONTO"] == -1780.51
    assert by_desc["TST-RUDY Duncan Toronto"] == -30.00
    assert check.ok


def test_credit_statement_year_comes_from_the_period_line():
    txs, _ = SimpliiCreditPdfImporter.parse_text(CREDIT_STATEMENT)
    dates = {t.description: t.date.isoformat() for t in txs}
    assert dates["METRO 759 TORONTO"] == "2026-07-10"
    assert dates["PAYMENT THANK YOU/PAIEMENT MERCI"] == "2026-07-30"


def test_credit_statement_that_does_not_reconcile_is_flagged():
    broken = CREDIT_STATEMENT.replace(" Restaurants 30.00", " Restaurants 3.00")
    txs, check = SimpliiCreditPdfImporter.parse_text(broken)
    assert len(txs) == 3
    assert not check.ok


def test_chequing_statement_is_recognised():
    assert SimpliiChequingPdfImporter.sniff_text(CHEQUING_STATEMENT)
    assert not SimpliiCreditPdfImporter.sniff_text(CHEQUING_STATEMENT)


def test_chequing_amount_and_direction_come_from_the_balance_column():
    """The two jammed-together numbers cannot be split by column position --
    only the running balance says which row is money in and which is out."""
    txs, check = SimpliiChequingPdfImporter.parse_text(CHEQUING_STATEMENT)
    amounts = {t.description: t.amount for t in txs}
    assert amounts["INTERAC E-TRANSFER RECEIVE Santosh Kolagati"] == 500.00
    assert amounts["CHEQUE #12"] == -2100.00
    assert amounts["INTERAC E-TRANSFER SEND Suzie"] == -60.00
    assert check.ok
    assert check.computed_closing == 468.18


def test_chequing_statement_that_does_not_reconcile_is_flagged():
    broken = CHEQUING_STATEMENT.replace("closing balance 468.18", "closing balance 999.99")
    _, check = SimpliiChequingPdfImporter.parse_text(broken)
    assert not check.ok
    assert "off by" in check.describe()


def test_a_dropped_chequing_row_cannot_reconcile():
    """The check must verify OUR parse, not the bank's arithmetic.

    Reconciling `opening + stated_funds_in - stated_funds_out` against the
    printed closing balance always succeeds, because a bank statement is
    internally consistent by construction. It says nothing about whether the
    parser matched every row. Summing the parsed amounts is what catches a row
    the regex missed -- here, a $2,100 cheque.
    """
    dropped = CHEQUING_STATEMENT.replace("Jul 03 Jul 02 CHEQUE #12 528.182,100.00\n", "")
    txs, check = SimpliiChequingPdfImporter.parse_text(dropped)

    assert len(txs) == 2, "the cheque row is gone"
    # The statement's own totals still reconcile perfectly against each other,
    # and the missing $2,100 is silently absorbed into the next row's balance
    # delta -- so the closing balance still lands exactly on 468.18:
    assert 2128.18 + 500.00 - 2160.00 == pytest.approx(468.18)
    assert check.computed_closing == pytest.approx(check.closing)
    # ...and yet the parse must be rejected, because the row whose amount got
    # inflated no longer matches the amount printed beside it.
    assert not check.ok
    assert check.balance_breaks > 0
    assert "do not match the running balance" in check.describe()


def test_a_sign_error_is_caught_by_the_printed_totals():
    """A balance walk alone cannot see a row parsed in the wrong direction;
    the statement's printed funds-in / funds-out totals can."""
    txs, check = SimpliiChequingPdfImporter.parse_text(CHEQUING_STATEMENT)
    assert check.ok
    parsed_in = sum(t.amount for t in txs if t.amount > 0)
    parsed_out = -sum(t.amount for t in txs if t.amount < 0)
    assert parsed_in == pytest.approx(500.00)
    assert parsed_out == pytest.approx(2160.00)


def test_statement_check_strictness_differs_by_how_amounts_were_read():
    """One class, two documented behaviours -- not two classes quietly disagreeing.

    A parser that reads amounts from their own column treats a row-order break
    as cosmetic. A parser that derives the amount FROM the balance chain has no
    independent reading, so a break means the derivation failed.
    """
    from finasst.importers.base import StatementCheck

    cosmetic = StatementCheck(opening=100.0, closing=150.0, computed_closing=150.0,
                              balance_breaks=2, walk_is_evidence=False)
    derived = StatementCheck(opening=100.0, closing=150.0, computed_closing=150.0,
                             balance_breaks=2, walk_is_evidence=True)
    assert cosmetic.ok, "totals agree; ordering is not a correctness problem"
    assert not derived.ok, "the walk was the only evidence, and it broke"
