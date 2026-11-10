"""The Invoice folder watch: invoices filed under Invoice/YYYY MM/<vendor>/ since the last import run
(by the e-mail filer, by hand, or from the dashboard) are read and matched by the next run, against the
purchases still waiting for one, without taking any link away."""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_bank_sync import NOW, BankDrive, BankSyncTestCase  # noqa: E402
from test_pipeline import FakeGemini, file_, file_name_in, folder, inv, txn  # noqa: E402
import main  # noqa: E402
import matching  # noqa: E402
from drive import DriveError  # noqa: E402

FILED = "2026-11-02T14:00:00.000Z"

FACTS = {
    "Invoice-RET-1.pdf": {"is_invoice": True, "vendor": "Retell AI", "invoice_number": "RET-1",
                          "invoice_date": "2026-10-05", "total": 70.40, "currency": "USD", "card_last4": None,
                          "payment_method": None, "summary": "voice agent minutes"},
    "Invoice-ANTH-1.pdf": {"is_invoice": True, "vendor": "Anthropic", "invoice_number": "A-1",
                           "invoice_date": "2026-09-04", "total": 150.0, "currency": "CAD", "card_last4": None,
                           "payment_method": None, "summary": "Claude Team"},
    "ChatGPT Plus Receipt.pdf": {"is_invoice": True, "vendor": "OpenAI", "invoice_number": "O-1",
                                 "invoice_date": "2026-09-10", "total": 21.00, "currency": "USD", "card_last4": None,
                                 "payment_method": None, "summary": "ChatGPT Plus"},
    "Invoice-NOV.pdf": {"is_invoice": True, "vendor": "Anthropic", "invoice_number": "A-9",
                        "invoice_date": "2026-11-01", "total": 150.0, "currency": "CAD", "card_last4": None,
                        "payment_method": None, "summary": "Claude Team"},
}


class WatchDrive(BankDrive):
    """BankDrive plus parents on get (the watch walks up from a file) and a change feed: `feed` is
    the list of changes in order, a page token is "t<position>"."""

    def __init__(self, tree, **kw):
        super().__init__(tree, **kw)
        self.feed, self.token_calls, self.fail_changes = [], [], None

    def get(self, file_id, fields=None):
        if file_id == "bank":
            return {"id": "bank", "name": "Bank Transactions", "mimeType": main.FOLDER}
        parent, entry = self._find(file_id)
        if entry is None and file_id in self.tree:
            return {"id": file_id, "name": file_id, "mimeType": main.FOLDER, "parents": []}
        got = super().get(file_id, fields)
        if parent:
            got["parents"] = [parent]
        return got

    def start_page_token(self):
        self.token_calls.append("start")
        return f"t{len(self.feed)}"

    def changes(self, token, max_pages=20):
        self.token_calls.append(token)
        if self.fail_changes:
            exc, self.fail_changes = self.fail_changes, None
            raise exc
        start = int(token[1:])
        return [dict(c) for c in self.feed[start:]], f"t{len(self.feed)}"

    # helpers for the tests
    def file_in(self, parent, entry):
        self.tree.setdefault(parent, []).append(entry)
        self.feed.append({"fileId": entry["id"], "removed": False,
                          "file": dict(entry, parents=[parent], trashed=False)})

    def move(self, file_id, new_parent):
        parent, entry = self._find(file_id)
        self.tree[parent].remove(entry)
        self.tree.setdefault(new_parent, []).append(entry)
        self.feed.append({"fileId": file_id, "removed": False, "file": dict(entry, parents=[new_parent], trashed=False)})


