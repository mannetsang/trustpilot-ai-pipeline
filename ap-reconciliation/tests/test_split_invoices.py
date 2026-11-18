"""Invoices that list several card charges (an Amazon order shipped in parts is charged per shipment and
lists every charge under "Credit Card transactions"): each charge is matched to its own statement line,
the same amount on the same card within a day, and several transactions share the invoice."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import ApiTestCase, inv, txn  # noqa: E402
import invoices  # noqa: E402
import matching  # noqa: E402
from store import MemoryStore, StoreError  # noqa: E402


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


class SharedInvoiceStoreTests(unittest.TestCase):
    def test_one_transaction_per_charge_and_none_whole_beside_them(self):
        store = MemoryStore()
        store.insert_transactions([txn("a", "2026-07-16", "Amzn", 68.94), txn("b", "2026-07-16", "Amzn", 83.72),
                                   txn("c", "2026-07-16", "Amzn", 83.72)])
        store.update_transaction("a", {"invoice_id": "o1", "invoice_part": 1})
        store.update_transaction("b", {"invoice_id": "o1", "invoice_part": 0})
        with self.assertRaises(StoreError):
            store.update_transaction("c", {"invoice_id": "o1", "invoice_part": 0})       # that charge is taken
        self.assertEqual(store.used_invoice_parts(), {"o1": {0, 1}})


class SharedInvoiceMatchingTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store.upsert_invoices([amazon("o1", 152.66, [("2026-07-16", 83.72, "1610"), ("2026-07-15", 68.94, "1610")])])

    def test_match_month_links_both_charges_of_one_order(self):
        self.store.insert_transactions([txn("t1", "2026-07-16", "Amzn Mktp Ca", 68.94),
                                        txn("t2", "2026-07-16", "Amzn Mktp Ca", 83.72)])
        counts = matching.match_month(self.store, None, 2026, 7, use_ai=False, log=lambda *a: None)
        t1, t2 = self.store.get_transaction("t1"), self.store.get_transaction("t2")
        self.assertEqual((t1["invoice_status"], t1["invoice_id"], t1["invoice_part"]), ("matched", "o1", 1))
        self.assertEqual((t2["invoice_status"], t2["invoice_id"], t2["invoice_part"]), ("matched", "o1", 0))
        self.assertEqual(counts["matched"], 2)

    def test_an_order_listing_several_charges_is_never_linked_whole(self):
        self.store.insert_transactions([txn("t", "2026-07-16", "Amzn Mktp Ca", 152.66)])     # the order's total
        matching.match_month(self.store, None, 2026, 7, use_ai=False, log=lambda *a: None)
        self.assertEqual(self.store.get_transaction("t")["invoice_status"], "missing")

    def test_a_possible_whole_link_to_such_an_order_gives_way_to_the_charges(self):
        self.store.insert_transactions([
            dict(txn("t1", "2026-07-16", "Amzn Mktp Ca", 68.94, status="possible", invoice_id="o1", method="ai"),
                 match_confidence=0.7),
            txn("t2", "2026-07-16", "Amzn Mktp Ca", 83.72)])
        matching.match_month(self.store, None, 2026, 7, use_ai=False, log=lambda *a: None)
        self.assertEqual(self.store.get_transaction("t1")["invoice_part"], 1)
        self.assertEqual(self.store.get_transaction("t2")["invoice_part"], 0)

    def test_the_per_row_rematch_takes_a_free_charge(self):
        self.store.insert_transactions([
            dict(txn("t1", "2026-07-16", "Amzn Mktp Ca", 68.94, status="matched", invoice_id="o1", method="rule"),
                 invoice_part=1),
            txn("t2", "2026-07-17", "Amzn Mktp Ca", 83.72)])
        out = matching.match_transaction(self.store, None, "t2", use_ai=False, log=lambda *a: None)
        self.assertEqual((out["outcome"], out["invoice_id"]), ("found", "o1"))
        self.assertEqual(self.store.get_transaction("t2")["invoice_part"], 0)

    def test_the_watch_matches_the_charges_of_a_new_order(self):
        self.store.insert_transactions([txn("t1", "2026-07-16", "Amzn Mktp Ca", 68.94),
                                        txn("t2", "2026-07-16", "Amzn Mktp Ca", 83.72)])
        counts = matching.match_new_invoices(self.store, None, ["o1"], use_ai=False, log=lambda *a: None)
        self.assertEqual(counts["matched"], 2)
        self.assertEqual(self.store.used_invoice_parts(), {"o1": {0, 1}})


class SharedInvoiceApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.store.upsert_invoices([amazon("o1", 152.66, [("2026-07-16", 83.72, "1610"), ("2026-07-15", 68.94, "1610")]),
                                    inv("plain", "Staples", 20.0, "2026-07-10", month_folder="2026 07")])
        self.store.insert_transactions([txn("t1", "2026-07-16", "Amzn Mktp Ca", 68.94),
                                        txn("t2", "2026-07-16", "Amzn Mktp Ca", 83.72),
                                        txn("t3", "2026-07-16", "Amzn Mktp Ca", 10.00),
                                        txn("t4", "2026-07-12", "Staples", 20.00),
                                        txn("t5", "2026-07-12", "Staples", 20.00)])

    def test_link_by_hand_takes_the_charge_of_the_same_amount_then_the_next_free_one(self):
        row = self.patch("t2", {"action": "link", "invoice_id": "o1"})["transaction"]
        self.assertEqual((row["invoice_part"], row["invoice"]["charge"]["number"], row["invoice"]["charge"]["of"]), (0, 1, 2))
        self.assertIn("charge 1 of 2", row["match_note"])
        row = self.patch("t3", {"action": "link", "invoice_id": "o1"})["transaction"]
        self.assertEqual(row["invoice_part"], 1)                              # the only charge left
        r = self.patch("t1", {"action": "link", "invoice_id": "o1"}, expect=409)
        self.assertIn("every card charge", r["error"])

    def test_a_plain_invoice_is_still_one_to_one_and_unlink_frees_the_charge(self):
        self.patch("t4", {"action": "link", "invoice_id": "plain"})
        self.patch("t5", {"action": "link", "invoice_id": "plain"}, expect=409)
        self.patch("t2", {"action": "link", "invoice_id": "o1"})
        row = self.patch("t2", {"action": "unlink"})["transaction"]
        self.assertEqual((row["invoice_id"], row["invoice_part"]), (None, None))
        self.assertEqual(self.store.used_invoice_parts(), {"plain": {"whole"}})

    def test_the_invoice_list_says_how_many_charges_are_linked(self):
        self.patch("t2", {"action": "link", "invoice_id": "o1"})
        r = self.client.get("/api/invoices?year=2026&month=7")
        o1 = next(i for i in r.get_json()["invoices"] if i["id"] == "o1")
        self.assertEqual((o1["linked"], o1["charges"], o1["charges_linked"]), (False, 2, 1))
        self.patch("t1", {"action": "link", "invoice_id": "o1"})
        o1 = next(i for i in self.client.get("/api/invoices?year=2026&month=7").get_json()["invoices"] if i["id"] == "o1")
        self.assertTrue(o1["linked"])


class RereadTests(ApiTestCase):
    def test_amazon_invoices_read_before_are_read_again_and_their_months_queued(self):
        import main
        from unittest import mock
        from test_pipeline import FakeDrive, FakeGemini, file_, folder
        old = dict(inv("o1", "Amazon.ca", 152.66, "2026-07-15", month_folder="2026 07", vendor_folder="Amazon"),
                   file_name="2026-07-15_702-4984379-8097821.pdf")
        other = inv("s1", "Staples", 20.0, "2026-07-10", month_folder="2026 07")
        self.store.upsert_invoices([old, other])
        drive = FakeDrive({"root": [folder("m07", "2026 07")], "m07": [folder("v", "Amazon")],
                           "v": [file_("o1", old["file_name"])]})
        facts = {"is_invoice": True, "vendor": "Amazon.ca", "invoice_number": "702-4984379-8097821",
                 "invoice_date": "2026-07-15", "total": 152.66, "currency": "CAD", "card_last4": "1610",
                 "card_charges": [{"date": "2026-07-16", "amount": 83.72, "card_last4": "1610"},
                                  {"date": "2026-07-15", "amount": 68.94, "card_last4": "1610"}],
                 "summary": "surge protector, laptop case, extension cord"}
        gemini = FakeGemini(handler=lambda parts, model: dict(facts))
        main.set_clients(drive=drive, gemini=gemini)
        with mock.patch.object(main, "AI_DISABLED", False):
            out = main.reread_card_charges("amazon", log=lambda *a: None)
            again = main.reread_card_charges("amazon", log=lambda *a: None)
        self.assertEqual((out["to_read"], out["read"], out["failed"]), (1, 1, 0))
        self.assertEqual(again["to_read"], 0)                             # done is not read again
        row = self.store.get_invoice("o1")
        self.assertEqual([c["amount"] for c in row["card_charges"]], [83.72, 68.94])
        self.assertIsNone(self.store.get_invoice("s1").get("card_charges"))   # another vendor is left alone
        pending = (self.store.get_state("bank_sync") or {}).get("months_pending")
        self.assertEqual(sorted(tuple(m) for m in pending), [(2026, 6), (2026, 7), (2026, 8)])


if __name__ == "__main__":
    unittest.main()
