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


def fresh_process():
    """The alias cache as a new job process starts with: never read."""
    matching._card_aliases.update(map={}, at=0.0, store=None)


class ReviewFixTests(unittest.TestCase):
    """The cases the review of the reissue and split-total change reproduced."""

    def test_an_invoice_one_purchase_pays_whole_is_not_split_out_of_smaller_ones(self):
        # Three Amazon orders the same day, each paid by its own charge; the 49.98 lists its one charge.
        store = store_with_cards("5018")
        big = inv("big", "Amazon.ca", 49.98, "2026-11-10", month_folder="2026 11", card="5018")
        big["card_charges"] = [{"date": "2026-11-10", "amount": 49.98, "card_last4": "5018"}]
        store.upsert_invoices([big, inv("oa", "Amazon.ca", 24.99, "2026-11-10", month_folder="2026 11", card="5018"),
                               inv("ob", "Amazon.ca", 24.99, "2026-11-10", month_folder="2026 11", card="5018")])
        store.insert_transactions([txn("p_big", "2026-11-10", "AMZN Mktp CA*1", 49.98, card="5018"),
                                   txn("p_a", "2026-11-10", "AMZN Mktp CA*2", 24.99, card="5018"),
                                   txn("p_b", "2026-11-10", "AMZN Mktp CA*3", 24.99, card="5018")])
        matching.match_month(store, None, 2026, 11, **QUIET)
        self.assertEqual(store.get_transaction("p_big")["invoice_id"], "big")
        self.assertEqual({store.get_transaction(t)["invoice_id"] for t in ("p_a", "p_b")}, {"oa", "ob"})
        self.assertEqual(store.get_invoice("big")["card_charges"], big["card_charges"])

    def test_the_watch_filing_the_big_order_first_links_it_whole(self):
        store = store_with_cards("5018")
        store.insert_transactions([txn("p_big", "2026-11-10", "AMZN Mktp CA*1", 49.98, card="5018"),
                                   txn("p_a", "2026-11-10", "AMZN Mktp CA*2", 24.99, card="5018"),
                                   txn("p_b", "2026-11-10", "AMZN Mktp CA*3", 24.99, card="5018")])
        store.upsert_invoices([inv("big", "Amazon.ca", 49.98, "2026-11-10", month_folder="2026 11", card="5018")])
        matching.match_new_invoices(store, None, ["big"], **QUIET)
        self.assertEqual(store.get_transaction("p_big")["invoice_id"], "big")
        self.assertIsNone(store.get_invoice("big").get("card_charges"))

    def test_a_rematch_of_a_matched_purchase_keeps_its_invoice_whole(self):
        store = store_with_cards("1610")
        store.upsert_invoices([inv("x", "Freightcom", 100.00, "2026-11-03", month_folder="2026 11", card="1610")])
        store.insert_transactions([txn("p", "2026-11-03", "FREIGHTCOM INC", 100.00, status="matched",
                                       invoice_id="x", method="rule"),
                                   txn("h1", "2026-11-03", "FREIGHTCOM INC", 50.00),
                                   txn("h2", "2026-11-03", "FREIGHTCOM INC", 50.00)])
        out = matching.match_transaction(store, None, "p", **QUIET)
        self.assertIn(out["outcome"], ("same", "kept"))
        self.assertEqual(store.get_transaction("p")["invoice_id"], "x")
        self.assertIsNone(store.get_invoice("x").get("card_charges"))

    def test_a_rematch_never_splits_an_invoice_its_purchase_is_no_charge_of(self):
        # Month-wide the 100.00 is made two ways (60+40 on the 3rd, or with the 60 of the 4th).
        store = store_with_cards("1610")
        store.upsert_invoices([inv("x", "Freightcom", 100.00, "2026-11-03", month_folder="2026 11", card="1610")])
        store.insert_transactions([txn("a", "2026-11-03", "FREIGHTCOM INC", 60.00),
                                   txn("b", "2026-11-03", "FREIGHTCOM INC", 40.00),
                                   txn("c", "2026-11-04", "FREIGHTCOM INC", 60.00),
                                   txn("st", "2026-11-01", "STAPLES 123", 25.00)])
        matching.match_transaction(store, None, "st", **QUIET)
        matching.match_transaction(store, None, "b", **QUIET)
        self.assertIsNone(store.get_invoice("x").get("card_charges"))

    def test_a_split_is_judged_on_the_purchases_of_the_month_before_too(self):
        store = store_with_cards("1610")
        store.upsert_invoices([inv("x", "Freightcom", 100.00, "2026-11-01", month_folder="2026 11", card="1610")])
        store.insert_transactions([txn("p", "2026-10-31", "FREIGHTCOM INC", 60.00),
                                   txn("q", "2026-11-01", "FREIGHTCOM INC", 40.00),
                                   txn("r", "2026-11-01", "FREIGHTCOM INC", 60.00)])
        matching.match_month(store, None, 2026, 11, **QUIET)
        self.assertIsNone(store.get_invoice("x").get("card_charges"))

    def test_an_invoice_showing_no_card_is_not_made_up_across_cards(self):
        store = store_with_cards("1610", "2887", "4421")
        store.upsert_invoices([inv("ic", "Staples", 100.00, "2026-11-05", month_folder="2026 11")])
        store.insert_transactions([txn("a", "2026-11-05", "STAPLES 123", 60.00, card="1610"),
                                   txn("b", "2026-11-05", "STAPLES 123", 40.00, card="2887"),
                                   txn("c", "2026-11-05", "STAPLES 123", 100.00, card="4421")])
        matching.match_new_invoices(store, None, ["ic"], **QUIET)
        self.assertIsNone(store.get_invoice("ic").get("card_charges"))
        self.assertEqual(store.get_transaction("c")["invoice_id"], "ic")
        other = store_with_cards("1610", "2887")
        other.upsert_invoices([inv("ic", "Staples", 100.00, "2026-11-05", month_folder="2026 11")])
        other.insert_transactions([txn("a", "2026-11-05", "STAPLES 123", 60.00, card="1610"),
                                   txn("b", "2026-11-05", "STAPLES 123", 40.00, card="2887")])
        matching.match_month(other, None, 2026, 11, **QUIET)
        self.assertIsNone(other.get_invoice("ic").get("card_charges"))

    def test_a_rematch_leaves_two_invoices_needing_one_purchase_for_a_person(self):
        # A's only set is {x, y}, B's is {y, z}: the month run records neither, and so must a click.
        for clicked in ("x", "z"):
            store = store_with_cards("1610")
            store.upsert_invoices([inv("A", "Freightcom", 100.00, "2026-11-03", month_folder="2026 11", card="1610"),
                                   inv("B", "Freightcom", 70.00, "2026-11-03", month_folder="2026 11", card="1610")])
            store.insert_transactions([txn("x", "2026-11-03", "FREIGHTCOM INC", 60.00),
                                       txn("y", "2026-11-03", "FREIGHTCOM INC", 40.00),
                                       txn("z", "2026-11-03", "FREIGHTCOM INC", 30.00)])
            matching.match_transaction(store, None, clicked, **QUIET)
            self.assertEqual((store.get_invoice("A").get("card_charges"), store.get_invoice("B").get("card_charges")),
                             (None, None), clicked)

    def test_the_watch_weighs_older_invoices_a_purchase_is_the_whole_of(self):
        store = store_with_cards("4805")
        store.upsert_invoices([square("rcpt", 237.30, "2026-12-01", "2026 12")])     # filed earlier, unlinked
        store.insert_transactions([sq("t1", "2026-12-01", 237.30), sq("t2", "2026-12-01", 45.20),
                                   sq("t3", "2026-12-01", 535.62)])
        store.upsert_invoices([square("tax", 818.12, "2026-12-01", "2026 12")])
        matching.match_new_invoices(store, None, ["tax"], **QUIET)
        self.assertIsNone(store.get_invoice("tax").get("card_charges"))

    def test_a_card_added_by_a_statement_is_its_own_card_in_the_next_match(self):
        store = store_with_cards("4805")                    # 4821 is 4805's +2 number for now
        store.upsert_cards([{"last4": "4821"}])             # a statement brings a real card 4821
        store.upsert_invoices([square("r", 99.00, "2026-11-01", "2026 11", card="4805")])
        store.insert_transactions([sq("t", "2026-11-01", 99.00, card="4821")])
        matching.match_month(store, None, 2026, 11, **QUIET)
        self.assertNotEqual(store.get_transaction("t")["invoice_status"], "matched")

    def test_a_fresh_process_moves_a_reissue_link_to_its_charge_on_a_reread(self):
        store = MemoryStore()
        store.upsert_cards([{"last4": "5018"}])
        o1 = inv("o1", "Amazon.ca", 40.00, "2026-07-20", month_folder="2026 07", card="5026")
        store.upsert_invoices([o1])
        store.insert_transactions([txn("c", "2026-07-21", "AMZN Mktp CA*3", 40.00, card="5018", status="matched",
                                       invoice_id="o1", method="rule")])
        fresh_process()
        reread = dict(o1, card_charges=[{"date": "2026-07-21", "amount": 40.00, "card_last4": "5026"},
                                        {"date": "2026-07-25", "amount": 12.00, "card_last4": "5026"}])
        store.upsert_invoices([reread])
        self.assertEqual(matching.split_whole_links(store, reread, log=lambda *a: None), (1, 0))
        c = store.get_transaction("c")
        self.assertEqual((c["invoice_status"], c["invoice_id"], c["invoice_part"]), ("matched", "o1", 0))