class InvoiceWatchTestCase(BankSyncTestCase):
    def setUp(self):
        super().setUp()
        self.tree.clear()
        self.tree.update({
            "bank": [],
            "inv_root": [folder("m09", "2026 09"), folder("m10", "2026 10"), folder("m11", "2026 11"),
                         folder("misc", "Misc")],
            "m09": [folder("v09_anth", "Anthropic"), folder("v09_openai", "OpenAI")],
            "v09_anth": [], "v09_openai": [],
            "m10": [folder("v10_retell", "Retell AI")], "v10_retell": [],
            "m11": [folder("v11_anth", "Anthropic")], "v11_anth": [],
            "misc": [],
            "outside": [],               # EmailattatchmentOriginal: not under Invoice
        })
        self.drive = WatchDrive(self.tree)
        self.judge_prompts, self.judge_answer = [], (lambda text: {"matches": []})

        def handler(parts, model):
            text = "\n".join(p.get("text", "") for p in parts)
            if "TRANSACTIONS:" in text and "INVOICES:" in text:
                self.judge_prompts.append(text)
                return self.judge_answer(text)
            return dict(FACTS.get(file_name_in(parts), {"is_invoice": False, "summary": "not an invoice"}))

        self.gemini = FakeGemini(handler=handler)
        main.set_clients(store=self.store, drive=self.drive, gemini=self.gemini)
        self.more = [mock.patch.object(main, "INVOICE_FOLDER_ID", "inv_root"),
                     mock.patch.object(main, "AI_DISABLED", False),
                     mock.patch.object(main, "INVOICE_WATCH_ON", True)]
        for p in self.more:
            p.start()

    def tearDown(self):
        for p in self.more:
            p.stop()
        super().tearDown()

    def watch(self):
        return self.store.get_state(main.INVOICE_WATCH) or {}

    def row(self, tid):
        return self.store.get_transaction(tid)

    def baseline(self):
        """A first run with nothing filed yet: the feed starts here."""
        self.sync()
        self.assertEqual(self.watch()["token"], f"t{len(self.drive.feed)}")
        self.drive.downloads.clear()


class FirstRunTests(InvoiceWatchTestCase):
    def test_the_first_run_starts_the_feed_and_reads_this_month_and_the_one_before(self):
        self.tree["v10_retell"].append(file_("f_ret", "Invoice-RET-1.pdf", modified=FILED))
        self.tree["v09_anth"].append(file_("f_anth", "Invoice-ANTH-1.pdf", modified=FILED))
        self.store.insert_transactions([
            txn("t_ret", "2026-10-06", "RETELL AI", 96.12, source_amount=70.40, source_currency="USD")])
        result = self.sync()
        state = self.watch()
        self.assertEqual(state["token"], "t0")
        self.assertEqual((state["folders_pending"], state["to_match"]), ([], {}))
        self.assertEqual(self.drive.downloads, ["f_ret"])                  # 2026 09 is not part of the catch-up
        row = self.row("t_ret")
        self.assertEqual((row["invoice_status"], row["invoice_id"], row["match_method"]), ("matched", "f_ret", "rule"))
        self.assertEqual((state["last"]["read"], state["last"]["matched"]), (1, 1))
        self.assertEqual(result["invoice_folders_left"], 0)
        self.assertIsNone(self.store.get_lease(main.LEASE_NAME)["holder"])

    def test_the_status_endpoint_reports_the_watch(self):
        self.tree["v10_retell"].append(file_("f_ret", "Invoice-RET-1.pdf", modified=FILED))
        self.store.insert_transactions([
            txn("t_ret", "2026-10-06", "RETELL AI", 96.12, source_amount=70.40, source_currency="USD")])
        self.sync()
        body = self.client.get("/api/bank-imports").get_json()
        self.assertTrue(body["invoices"]["enabled"])
        self.assertEqual(body["invoices"]["last"]["matched"], 1)
        self.assertEqual(body["invoices"]["folders_pending"], [])

    def test_the_watch_can_be_turned_off(self):
        with mock.patch.object(main, "INVOICE_WATCH_ON", False):
            self.assertEqual(self.sync()["status"], "nothing new")
        self.assertEqual(self.drive.token_calls, [])
        self.assertEqual(self.watch(), {})


