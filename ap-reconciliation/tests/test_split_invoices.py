"""Invoices that list several card charges (an Amazon order shipped in parts is charged per shipment and
lists every charge under "Credit Card transactions"): each charge is matched to its own statement line,
the same amount on the same card within a day, and several transactions share the invoice."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import inv, txn  # noqa: E402
import invoices  # noqa: E402
import matching  # noqa: E402


def amazon(iid, total, charges, date="2026-07-15", month_folder="2026 07", currency="CAD", card=None):
    row = inv(iid, "Amazon.ca", total, date, month_folder=month_folder, vendor_folder="Amazon", currency=currency, card=card)
    row["card_charges"] = [{"date": d, "amount": a, "card_last4": c} for d, a, c in charges]
    return row


class CardChargesReadingTests(unittest.TestCase):
    def test_the_listed_charges_are_kept_in_order_and_refunds_stay_negative(self):
        got = invoices.card_charges([
            {"date": "July 16, 2026", "amount": "CAD 83.72", "card_last4": "Visa ending in 1610"},
            {"date": "2026-07-15", "amount": 68.94, "card_last4": "1610"},
            {"date": "2026-07-20", "amount": "-12.00", "card_last4": None},
            {"date": "2026-07-21", "amount": None},
            "not a charge",
        ])
        self.assertEqual(got, [{"date": "2026-07-16", "amount": 83.72, "card_last4": "1610"},
                               {"date": "2026-07-15", "amount": 68.94, "card_last4": "1610"},
                               {"date": "2026-07-20", "amount": -12.0, "card_last4": None}])

    def test_no_list_is_none_and_an_empty_list_is_empty(self):
        self.assertIsNone(invoices.card_charges(None))
        self.assertIsNone(invoices.card_charges("Visa 83.72"))
        self.assertEqual(invoices.card_charges([]), [])


class ChargeMatchTests(unittest.TestCase):
    def setUp(self):
        # Order 702-4984379-8097821: two shipments, two charges; the statement shows both a day later.
        self.order = amazon("o1", 152.66, [("2026-07-16", 83.72, "1610"), ("2026-07-15", 68.94, "1610")])
        self.t1 = txn("t1", "2026-07-16", "Amzn Mktp Ca", 68.94)
        self.t2 = txn("t2", "2026-07-16", "Amzn Mktp Ca", 83.72)

    def test_each_charge_goes_to_its_own_purchase_and_both_share_the_invoice(self):
        got = matching.charge_match([self.t1, self.t2], [self.order])
        self.assertEqual({k: v[:3] for k, v in got.items()}, {"t1": ("o1", 1, "matched"), "t2": ("o1", 0, "matched")})
        self.assertIn("charge 2 of 2", got["t1"][4])

    def test_a_day_either_side_is_allowed_two_days_is_not(self):
        late = txn("t3", "2026-07-17", "Amzn Mktp Ca", 83.72)        # Amazon says Jul 16: one day
        later = txn("t4", "2026-07-18", "Amzn Mktp Ca", 68.94)       # Amazon says Jul 15: three days
        got = matching.charge_match([late, later], [self.order])
        self.assertEqual(set(got), {"t3"})

    def test_identical_charges_go_to_identical_purchases_one_each(self):
        monitors = amazon("o2", 290.08, [("2026-10-01", 145.04, "1610"), ("2026-10-01", 145.04, "1610")],
                          date="2026-09-30", month_folder="2026 09")
        a, b = txn("a", "2026-10-01", "Amazon.Ca*1", 145.04), txn("b", "2026-10-01", "Amazon.Ca*2", 145.04)
        c = txn("c", "2026-10-01", "Amazon.Ca*3", 145.04)
        got = matching.charge_match([a, b, c], [monitors])
        self.assertEqual(sorted(v[1] for v in got.values()), [0, 1])
        self.assertEqual(len(got), 2)                                    # only two charges were listed

    def test_another_card_another_currency_or_a_taken_charge_is_not_used(self):
        other_card = txn("x", "2026-07-16", "Amzn Mktp Ca", 83.72, card="2887")
        usd = txn("y", "2026-07-16", "Amzn Mktp Ca", 83.72, currency="USD")
        self.assertEqual(matching.charge_match([other_card, usd], [self.order]), {})
        got = matching.charge_match([self.t2], [self.order], taken={"o1": {0}})
        self.assertEqual(got, {})

    def test_a_single_listed_charge_links_the_whole_invoice(self):
        one = amazon("o3", 365.70, [("2026-07-20", 365.70, "9456")], currency="USD", card="9456")
        t = txn("t", "2026-07-21", "AMAZON MKTPL*X", 365.70, card="9456", currency="USD")
        self.assertEqual(matching.charge_match([t], [one])["t"][:2], ("o3", None))
        self.assertEqual(matching.charge_match([t], [one], taken={"o3": {matching.WHOLE}}), {})

    def test_a_refund_listed_on_the_invoice_is_not_a_charge(self):
        refunded = amazon("o4", 20.0, [("2026-07-15", 20.0, "1610"), ("2026-07-20", -20.0, "1610")])
        t = txn("t", "2026-07-20", "Amzn Mktp Ca", 20.0)
        self.assertEqual(matching.charge_match([t], [refunded]), {})       # the Jul 20 line is the refund


if __name__ == "__main__":
    unittest.main()
