"""Payments the Sunday review (Nov 2026) had to pair by hand: a billing centre's weekly invoice charged weeks
late (UPS 449087, Purolator 3190186), one charge paying several invoices (UPS brokerage pairs, three
Purolator weeks at once), statements that list the card payments they record (Google Ads, Cigna), and
payment confirmations taking the place of the invoice they confirm (UPS "Payment Confirmation" e-mails)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import ApiTestCase, FakeDrive, FakeGemini, inv, quiet, txn  # noqa: E402
import invoices as invoice_index  # noqa: E402
import main  # noqa: E402
import matching  # noqa: E402
from store import MemoryStore, StoreError  # noqa: E402

UPS = "UPS CANADA BILLING C"
QUIET = dict(use_ai=False, log=quiet)


def ups(iid, total, date, number=None, doc="invoice", paid=None, vendor="UPS"):
    row = inv(iid, vendor, total, date, month_folder=f"{date[:4]} {date[5:7]}", vendor_folder=vendor)
    row.update(document_type=doc, invoice_number=number, paid_invoice_numbers=paid)
    return row


def charge(tid, date, amount, description=UPS, card="4431", **kw):
    return txn(tid, date, description, amount, card=card, **kw)


def store_of(invoices, charges, reach="2026-10-21", since="2026-01-02"):
    """A store whose card 4431 statements run from `since` to `reach` (a line on each day), as the live one's
    did; None leaves that end at the charges themselves."""
    store = MemoryStore()
    store.upsert_cards([{"last4": "4431"}, {"last4": "4516"}])
    store.upsert_invoices(invoices)
    ends = [charge(tid, day, 3.33, "COFFEE") for tid, day in (("since", since), ("reach", reach)) if day]
    store.insert_transactions(list(charges) + ends)
    return store


def run_months(store, *months):
    for y, m in months:
        matching.match_month(store, None, y, m, **QUIET)


class LatePaymentTests(unittest.TestCase):
    def test_a_weekly_invoice_charged_two_months_late_is_its_payment(self):
        store = store_of([ups("may9", 4953.56, "2026-05-09")], [charge("t", "2026-07-08", 4953.56)])
        run_months(store, (2026, 7))
        t = store.get_transaction("t")
        self.assertEqual((t["invoice_status"], t["invoice_id"], t["match_confidence"]), ("matched", "may9", 0.9))
        self.assertIn("paid 60 days after the invoice date", t["match_note"])

    def test_not_when_the_vendor_bills_that_amount_again(self):
        store = store_of([ups("may9", 4953.56, "2026-05-09"), ups("may16", 4953.56, "2026-05-16")],
                         [charge("t", "2026-07-08", 4953.56)])
        run_months(store, (2026, 7))
        self.assertEqual(store.get_transaction("t")["invoice_status"], "missing")

    def test_not_when_another_charge_has_that_amount(self):
        store = store_of([ups("may9", 4953.56, "2026-05-09")],
                         [charge("t", "2026-07-08", 4953.56), charge("u", "2026-07-15", 4953.56)])
        run_months(store, (2026, 7))
        self.assertEqual({store.get_transaction(x)["invoice_status"] for x in ("t", "u")}, {"missing"})

    def test_only_possible_until_the_cards_statements_cover_the_whole_window(self):
        # The last statement line is the charge itself (Jul 8), short of May 9 + 70 days, or the first one is
        # after Apr 18: a second charge of the amount could be in a statement not imported. A person confirms.
        for ends in (dict(reach=None), dict(since="2026-05-01")):
            store = store_of([ups("may9", 4953.56, "2026-05-09")], [charge("t", "2026-07-08", 4953.56)], **ends)
            run_months(store, (2026, 7))
            t = store.get_transaction("t")
            self.assertEqual((t["invoice_status"], t["invoice_id"]), ("possible", "may9"), ends)
            self.assertIn("do not cover the whole window", t["match_note"])

    def test_a_payment_confirmation_of_the_amount_is_no_second_bill(self):
        store = store_of([ups("may9", 4953.56, "2026-05-09"),
                          ups("conf", 4953.56, "2026-07-07", doc="payment_confirmation", paid=[])],
                         [charge("t", "2026-07-08", 4953.56)])
        run_months(store, (2026, 7))
        t = store.get_transaction("t")
        self.assertEqual((t["invoice_id"], t["also_invoice_ids"]), ("may9", ["conf"]))

    def test_beyond_the_reach_it_is_left_alone(self):
        store = store_of([ups("apr1", 4953.56, "2026-04-01")], [charge("t", "2026-07-08", 4953.56)])
        run_months(store, (2026, 7))
        self.assertEqual(store.get_transaction("t")["invoice_status"], "missing")      # 98 days

    def test_the_invoice_watch_and_the_rematch_find_it_too(self):
        store = store_of([ups("may9", 4953.56, "2026-05-09")], [charge("t", "2026-07-08", 4953.56)])
        self.assertIn((2026, 7), matching.months_to_match(store.get_invoice("may9")))
        counts = matching.match_new_invoices(store, None, ["may9"], **QUIET)
        self.assertEqual(counts["matched"], 1)
        store = store_of([ups("may9", 4953.56, "2026-05-09")], [charge("t", "2026-07-08", 4953.56)])
        out = matching.match_transaction(store, None, "t", **QUIET)
        self.assertEqual((out["outcome"], out["invoice_id"], out["status"]), ("found", "may9", "matched"))


class CombinedTests(unittest.TestCase):
    def test_a_weeks_brokerage_invoices_charged_together(self):
        # UPS bills a 20.23 fee most weeks: three 20.23 invoices make 137.54 with 117.31, but only the one
        # dated the same Wednesday was charged with it.
        store = store_of([ups("b117", 117.31, "2026-08-05"), ups("f0805", 20.23, "2026-08-05"),
                          ups("f0722", 20.23, "2026-07-22"), ups("f0812", 20.23, "2026-08-12")],
                         [charge("t", "2026-08-24", 137.54)])
        run_months(store, (2026, 8))
        t = store.get_transaction("t")
        self.assertEqual((t["invoice_status"], t["invoice_id"], t["also_invoice_ids"]), ("matched", "b117", ["f0805"]))
        self.assertIn("one payment of 2 invoices from UPS", t["match_note"])
        self.assertEqual(store.used_invoice_parts()["f0805"], {"whole"})

    def test_weekly_invoices_missed_and_paid_in_one_charge(self):
        store = store_of([ups("aug15", 4135.45, "2026-08-15"), ups("sep5", 3205.63, "2026-09-05")],
                         [charge("t", "2026-10-01", 7341.08)])
        run_months(store, (2026, 10))
        t = store.get_transaction("t")
        self.assertEqual((t["invoice_id"], t["also_invoice_ids"]), ("aug15", ["sep5"]))

    def test_two_ways_to_make_the_amount_and_none_on_one_day_is_left_for_a_person(self):
        store = store_of([ups("a", 10.00, "2026-08-01"), ups("b", 20.00, "2026-08-02"),
                          ups("c", 15.00, "2026-08-03"), ups("d", 15.00, "2026-08-04")],
                         [charge("t", "2026-08-20", 30.00)])
        run_months(store, (2026, 8))
        self.assertEqual(store.get_transaction("t")["invoice_status"], "missing")

    def test_another_vendors_invoices_never_make_up_the_amount(self):
        store = store_of([ups("a", 100.00, "2026-08-05"), ups("b", 37.54, "2026-08-05", vendor="Purolator")],
                         [charge("t", "2026-08-24", 137.54)])
        run_months(store, (2026, 8))
        self.assertEqual(store.get_transaction("t")["invoice_status"], "missing")

    def test_a_new_invoice_completes_a_pair_and_the_rematch_finds_one(self):
        store = store_of([ups("b117", 117.31, "2026-08-05")], [charge("t", "2026-08-24", 137.54)])
        store.upsert_invoices([ups("f0805", 20.23, "2026-08-05")])
        counts = matching.match_new_invoices(store, None, ["f0805"], **QUIET)
        self.assertEqual(counts["matched"], 1)
        self.assertEqual(store.get_transaction("t")["also_invoice_ids"], ["f0805"])
        store = store_of([ups("b117", 117.31, "2026-08-05"), ups("f0805", 20.23, "2026-08-05")],
                         [charge("t", "2026-08-24", 137.54)])
        out = matching.match_transaction(store, None, "t", **QUIET)
        self.assertEqual((out["invoice_id"], out["also_invoice_ids"]), ("b117", ["f0805"]))


class VendorEvidenceTests(unittest.TestCase):
    def test_the_folder_named_in_the_descriptor_or_a_history_of_links(self):
        ups_charge = charge("t", "2026-08-24", 137.54)
        self.assertTrue(matching.folder_named(ups_charge, ups("a", 1.0, "2026-08-05")))
        self.assertFalse(matching.folder_named(ups_charge, ups("b", 1.0, "2026-08-05", vendor="Canada Post")))
        self.assertTrue(matching.folder_named(charge("f", "2026-08-07", 1.0, "FEDEX95845920"),
                                              ups("c", 1.0, "2026-08-05", vendor="FedEx")))
        water = charge("w", "2026-08-07", 42.92, "PRIMO WATER NORTH AM")
        springs = ups("s", 42.92, "2026-07-16", vendor="Canadian Springs")
        store = store_of([springs, ups("s1", 10.0, "2026-05-02", vendor="Canadian Springs"),
                          ups("s2", 11.0, "2026-06-02", vendor="Canadian Springs")],
                         [water, charge("p1", "2026-05-20", 10.0, "PRIMO WATER NORTH AM", status="matched",
                                        invoice_id="s1", method="rule")])
        self.assertFalse(matching.VendorHistory(store)(water, springs))          # one link before: not enough
        store.update_transaction("p1", {"invoice_status": "matched"})
        store.insert_transactions([charge("p2", "2026-06-20", 11.0, "PRIMO WATER NORTH AM", status="matched",
                                          invoice_id="s2", method="manual")])
        self.assertTrue(matching.VendorHistory(store)(water, springs))

    def test_a_canada_post_pair_never_pays_a_ups_charge(self):
        store = store_of([ups("a", 100.00, "2026-08-05", vendor="Canada Post"),
                          ups("b", 37.54, "2026-08-05", vendor="Canada Post")],
                         [charge("t", "2026-08-24", 137.54)])
        run_months(store, (2026, 8))
        self.assertEqual(store.get_transaction("t")["invoice_status"], "missing")


class StatementTests(unittest.TestCase):
    def ads(self, charges, iid="ads_may"):
        row = inv(iid, "Google Ads", 1500.0, "2026-05-31", month_folder="2026 05", vendor_folder="Google Ads",
                  currency="USD", is_invoice=False)
        row.update(document_type="statement", card_charges=[{"date": d, "amount": a, "card_last4": "4516"}
                                                            for d, a in charges])
        return row

    def test_each_payment_a_statement_lists_pays_its_charge(self):
        store = store_of([self.ads([("2026-05-01", 329.10), ("2026-05-12", 500.0), ("2026-05-20", 500.0)])],
                         [charge(t, d, a, "GOOGLE *ADS4167500770", card="4516", currency="USD")
                          for t, d, a in (("p1", "2026-05-01", 329.10), ("p2", "2026-05-12", 500.0),
                                          ("p3", "2026-05-21", 500.0))])
        run_months(store, (2026, 5))
        parts = {t: (store.get_transaction(t)["invoice_id"], store.get_transaction(t)["invoice_part"])
                 for t in ("p1", "p2", "p3")}
        self.assertEqual(parts, {"p1": ("ads_may", 0), "p2": ("ads_may", 1), "p3": ("ads_may", 2)})

    def test_a_statement_never_takes_a_charge_from_another_vendor_or_from_a_receipt(self):
        card_statement = dict(self.ads([("2026-05-03", 42.0), ("2026-05-04", 17.5)], iid="visa"),
                              vendor="RBC Visa", vendor_folder="RBC", file_name="visa.pdf")
        receipt = inv("r", "Google Ads", 500.0, "2026-05-12", month_folder="2026 05", vendor_folder="Google Ads",
                      currency="USD")
        store = store_of([card_statement, receipt, self.ads([("2026-05-12", 500.0), ("2026-05-20", 500.0)])],
                         [charge("c1", "2026-05-03", 42.0, "STAPLES", card="4516", currency="USD"),
                          charge("c2", "2026-05-12", 500.0, "GOOGLE *ADS4167500770", card="4516", currency="USD")])
        run_months(store, (2026, 5))
        self.assertEqual(store.get_transaction("c1")["invoice_status"], "missing")       # Staples is no RBC
        self.assertEqual(store.get_transaction("c2")["invoice_id"], "r")                 # the receipt first

    def test_a_statement_listing_no_payments_is_still_no_candidate(self):
        self.assertFalse(matching.is_candidate(self.ads([])))
        self.assertFalse(matching.is_candidate(self.ads([("2026-05-01", 329.10)])))
        self.assertTrue(matching.is_candidate(self.ads([("2026-05-01", 329.10), ("2026-05-12", 500.0)])))


class ConfirmationTests(unittest.TestCase):
    def invoices(self):
        return [ups("inv", 2448.33, "2026-09-19", number="0000449087386"),
                ups("conf", 2448.33, "2026-10-05", doc="payment_confirmation", paid=[])]

    def test_a_charge_closest_to_its_confirmation_is_linked_to_the_invoice(self):
        store = store_of(self.invoices(), [charge("t", "2026-10-06", 2448.33)])
        run_months(store, (2026, 10))
        t = store.get_transaction("t")
        self.assertEqual((t["invoice_status"], t["invoice_id"], t["also_invoice_ids"]), ("matched", "inv", ["conf"]))

    def test_a_charge_the_rules_linked_to_the_confirmation_moves_to_the_invoice(self):
        store = store_of(self.invoices(), [charge("t", "2026-10-06", 2448.33, status="matched", invoice_id="conf",
                                                  method="rule")])
        run_months(store, (2026, 10))
        t = store.get_transaction("t")
        self.assertEqual((t["invoice_id"], t["also_invoice_ids"], t["match_method"]), ("inv", ["conf"], "rule"))

    def test_a_link_made_by_hand_is_never_touched(self):
        store = store_of(self.invoices(), [charge("t", "2026-10-06", 2448.33, status="matched", invoice_id="conf",
                                                  method="manual")])
        run_months(store, (2026, 10))
        t = store.get_transaction("t")
        self.assertEqual((t["invoice_id"], t["also_invoice_ids"]), ("conf", []))

    def test_a_confirmation_naming_three_invoices_gives_all_three(self):
        weeks = [ups("w1", 2408.19, "2026-08-15", "520308843", vendor="Purolator"),
                 ups("w2", 2349.07, "2026-08-22", "550302169", vendor="Purolator"),
                 ups("w3", 2391.93, "2026-08-29", "535326565", vendor="Purolator"),
                 ups("rcpt", 7149.19, "2026-09-30", "041767", doc="payment_confirmation",
                     paid=["520308843", "550302169", "535326565"], vendor="Purolator")]
        store = store_of(weeks, [charge("t", "2026-10-01", 7149.19, "PUROLATOR")])
        run_months(store, (2026, 10))
        t = store.get_transaction("t")
        self.assertEqual((t["invoice_id"], t["also_invoice_ids"]), ("w1", ["rcpt", "w2", "w3"]))

    def test_a_new_confirmation_is_kept_with_the_invoice_already_matched(self):
        store = store_of([self.invoices()[0]], [charge("t", "2026-10-06", 2448.33)])
        run_months(store, (2026, 10))
        self.assertEqual(store.get_transaction("t")["invoice_id"], "inv")
        store.upsert_invoices([self.invoices()[1]])
        counts = matching.match_new_invoices(store, None, ["conf"], **QUIET)
        self.assertEqual(counts.get("confirmed"), 1)
        self.assertEqual(store.get_transaction("t")["also_invoice_ids"], ["conf"])

    def test_without_its_invoice_the_confirmation_stays_the_link(self):
        store = store_of([self.invoices()[1]], [charge("t", "2026-10-06", 2448.33)])
        run_months(store, (2026, 10))
        t = store.get_transaction("t")
        self.assertEqual((t["invoice_status"], t["invoice_id"], t["also_invoice_ids"]), ("matched", "conf", []))


class StoreRuleTests(unittest.TestCase):
    def setUp(self):
        self.store = store_of([ups("a", 10.0, "2026-08-01"), ups("b", 20.0, "2026-08-01"), ups("c", 5.0, "2026-08-01")],
                              [charge("t", "2026-08-20", 30.0), charge("u", "2026-08-21", 20.0)], reach=None)

    def test_a_document_paid_with_one_charge_is_nobody_elses(self):
        self.store.update_transaction("t", {"invoice_status": "matched", "invoice_id": "b", "also_invoice_ids": ["a"]})
        with self.assertRaises(StoreError):
            self.store.update_transaction("u", {"invoice_status": "matched", "invoice_id": "a"})
        with self.assertRaises(StoreError):
            self.store.update_transaction("u", {"invoice_status": "matched", "invoice_id": "c", "also_invoice_ids": ["a"]})
        with self.assertRaises(StoreError):
            self.store.update_transaction("u", {"invoice_status": "matched", "invoice_id": "c", "also_invoice_ids": ["c"]})
        self.assertEqual(self.store.used_invoice_parts(), {"a": {"whole"}, "b": {"whole"}})

    def test_a_new_link_or_none_starts_without_them_and_they_are_kept_sorted(self):
        self.store.update_transaction("t", {"invoice_id": "b", "also_invoice_ids": ["c", "a", "c"]})
        self.assertEqual(self.store.get_transaction("t")["also_invoice_ids"], ["a", "c"])
        self.store.update_transaction("t", {"invoice_id": "a"})
        self.assertEqual(self.store.get_transaction("t")["also_invoice_ids"], [])

    def test_a_row_being_matched_again_frees_its_own(self):
        self.store.update_transaction("t", {"invoice_status": "possible", "invoice_id": "b", "also_invoice_ids": ["a"]})
        taken = matching.taken_parts(self.store, [self.store.get_transaction("t")])
        self.assertEqual(taken, {})


class ReleaseTests(unittest.TestCase):
    def linked(self, method="rule"):
        store = store_of([ups("b117", 117.31, "2026-08-05"), ups("f0805", 20.23, "2026-08-05"),
                          ups("conf", 137.54, "2026-08-23", doc="payment_confirmation", paid=[])],
                         [charge("t", "2026-08-24", 137.54)])
        store.update_transaction("t", {"invoice_status": "matched", "invoice_id": "b117", "match_method": method,
                                       "also_invoice_ids": ["f0805", "conf"]})
        return store

    def test_a_confirmation_gone_is_let_go(self):
        store = self.linked()
        store.update_invoices(["conf"], {"removed_at": "2026-11-23T00:00:00Z"})
        self.assertEqual(matching.release_stale_also(store, ["conf"], quiet), 1)
        t = store.get_transaction("t")
        self.assertEqual((t["invoice_id"], t["also_invoice_ids"]), ("b117", ["f0805"]))

    def test_an_invoice_paid_with_it_gone_sends_the_charge_back(self):
        store = self.linked()
        store.update_invoices(["f0805"], {"removed_at": "2026-11-23T00:00:00Z"})
        matching.release_stale_also(store, ["f0805"], quiet)
        t = store.get_transaction("t")
        self.assertEqual((t["invoice_status"], t["invoice_id"], t["also_invoice_ids"]), ("missing", None, []))

    def test_a_link_made_by_hand_stays(self):
        store = self.linked(method="manual")
        store.update_invoices(["f0805"], {"removed_at": "2026-11-23T00:00:00Z"})
        self.assertEqual(matching.release_stale_also(store, ["f0805"], quiet), 0)


class CheckTests(unittest.TestCase):
    def test_invoices_paid_together_are_compared_with_the_charge_by_their_sum(self):
        t = dict(charge("t", "2026-08-24", 137.54), match_method="manual")
        main_ = ups("b117", 117.31, "2026-08-05")
        self.assertEqual(matching.invoice_checks(t, main_, also=[ups("f0805", 20.23, "2026-08-05")]), [])
        warned = matching.invoice_checks(t, main_)
        self.assertEqual([c["kind"] for c in warned], ["amount"])

    def test_an_invoice_paid_late_is_news_a_receipt_dated_that_far_is_not(self):
        t = charge("t", "2026-07-08", 4953.56)
        late = matching.invoice_checks(t, ups("may9", 4953.56, "2026-05-09"))
        self.assertEqual([(c["kind"], c["level"]) for c in late], [("date", "info")])
        receipt = matching.invoice_checks(t, ups("may9", 4953.56, "2026-05-09", doc="receipt"))
        self.assertEqual([(c["kind"], c["level"]) for c in receipt], [("date", "warn")])

    def test_a_payment_a_statement_lists_is_a_receipt(self):
        t = dict(charge("p1", "2026-05-01", 329.10, card="4516", currency="USD"), invoice_part=0)
        statement = dict(inv("s", "Google Ads", 329.10, "2026-05-01", currency="USD", is_invoice=False),
                         document_type="statement")
        self.assertEqual(matching.invoice_checks(t, statement), [])


class ApiTests(ApiTestCase):
    def test_unlink_refuses_the_whole_set_and_the_row_shows_its_documents(self):
        self.store.upsert_cards([{"last4": "4431"}])
        self.store.upsert_invoices([ups("b117", 117.31, "2026-08-05"), ups("f0805", 20.23, "2026-08-05")])
        self.store.insert_transactions([charge("t", "2026-08-24", 137.54)])
        self.store.update_transaction("t", {"invoice_status": "matched", "invoice_id": "b117", "match_method": "rule",
                                            "also_invoice_ids": ["f0805"]})
        rows = self.client.get("/api/transactions?year=2026&month=8").get_json()["transactions"]
        row = next(r for r in rows if r["id"] == "t")
        self.assertEqual([a["id"] for a in row["also_invoices"]], ["f0805"])
        r = self.client.patch("/api/transactions/t", json={"action": "unlink"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        t = self.store.get_transaction("t")
        self.assertEqual((t["invoice_id"], t["also_invoice_ids"], sorted(t["rejected_invoice_ids"])),
                         (None, [], ["b117", "f0805"]))


class ReadingTests(unittest.TestCase):
    def read(self, facts):
        gemini = FakeGemini([facts])
        record = {"id": "f1", "file_name": "2026-10-05_UPS_email.pdf", "mime_type": "application/pdf",
                  "web_view_link": "", "folder_path": "/2026 10/UPS", "month_folder": "2026 10", "vendor_folder": "UPS",
                  "modified_time": "2026-11-20T17:00:00Z", "size": 10,
                  "_file": {"id": "f1", "mimeType": "application/pdf", "name": "x.pdf"}}
        return invoice_index.extract_invoice(gemini, FakeDrive({}), record)

    def test_a_payment_confirmation_keeps_the_invoices_it_names(self):
        row = self.read({"is_invoice": True, "document_type": "payment confirmation", "vendor": "UPS", "total": 7341.08,
                         "currency": "CAD", "invoice_date": "2026-09-30",
                         "paid_invoice_numbers": ["#0000449087336", "0000449087366", "0000449087366", ""]})
        self.assertEqual((row["document_type"], row["paid_invoice_numbers"]),
                         ("payment_confirmation", ["0000449087336", "0000449087366"]))

    def test_anything_else_names_none_but_is_marked_as_read(self):
        row = self.read({"is_invoice": True, "document_type": "invoice", "vendor": "UPS", "total": 20.23,
                         "currency": "CAD", "invoice_date": "2026-09-30", "paid_invoice_numbers": ["123"]})
        self.assertEqual((row["document_type"], row["paid_invoice_numbers"]), ("invoice", []))
        self.assertIn("payment_confirmation", invoice_index.INVOICE_PROMPT)
        self.assertIn("paid_invoice_numbers", invoice_index.INVOICE_PROMPT)


if __name__ == "__main__":
    unittest.main()