class LaterRunTests(InvoiceWatchTestCase):
    def test_an_invoice_filed_later_is_read_and_matched_on_the_next_run(self):
        self.baseline()
        self.store.insert_transactions([txn("t_anth", "2026-09-04", "Anthropic* Claude Team", 150.00)])
        self.drive.file_in("v09_anth", file_("f_anth", "Invoice-ANTH-1.pdf", modified=FILED))
        self.drive.file_in("outside", file_("f_mail", "Invoice-ANTH-1.pdf", modified=FILED))   # the e-mail copy
        self.sync(NOW + dt.timedelta(minutes=15))
        row = self.row("t_anth")
        self.assertEqual((row["invoice_status"], row["invoice_id"]), ("matched", "f_anth"))
        self.assertEqual(self.drive.downloads, ["f_anth"])                 # the copy outside Invoice is not read
        self.assertEqual(self.watch()["token"], "t2")
        self.assertEqual(self.watch()["folders_pending"], [])

    def test_a_change_outside_the_invoice_folder_is_left_alone(self):
        self.baseline()
        self.drive.file_in("outside", file_("f_mail", "attachment.pdf", modified=FILED))
        self.drive.file_in("misc", file_("f_misc", "Invoice-ANTH-1.pdf", modified=FILED))   # in no month folder
        result = self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(result["status"], "nothing new")
        self.assertEqual(self.drive.downloads, [])
        self.assertEqual(self.watch()["folders_pending"], [])

    def test_an_invoice_in_a_month_far_from_the_purchase_is_not_used(self):
        self.baseline()
        self.store.insert_transactions([txn("t_anth", "2026-09-04", "Anthropic* Claude Team", 150.00)])
        self.drive.file_in("v11_anth", file_("f_nov", "Invoice-NOV.pdf", modified=FILED))
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.row("t_anth")["invoice_status"], "missing")    # September draws on Aug-Oct only
        self.assertEqual(self.judge_prompts, [])

    def test_nothing_is_taken_away_and_a_possible_match_is_upgraded(self):
        self.baseline()
        self.store.upsert_invoices([inv("x_old", "Anthropic", 150.0, "2026-08-20"),
                                    inv("z_manual", "Anthropic", 150.0, "2026-09-04")])
        self.tree["v09_anth"].extend([file_("x_old", "x_old.pdf"), file_("z_manual", "z_manual.pdf")])
        p1 = dict(txn("t_possible", "2026-09-04", "Anthropic* Claude Team", 150.00, status="possible",
                      invoice_id="x_old", method="ai"), match_confidence=0.7)
        p2 = txn("t_manual", "2026-09-04", "Anthropic* Claude Team", 150.00, status="matched",
                 invoice_id="z_manual", method="manual")
        self.store.insert_transactions([p1, p2])
        self.drive.file_in("v09_anth", file_("f_anth", "Invoice-ANTH-1.pdf", modified=FILED))
        self.sync(NOW + dt.timedelta(minutes=15))
        up = self.row("t_possible")
        self.assertEqual((up["invoice_status"], up["invoice_id"], up["match_method"]), ("matched", "f_anth", "rule"))
        self.assertNotIn("x_old", self.store.used_invoice_ids())             # the old possible invoice is let go
        kept = self.row("t_manual")
        self.assertEqual((kept["invoice_status"], kept["invoice_id"], kept["match_method"]),
                         ("matched", "z_manual", "manual"))

    def test_the_judge_sees_only_the_purchases_a_new_invoice_could_document(self):
        self.baseline()
        self.store.insert_transactions([
            txn("t_gpt", "2026-09-11", "OPENAI *CHATGPT SUBSCR", 28.40, source_amount=20.00, source_currency="USD"),
            txn("t_tim", "2026-09-11", "Tim Hortons #123", 5.25),
            txn("t_costco", "2026-09-12", "Costco Wholesale", 88.00),
        ])
        self.judge_answer = lambda text: {"matches": [
            {"transaction_id": "t_gpt", "invoice_id": "f_gpt", "confidence": 0.9, "reason": "ChatGPT Plus, tax added"},
            {"transaction_id": "t_costco", "invoice_id": "f_gpt", "confidence": 0.9, "reason": "a forced pair"}]}
        self.drive.file_in("v09_openai", file_("f_gpt", "ChatGPT Plus Receipt.pdf", modified=FILED))
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(len(self.judge_prompts), 1)
        prompt = self.judge_prompts[0]
        self.assertIn("t_gpt", prompt)
        self.assertNotIn("t_tim", prompt)
        self.assertNotIn("t_costco", prompt)
        row = self.row("t_gpt")
        # 21.00 against 20.00: within what the judge may say, but only a person confirms an inexact amount.
        self.assertEqual((row["invoice_status"], row["invoice_id"], row["match_method"]), ("possible", "f_gpt", "ai"))
        self.assertEqual(self.row("t_costco")["invoice_status"], "missing")