class ReReadTests(unittest.TestCase):
    """A split invoice read again (renamed, a new Drive version) through the Invoice folder index."""

    def setUp(self):
        self.store = store_with_cards("4805")
        self.store.upsert_invoices([square("nov", 818.12, "2026-11-01", "2026 11")])
        self.store.insert_transactions([sq("t1", "2026-11-01", 237.30), sq("t2", "2026-11-01", 45.20),
                                        sq("t3", "2026-11-01", 535.62)])
        matching.match_month(self.store, None, 2026, 11, **QUIET)
        self.charges = self.store.get_invoice("nov")["card_charges"]
        self.assertEqual(len(self.charges), 3)

    def reread(self, **reading):
        import invoices as invoice_index
        from unittest import mock
        old = self.store.get_invoice("nov")
        rec = {k: old[k] for k in ("id", "file_name", "web_view_link", "folder_path", "month_folder", "vendor_folder")}
        rec.update(mime_type="application/pdf", modified_time="2026-11-19T10:00:00Z", size=1000)
        row = dict(old, modified_time=rec["modified_time"], extracted_at="2026-11-19T10:01:00+00:00",
                   **dict({"card_charges": []}, **reading))
        with mock.patch.object(invoice_index, "walk", lambda *a, **k: [dict(rec)]), \
                mock.patch.object(invoice_index, "extract_invoice", lambda *a, **k: dict(row)):
            invoice_index.index_invoices(self.store, None, object(), "root", month_folders=["2026 11"],
                                         log=lambda *a: None)
        return {t: self.store.get_transaction(t) for t in ("t1", "t2", "t3")}

    def test_a_reread_that_agrees_keeps_the_charges_and_the_links(self):
        rows = self.reread(card_charges=[{"date": "2026-11-01", "amount": 818.12, "card_last4": "4813"}])
        self.assertEqual(self.store.get_invoice("nov")["card_charges"], self.charges)
        self.assertTrue(all(r["invoice_status"] == "matched" and r["invoice_id"] == "nov" for r in rows.values()))

    def test_a_reread_with_another_total_lets_the_links_go_for_the_matcher(self):
        rows = self.reread(total=900.00)
        self.assertEqual(self.store.get_invoice("nov")["card_charges"], [])
        self.assertTrue(all(r["invoice_status"] == "missing" and r["invoice_id"] is None for r in rows.values()))


class CardsApiTests(ApiTestCase):
    def test_the_cards_dialog_lists_the_reissued_numbers_receipts_show(self):
        self.store.upsert_cards([{"last4": "5018"}, {"last4": "1610"}])
        self.store.upsert_invoices([inv("o1", "Amazon.ca", 40.0, "2026-07-20", month_folder="2026 07", card="5026")])
        cards = {c["last4"]: c for c in self.client.get("/api/cards").get_json()["cards"]}
        self.assertEqual(cards["5018"]["receipts_show"], ["5026"])
        self.assertEqual(cards["1610"]["receipts_show"], [])


if __name__ == "__main__":
    unittest.main()
