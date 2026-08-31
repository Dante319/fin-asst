"""Simplii PDF statements: the Cash Back Visa credit card and the no-fee
chequing account. Fixtures below are trimmed, hand-built extraction output
that keeps the two quirks that matter: charge rows split across two physical
lines, and chequing rows where two amount columns land jammed together with
no separator.
"""
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