class RobustnessTests(InvoiceWatchTestCase):
    def test_a_busy_run_leaves_its_place_in_the_feed_and_the_next_run_reads_the_change(self):
        self.baseline()
        self.store.insert_transactions([txn("t_anth", "2026-09-04", "Anthropic* Claude Team", 150.00)])
        self.drive.file_in("v09_anth", file_("f_anth", "Invoice-ANTH-1.pdf", modified=FILED))
        self.assertIsNotNone(self.store.take_lease(main.LEASE_NAME, "service:rev:1", "match", 300))
        self.assertEqual(self.sync(NOW + dt.timedelta(minutes=15))["status"], "busy")
        # Only a run holding the lease reads the feed or writes the watch's state.
        self.assertEqual((self.watch()["folders_pending"], self.watch()["token"]), ([], "t0"))
        self.store.release_lease(main.LEASE_NAME, "service:rev:1")
        self.sync(NOW + dt.timedelta(minutes=30))
        self.assertEqual(self.row("t_anth")["invoice_status"], "matched")
        self.assertEqual(self.watch()["folders_pending"], [])

    def test_invoices_read_but_not_matched_are_matched_on_the_next_run(self):
        self.baseline()
        self.store.insert_transactions([txn("t_anth", "2026-09-04", "Anthropic* Claude Team", 150.00)])
        self.drive.file_in("v09_anth", file_("f_anth", "Invoice-ANTH-1.pdf", modified=FILED))
        real = matching.match_new_invoices
        with mock.patch.object(matching, "match_new_invoices", side_effect=RuntimeError("Vertex timed out")):
            self.sync(NOW + dt.timedelta(minutes=15))           # noted, not raised
        self.assertEqual((self.watch()["to_match"], self.watch()["folders_pending"]), ({"f_anth": 1}, []))
        self.assertIn("Vertex timed out", self.watch()["error"])
        self.assertIsNone(self.store.get_lease(main.LEASE_NAME)["holder"])
        with mock.patch.object(matching, "match_new_invoices", side_effect=real):
            self.sync(NOW + dt.timedelta(minutes=30))
        self.assertEqual(self.row("t_anth")["invoice_status"], "matched")
        self.assertEqual(self.watch()["to_match"], {})

    def test_a_file_moved_out_of_the_invoice_folder_is_marked_removed(self):
        self.baseline()
        self.drive.file_in("v09_anth", file_("f_anth", "Invoice-ANTH-1.pdf", modified=FILED))
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertIsNone(self.store.get_invoice("f_anth").get("removed_at"))
        self.drive.move("f_anth", "outside")
        self.sync(NOW + dt.timedelta(minutes=30))
        self.assertTrue(self.store.get_invoice("f_anth").get("removed_at"))

    def test_an_expired_page_token_starts_the_feed_again(self):
        self.baseline()
        self.drive.fail_changes = DriveError("Drive GET changes -> 400 Invalid Value")
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertIsNone(self.watch()["token"])
        self.assertIn("400", self.watch()["error"])
        self.sync(NOW + dt.timedelta(minutes=30))
        self.assertEqual(self.watch()["token"], "t0")
        self.assertEqual(self.watch()["error"], "")

    def test_a_drive_failure_keeps_the_token_for_the_next_run(self):
        self.baseline()
        self.drive.fail_changes = DriveError("Drive GET changes -> 503 backend error")
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.watch()["token"], "t0")


