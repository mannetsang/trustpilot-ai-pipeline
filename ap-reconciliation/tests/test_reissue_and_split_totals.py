"""Reissued cards (a merchant's receipt shows the new number, the statement the old one) and invoices
paid in several card charges they do not list (Square bills three subscriptions on one tax invoice and
charges each separately)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import ApiTestCase, inv, txn  # noqa: E402
import matching  # noqa: E402
from store import MemoryStore  # noqa: E402

QUIET = dict(use_ai=False, log=lambda *a: None)


def store_with_cards(*cards):
    store = MemoryStore()
    store.upsert_cards([{"last4": c} for c in cards])
    matching.refresh_card_aliases(store, force=True)
    return store


class ReissueNumberTests(unittest.TestCase):
    def test_the_pairs_seen_on_real_receipts(self):
        # Statement card -> the number merchants show (Nov 2026): Amazon, Freightcom, Square, Reddit, TickTick.
        for old, new in (("5018", "5026"), ("7436", "7444"), ("4805", "4813"), ("4431", "4449"), ("8100", "8118")):
            self.assertIn(new, matching.reissue_numbers(old), old)
            self.assertIn(old, matching.reissue_numbers(new), new)       # and back

    def test_a_carry_beyond_the_four_digits_is_not_guessed_and_junk_is_nothing(self):
        self.assertEqual({n[:3] for n in matching.reissue_numbers("9995")}, {"998", "997"})   # no 1000, 1001
        self.assertEqual(matching.reissue_numbers("12a4"), set())
        self.assertEqual(matching.reissue_numbers(None), set())

    def test_aliases_skip_real_cards_and_numbers_two_cards_could_be(self):
        aliases = matching.card_aliases(["5018", "4805"])
        self.assertEqual((aliases["5026"], aliases["4813"]), ("5018", "4805"))
        self.assertNotIn("5026", matching.card_aliases(["5018", "5026"]))      # a card of its own
        # 0016 is 0008's next number and 0024's previous one: whose would it be?
        self.assertNotIn("0016", matching.card_aliases(["0008", "0024"]))


class ReissueMatchingTests(unittest.TestCase):
    def setUp(self):
        self.store = store_with_cards("5018", "1610")

    def test_an_order_naming_the_new_number_matches_the_statement_card(self):
        order = inv("o1", "Amazon.ca", 152.66, "2026-07-15", month_folder="2026 07", vendor_folder="Amazon",
                    card="5026")
        order["card_charges"] = [{"date": "2026-07-16", "amount": 83.72, "card_last4": "5026"},
                                 {"date": "2026-07-15", "amount": 68.94, "card_last4": "5026"}]
        single = inv("o2", "Amazon.ca", 40.00, "2026-07-20", month_folder="2026 07", vendor_folder="Amazon",
                     card="5026")
        self.store.upsert_invoices([order, single])
        self.store.insert_transactions([txn("a", "2026-07-16", "AMZN Mktp CA*1", 83.72, card="5018"),
                                        txn("b", "2026-07-16", "AMZN Mktp CA*2", 68.94, card="5018"),
                                        txn("c", "2026-07-21", "AMZN Mktp CA*3", 40.00, card="5018")])
        matching.match_month(self.store, None, 2026, 7, **QUIET)
        got = {t: self.store.get_transaction(t) for t in "abc"}
        self.assertEqual({t: (r["invoice_status"], r["invoice_id"]) for t, r in got.items()},
                         {"a": ("matched", "o1"), "b": ("matched", "o1"), "c": ("matched", "o2")})
        self.assertIn("reissue of 5018", got["c"]["match_note"])

    def test_another_card_is_still_another_card(self):
        self.store.upsert_invoices([inv("o3", "Amazon.ca", 40.00, "2026-07-20", month_folder="2026 07", card="1610")])
        self.store.insert_transactions([txn("c", "2026-07-21", "AMZN Mktp CA*3", 40.00, card="5018")])
        matching.match_month(self.store, None, 2026, 7, **QUIET)
        self.assertNotEqual(self.store.get_transaction("c")["invoice_status"], "matched")

    def test_the_receipt_check_does_not_warn_about_a_reissued_number(self):
        t = txn("c", "2026-07-21", "AMZN Mktp CA*3", 40.00, card="5018")
        kinds = lambda card: {c["kind"] for c in matching.invoice_checks(t, inv("x", "Amazon", 40.0, "2026-07-20", card=card))}
        self.assertNotIn("card", kinds("5026"))
        self.assertIn("card", kinds("1610"))


def square(iid, total, date, month_folder, card="4813"):
    return dict(inv(iid, "Square", total, date, month_folder=month_folder, card=card,
                    file_name=f"{date}_Square_{iid}.pdf"), extracted_at="2026-11-18T19:00:00+00:00")


def sq(tid, date, amount, card="4805"):
    return txn(tid, date, "SQ *SQUARE PAID SERVICES square.com", amount, card=card)


class SplitTotalTests(unittest.TestCase):
    def setUp(self):
        self.store = store_with_cards("4805")

    def test_three_charges_adding_up_to_the_invoice_are_each_linked_to_it(self):
        self.store.upsert_invoices([square("nov", 818.12, "2026-11-01", "2026 11")])
        self.store.insert_transactions([sq("t1", "2026-11-01", 237.30), sq("t2", "2026-11-01", 45.20),
                                        sq("t3", "2026-11-01", 535.62)])
        counts = matching.match_month(self.store, None, 2026, 11, **QUIET)
        self.assertEqual(counts["matched"], 3)
        rows = [self.store.get_transaction(t) for t in ("t1", "t2", "t3")]
        self.assertEqual({(r["invoice_id"], r["invoice_part"]) for r in rows}, {("nov", 0), ("nov", 1), ("nov", 2)})
        self.assertIn("add up to the invoice total", rows[0]["match_note"])
        charges = self.store.get_invoice("nov")["card_charges"]
        self.assertEqual([c["amount"] for c in charges], [535.62, 237.30, 45.20])
        self.assertTrue(all(c["inferred"] for c in charges))
        again = matching.match_month(self.store, None, 2026, 11, **QUIET)        # stable on the next run
        self.assertEqual(again["matched"], 0)
        self.assertEqual(self.store.used_invoice_parts(), {"nov": {0, 1, 2}})

    def test_two_invoices_one_paid_whole_one_in_two_charges(self):
        # May 2026: Appointments Plus on its own invoice (237.30), Online + Retail on another (580.82).
        self.store.upsert_invoices([square("a", 237.30, "2026-05-01", "2026 05"),
                                    square("b", 580.82, "2026-05-01", "2026 05")])
        self.store.insert_transactions([sq("t1", "2026-05-01", 237.30), sq("t2", "2026-05-01", 45.20),
                                        sq("t3", "2026-05-01", 535.62)])
        matching.match_month(self.store, None, 2026, 5, **QUIET)
        link = lambda t: (self.store.get_transaction(t)["invoice_id"], self.store.get_transaction(t)["invoice_part"])
        self.assertEqual(link("t1"), ("a", None))
        self.assertEqual({link("t2"), link("t3")}, {("b", 0), ("b", 1)})

    def test_two_ways_to_make_the_total_or_another_vendor_or_day_is_left_alone(self):
        self.store.upsert_invoices([square("n", 100.00, "2026-11-01", "2026 11")])
        self.store.insert_transactions([sq("t1", "2026-11-01", 60.00), sq("t2", "2026-11-01", 40.00),
                                        sq("t3", "2026-11-01", 70.00), sq("t4", "2026-11-01", 30.00)])
        matching.match_month(self.store, None, 2026, 11, **QUIET)
        self.assertIsNone(self.store.get_invoice("n").get("card_charges"))
        other = MemoryStore()
        other.upsert_cards([{"last4": "4805"}])
        other.upsert_invoices([square("n", 100.00, "2026-11-01", "2026 11")])
        other.insert_transactions([txn("x", "2026-11-01", "STAPLES 123", 60.00, card="4805"),
                                   sq("y", "2026-11-01", 40.00), sq("z", "2026-11-05", 60.00)])
        matching.match_month(other, None, 2026, 11, **QUIET)
        self.assertIsNone(other.get_invoice("n").get("card_charges"))

    def test_an_invoice_that_lists_another_charge_is_trusted(self):
        listed = square("n", 818.12, "2026-11-01", "2026 11")
        listed["card_charges"] = [{"date": "2026-11-01", "amount": 500.00, "card_last4": "4813"}]
        self.store.upsert_invoices([listed])
        self.store.insert_transactions([sq("t1", "2026-11-01", 237.30), sq("t2", "2026-11-01", 580.82)])
        matching.match_month(self.store, None, 2026, 11, **QUIET)
        self.assertEqual(self.store.get_invoice("n")["card_charges"][0]["amount"], 500.00)

    def test_the_rematch_and_the_watch_find_them_too(self):
        self.store.upsert_invoices([square("nov", 818.12, "2026-11-01", "2026 11")])
        self.store.insert_transactions([sq("t1", "2026-11-01", 237.30), sq("t2", "2026-11-01", 45.20),
                                        sq("t3", "2026-10-31", 535.62)])
        out = matching.match_transaction(self.store, None, "t2", **QUIET)
        self.assertEqual((out["outcome"], out["invoice_id"]), ("found", "nov"))
        self.assertEqual(len(self.store.get_invoice("nov")["card_charges"]), 3)
        fresh = store_with_cards("4805")
        fresh.insert_transactions([sq("t1", "2026-11-01", 237.30), sq("t2", "2026-11-01", 45.20),
                                   sq("t3", "2026-11-01", 535.62)])
        fresh.upsert_invoices([square("nov", 818.12, "2026-11-01", "2026 11")])
        counts = matching.match_new_invoices(fresh, None, ["nov"], **QUIET)
        self.assertEqual(counts["matched"], 3)

    def test_charges_are_not_recorded_over_a_newer_reading(self):
        self.store.upsert_invoices([square("nov", 818.12, "2026-11-01", "2026 11")])
        charges = [{"date": "2026-11-01", "amount": 818.12, "card_last4": "4805", "inferred": True}]
        self.assertFalse(self.store.record_inferred_charges("nov", charges, None, "2026-01-01T00:00:00+00:00"))
        self.assertTrue(self.store.record_inferred_charges("nov", charges, None, "2026-11-18T19:00:00+00:00"))
        self.assertFalse(self.store.record_inferred_charges("nov", charges, None, "2026-11-18T19:00:00+00:00"))


class CardsApiTests(ApiTestCase):
    def test_the_cards_dialog_lists_the_reissued_numbers_receipts_show(self):
        self.store.upsert_cards([{"last4": "5018"}, {"last4": "1610"}])
        self.store.upsert_invoices([inv("o1", "Amazon.ca", 40.0, "2026-07-20", month_folder="2026 07", card="5026")])
        cards = {c["last4"]: c for c in self.client.get("/api/cards").get_json()["cards"]}
        self.assertEqual(cards["5018"]["receipts_show"], ["5026"])
        self.assertEqual(cards["1610"]["receipts_show"], [])


if __name__ == "__main__":
    unittest.main()