class ReviewFixTests(InvoiceWatchTestCase):
    """The cases the review of the first version found."""

    def test_a_pair_that_would_not_be_kept_does_not_take_the_invoice_from_another_purchase(self):
        self.baseline()
        self.store.upsert_invoices([inv("x_old", "Stripe", 150.0, "2026-09-03")])
        self.tree["m09"].append(folder("v09_stripe", "Stripe"))
        self.tree["v09_stripe"] = [file_("x_old", "x_old.pdf")]              # in Drive, unchanged since read
        weak = dict(txn("t_paypal", "2026-09-04", "PAYPAL *PAYMENTS", 150.00, status="possible",
                        invoice_id="x_old", method="rule"), match_confidence=0.6)
        self.store.insert_transactions([weak, txn("t_anth", "2026-09-19", "Anthropic* Claude Team", 150.00)])
        self.drive.file_in("v09_anth", file_("f_anth", "Invoice-ANTH-1.pdf", modified=FILED))
        self.sync(NOW + dt.timedelta(minutes=15))
        row = self.row("t_anth")
        self.assertEqual((row["invoice_status"], row["invoice_id"], row["match_method"]), ("matched", "f_anth", "rule"))
        self.assertEqual(self.row("t_paypal")["invoice_id"], "x_old")
        self.assertEqual(self.judge_prompts, [])

    def test_an_invoice_the_judge_could_not_place_is_tried_again(self):
        self.baseline()
        self.store.insert_transactions([
            txn("t_gpt", "2026-09-11", "OPENAI *CHATGPT SUBSCR", 28.40, source_amount=20.00, source_currency="USD")])
        self.drive.file_in("v09_openai", file_("f_gpt", "ChatGPT Plus Receipt.pdf", modified=FILED))

        def down(text):
            raise RuntimeError("Vertex 503")
        self.judge_answer = down
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.row("t_gpt")["invoice_status"], "missing")
        self.assertEqual(self.watch()["to_match"], {"f_gpt": 1})
        self.judge_answer = lambda text: {"matches": [
            {"transaction_id": "t_gpt", "invoice_id": "f_gpt", "confidence": 0.9, "reason": "ChatGPT Plus"}]}
        self.sync(NOW + dt.timedelta(minutes=30))
        self.assertEqual((self.row("t_gpt")["invoice_status"], self.row("t_gpt")["invoice_id"]), ("possible", "f_gpt"))
        self.assertEqual(self.watch()["to_match"], {})

    def test_the_watch_gives_up_after_a_few_runs(self):
        self.baseline()
        self.store.insert_transactions([
            txn("t_gpt", "2026-09-11", "OPENAI *CHATGPT SUBSCR", 28.40, source_amount=20.00, source_currency="USD")])
        self.drive.file_in("v09_openai", file_("f_gpt", "ChatGPT Plus Receipt.pdf", modified=FILED))

        def down(text):
            raise RuntimeError("Vertex 503")
        self.judge_answer = down
        for n in range(main.INVOICE_MAX_TRIES):
            self.sync(NOW + dt.timedelta(minutes=15 * (n + 1)))
        self.assertEqual(self.watch()["to_match"], {})
        self.assertEqual(len(self.judge_prompts), main.INVOICE_MAX_TRIES)

    def test_a_possible_match_to_an_invoice_gone_from_drive_gives_way_to_an_equal_one(self):
        self.baseline()
        gone = dict(inv("x_gone", "Anthropic", 150.0, "2026-09-04"), removed_at="2026-11-01T00:00:00Z")
        self.store.upsert_invoices([gone])
        held = dict(txn("t_anth", "2026-09-04", "Anthropic* Claude Team", 150.00, status="possible",
                        invoice_id="x_gone", method="rule"), match_confidence=0.99)
        self.store.insert_transactions([held])
        self.drive.file_in("v09_anth", file_("f_anth", "Invoice-ANTH-1.pdf", modified=FILED))
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.row("t_anth")["invoice_id"], "f_anth")

    def test_invoices_stored_by_an_earlier_round_are_matched_even_when_a_later_round_fails(self):
        self.baseline()
        self.store.insert_transactions([txn("t_anth", "2026-09-04", "Anthropic* Claude Team", 150.00)])
        self.drive.file_in("v09_anth", file_("f_anth", "Invoice-ANTH-1.pdf", modified=FILED))
        self.drive.file_in("v09_anth", file_("f_other", "receipt.pdf", modified=FILED))
        real = main.invoice_index.index_invoices
        calls = []

        def second_round_fails(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise DriveError("Drive GET files -> 500 backend error")
            return real(*args, **kwargs)

        with mock.patch.object(main, "INDEX_BATCH", 1), \
                mock.patch.object(main.invoice_index, "index_invoices", side_effect=second_round_fails):
            self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(set(self.watch()["to_match"]) | {"f_anth"}, {"f_anth", "f_other"})
        self.assertEqual(self.watch()["folders_pending"], ["2026 09"])          # read again next run
        self.sync(NOW + dt.timedelta(minutes=30))
        self.assertEqual(self.row("t_anth")["invoice_id"], "f_anth")

    def test_an_invoice_moved_to_the_right_month_is_matched_there(self):
        self.baseline()
        self.store.insert_transactions([
            txn("t_ret", "2026-10-06", "RETELL AI", 96.12, source_amount=70.40, source_currency="USD")])
        self.tree["m08"] = [folder("v08_retell", "Retell AI")]
        self.tree["v08_retell"] = []
        self.tree["inv_root"].append(folder("m08", "2026 08"))
        self.drive.file_in("v08_retell", file_("f_ret", "Invoice-RET-1.pdf", modified=FILED))
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.row("t_ret")["invoice_status"], "missing")    # August serves Jul-Sep only
        self.drive.move("f_ret", "v10_retell")
        self.sync(NOW + dt.timedelta(minutes=30))
        self.assertEqual(self.store.get_invoice("f_ret")["month_folder"], "2026 10")
        self.assertEqual((self.row("t_ret")["invoice_status"], self.row("t_ret")["invoice_id"]), ("matched", "f_ret"))

    def test_an_invoice_read_by_someone_elses_rematch_first_is_still_matched(self):
        self.baseline()
        self.store.insert_transactions([
            txn("t_ret", "2026-10-06", "RETELL AI", 96.12, source_amount=70.40, source_currency="USD")])
        self.drive.file_in("v10_retell", file_("f_ret", "Invoice-RET-1.pdf", modified=FILED))
        main.invoice_index.index_invoices(self.store, self.drive, self.gemini, "inv_root", month_folders=["2026 10"])
        self.assertIsNotNone(self.store.get_invoice("f_ret"))
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.row("t_ret")["invoice_id"], "f_ret")

    def test_a_passing_drive_error_on_the_way_up_keeps_the_place_in_the_feed(self):
        self.baseline()
        self.store.insert_transactions([txn("t_anth", "2026-09-04", "Anthropic* Claude Team", 150.00)])
        self.drive.file_in("v09_anth", file_("f_anth", "Invoice-ANTH-1.pdf", modified=FILED))
        real_get, failures = self.drive.get, []

        def flaky(file_id, fields=None):
            if file_id == "v09_anth" and not failures:
                failures.append(1)
                raise DriveError("Drive GET files/v09_anth -> 429 rateLimitExceeded")
            return real_get(file_id, fields)

        self.drive.get = flaky
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.watch()["token"], "t0")
        self.assertIn("429", self.watch()["error"])
        self.sync(NOW + dt.timedelta(minutes=30))
        self.assertEqual(self.row("t_anth")["invoice_id"], "f_anth")

    def test_a_file_whose_read_failed_is_read_again_on_the_next_run(self):
        self.baseline()
        self.store.insert_transactions([txn("t_anth", "2026-09-04", "Anthropic* Claude Team", 150.00)])
        self.drive.file_in("v09_anth", file_("f_anth", "Invoice-ANTH-1.pdf", modified=FILED))
        handler, failures = self.gemini.handler, []

        def flaky(parts, model):
            if "TRANSACTIONS:" not in "".join(p.get("text", "") for p in parts) and not failures:
                failures.append(1)
                raise RuntimeError("Vertex 429 resource exhausted")
            return handler(parts, model)

        self.gemini.handler = flaky
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.row("t_anth")["invoice_status"], "missing")
        self.assertEqual(self.watch()["folders_pending"], ["2026 09"])
        self.sync(NOW + dt.timedelta(minutes=30))
        self.assertEqual(self.row("t_anth")["invoice_id"], "f_anth")

    def test_the_line_counts_the_whole_run_not_its_last_folder(self):
        self.tree["v11_anth"].append(file_("f_nov", "Invoice-NOV.pdf", modified=FILED))
        self.tree["v10_retell"].append(file_("f_ret", "Invoice-RET-1.pdf", modified=FILED))
        self.store.insert_transactions([
            txn("t_ret", "2026-10-06", "RETELL AI", 96.12, source_amount=70.40, source_currency="USD")])
        self.sync()
        last = self.watch()["last"]
        self.assertEqual((last["read"], last["matched"]), (2, 1))


class SecondReviewTests(InvoiceWatchTestCase):
    """The cases the second review found."""

    def test_a_vendor_folder_moved_into_the_right_month_is_matched_there(self):
        self.baseline()
        self.store.insert_transactions([
            txn("t_ret", "2026-10-06", "RETELL AI", 96.12, source_amount=70.40, source_currency="USD")])
        self.tree["inv_root"].append(folder("m08", "2026 08"))
        self.tree["m08"] = [folder("v08_retell", "Retell AI")]
        self.tree["v08_retell"] = []
        self.drive.file_in("v08_retell", file_("f_ret", "Invoice-RET-1.pdf", modified=FILED))
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.row("t_ret")["invoice_status"], "missing")
        self.drive.move("v08_retell", "m10")                   # the whole folder: one change, for the folder
        self.sync(NOW + dt.timedelta(minutes=30))
        self.assertEqual(self.store.get_invoice("f_ret")["month_folder"], "2026 10")
        self.assertEqual(self.row("t_ret")["invoice_id"], "f_ret")

    def test_a_step_that_keeps_failing_ends_and_the_statement_months_still_run(self):
        self.baseline()
        self.drive.file_in("v09_anth", file_("f_anth", "Invoice-ANTH-1.pdf", modified=FILED))
        main.update_sync_state(self.store, months_pending=[[2026, 10]])
        matched = []
        with mock.patch.object(main.invoice_index, "index_invoices",
                               side_effect=RuntimeError("Postgres refused the batch")), \
                mock.patch.object(main, "index_month", return_value={}), \
                mock.patch.object(main, "match_month", side_effect=lambda y, m, **k: matched.append((y, m)) or {}):
            for n in range(main.INVOICE_MAX_TRIES):
                self.sync(NOW + dt.timedelta(minutes=15 * (n + 1)))
        self.assertEqual(matched, [(2026, 10)])                 # on the first run, despite the failing step
        self.assertEqual(self.watch()["folders_pending"], [])   # let go after INVOICE_MAX_TRIES runs
        self.assertEqual(self.watch()["folder_failures"], {})

    def test_a_judge_answer_given_as_one_object_is_read(self):
        self.baseline()
        self.store.insert_transactions([
            txn("t_gpt", "2026-09-11", "OPENAI *CHATGPT SUBSCR", 28.40, source_amount=20.00, source_currency="USD")])
        self.judge_answer = lambda text: {"matches": {"transaction_id": "t_gpt", "invoice_id": "f_gpt",
                                                      "confidence": 0.9, "reason": "ChatGPT Plus"}}
        self.drive.file_in("v09_openai", file_("f_gpt", "ChatGPT Plus Receipt.pdf", modified=FILED))
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.row("t_gpt")["invoice_id"], "f_gpt")

    def test_one_reading_the_database_refuses_does_not_lose_the_batch(self):
        self.baseline()
        self.store.insert_transactions([txn("t_anth", "2026-09-04", "Anthropic* Claude Team", 150.00)])
        self.drive.file_in("v09_anth", file_("f_anth", "Invoice-ANTH-1.pdf", modified=FILED))
        self.drive.file_in("v09_anth", file_("f_bad", "Invoice-RET-1.pdf", modified=FILED))
        real = self.store.upsert_invoices

        def refuse_bad(rows):
            if any(r["id"] == "f_bad" and r.get("total") is not None for r in rows):
                raise main.StoreError('POST invoices -> 400 {"code":"22003","message":"numeric field overflow"}')
            return real(rows)

        with mock.patch.object(self.store, "upsert_invoices", side_effect=refuse_bad):
            self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.row("t_anth")["invoice_id"], "f_anth")
        bad = self.store.get_invoice("f_bad")
        self.assertTrue(bad["extraction_error"].startswith("unsupported: the reading could not be stored"))
        self.assertEqual(self.watch()["folders_pending"], [])

    def test_a_possible_match_to_an_invoice_refiled_far_away_gives_way(self):
        self.baseline()
        self.store.upsert_invoices([inv("x_far", "Stripe", 150.0, "2026-09-03", month_folder="2026 11")])
        self.tree["v11_anth"].append(file_("x_far", "x_far.pdf"))
        held = dict(txn("t_paypal", "2026-09-06", "PAYPAL *PAYMENTS", 150.00, status="possible",
                        invoice_id="x_far", method="rule"), match_confidence=0.65)
        self.store.insert_transactions([held])
        self.drive.file_in("v09_anth", file_("f_anth", "Invoice-ANTH-1.pdf", modified=FILED))
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.row("t_paypal")["invoice_id"], "f_anth")


class MatchNewInvoicesTests(unittest.TestCase):
    """matching.match_new_invoices on its own."""

    def setUp(self):
        self.store = main.MemoryStore()

    def test_an_invoice_already_held_is_not_offered_again(self):
        self.store.upsert_invoices([inv("i1", "Anthropic", 150.0, "2026-09-04")])
        self.store.insert_transactions([
            txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00, status="matched", invoice_id="i1", method="rule"),
            txn("t2", "2026-09-05", "Anthropic* Claude Team", 150.00)])
        counts = matching.match_new_invoices(self.store, None, ["i1"], use_ai=False)
        self.assertEqual(counts["invoices"], 0)
        self.assertEqual(self.store.get_transaction("t2")["invoice_status"], "missing")

    def test_a_row_changed_meanwhile_is_left_alone(self):
        self.store.upsert_invoices([inv("i1", "Anthropic", 150.0, "2026-09-04")])
        self.store.insert_transactions([txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00)])
        real = self.store.update_transaction

        def someone_linked_first(tid, patch, expect=None):
            real(tid, {"invoice_status": "waived"})
            return real(tid, patch, expect=expect)

        with mock.patch.object(self.store, "update_transaction", side_effect=someone_linked_first):
            counts = matching.match_new_invoices(self.store, None, ["i1"], use_ai=False)
        self.assertEqual((counts["skipped"], counts["matched"]), (1, 0))
        self.assertEqual(self.store.get_transaction("t1")["invoice_status"], "waived")


if __name__ == "__main__":
    unittest.main()
