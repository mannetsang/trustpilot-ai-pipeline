"""Per-row Rematch: the dashboard's button that reads invoices newly filed in Drive for one
purchase's months and then looks for that purchase's invoice again (matching.match_transaction,
main.rematch_transaction, POST /api/transactions/<id>/rematch)."""
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import (MEMBER, ApiTestCase, FakeDrive, FakeGemini, facts_handler, file_, folder,  # noqa: E402
                           inv, invoice_tree, quiet, txn)
import invoices  # noqa: E402
import main  # noqa: E402
import matching  # noqa: E402
from store import MemoryStore, SupabaseStore  # noqa: E402

PAGE = {"X-Requested-With": "XMLHttpRequest"}


def store_with(transactions, invoices):
    store = MemoryStore()
    store.insert_transactions(transactions)
    store.upsert_invoices(invoices)
    return store


class MatchTransactionTests(unittest.TestCase):
    """The single-purchase matcher on its own, against an in-memory store."""

    def rematch(self, store, tid, gemini=None, **kwargs):
        return matching.match_transaction(store, gemini, tid, use_ai=gemini is not None, log=quiet, **kwargs)

    def test_finds_the_invoice_by_the_rules_and_records_who_asked(self):
        store = store_with([txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00)],
                           [inv("i1", "Anthropic", 150.00, "2026-09-03"), inv("i2", "Other Co", 20.00, "2026-09-03")])
        r = self.rematch(store, "t1", updated_by="kelly@superhairpieces.com")
        self.assertEqual((r["status"], r["invoice_id"], r["method"], r["changed"], r["previous_status"]),
                         ("matched", "i1", "rule", True, "missing"))
        self.assertEqual((r["invoice"]["vendor"], r["invoice"]["total"], r["invoices_considered"]), ("Anthropic", 150.0, 2))
        t = store.get_transaction("t1")
        self.assertEqual((t["invoice_status"], t["invoice_id"], t["match_method"], t["updated_by"]),
                         ("matched", "i1", "rule", "kelly@superhairpieces.com"))
        self.assertTrue(t["updated_at"])

    def test_nothing_found_stays_missing_and_writes_nothing(self):
        store = store_with([txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00)],
                           [inv("i1", "Other Co", 20.00, "2026-09-03")])
        r = self.rematch(store, "t1")
        self.assertEqual((r["status"], r["invoice_id"], r["changed"], r["invoice"]), ("missing", None, False, None))
        self.assertIsNone(store.get_transaction("t1")["updated_at"])

    def test_an_invoice_another_purchase_holds_is_not_taken(self):
        store = store_with([txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00),
                            txn("t2", "2026-09-05", "Anthropic* Claude Team", 150.00, status="matched",
                                invoice_id="i1", method="rule")],
                           [inv("i1", "Anthropic", 150.00, "2026-09-03")])
        r = self.rematch(store, "t1")
        self.assertEqual((r["status"], r["invoices_considered"]), ("missing", 0))
        self.assertEqual(store.get_transaction("t2")["invoice_id"], "i1")

    def test_a_correct_automatic_match_survives(self):
        store = store_with([txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00, status="matched",
                                invoice_id="i1", method="rule")],
                           [inv("i1", "Anthropic", 150.00, "2026-09-03")])
        store.update_transaction("t1", {"match_confidence": 0.99, "match_note": "x"})
        before = store.get_transaction("t1")
        r = self.rematch(store, "t1")
        self.assertEqual((r["status"], r["invoice_id"], r["previous_invoice_id"]), ("matched", "i1", "i1"))
        self.assertEqual(store.get_transaction("t1")["invoice_id"], before["invoice_id"])

    def test_a_refused_invoice_is_never_proposed_again(self):
        store = store_with([txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00)],
                           [inv("i1", "Anthropic", 150.00, "2026-09-03")])
        store.update_transaction("t1", {"rejected_invoice_ids": ["i1"]})
        self.assertEqual(self.rematch(store, "t1")["status"], "missing")

    def test_a_better_invoice_replaces_a_possible_match(self):
        # Filed later: the exact invoice; the possible one was 1% off from another month.
        store = store_with([txn("t1", "2026-09-10", "Amzn Mktp Ca", 45.10, status="possible", invoice_id="i_near",
                                method="rule")],
                           [inv("i_near", "Amazon", 45.50, "2026-09-09"), inv("i_exact", "Amazon", 45.10, "2026-09-09")])
        r = self.rematch(store, "t1")
        self.assertEqual((r["status"], r["invoice_id"], r["previous_status"], r["changed"]), ("matched", "i_exact", "possible", True))

    def test_only_the_months_around_the_purchase_are_searched(self):
        store = store_with([txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00)],
                           [inv("i_far", "Anthropic", 150.00, "2026-06-03", month_folder="2026 06"),
                            inv("i_next", "Anthropic", 150.00, "2026-09-05", month_folder="2026 10")])
        r = self.rematch(store, "t1")
        self.assertEqual((r["invoice_id"], r["invoices_considered"]), ("i_next", 1))

    def test_the_ai_judge_is_asked_when_the_rules_find_nothing(self):
        store = store_with([txn("t1", "2026-09-12", "Www.Retellai.Com", 96.12, source_amount=70.40, source_currency="USD")],
                           [inv("i_r", "Retell AI Inc", 70.40, "2026-09-11", currency="USD", vendor_folder="Retell"),
                            inv("i_o", "Other", 12.00, "2026-09-11")])
        gemini = FakeGemini(responses=[{"matches": [{"transaction_id": "t1", "invoice_id": "i_r", "confidence": 0.95,
                                                     "reason": "same vendor and USD amount"}]}])
        with mock.patch.object(matching, "rule_match", return_value={}):     # make the rules find nothing
            r = self.rematch(store, "t1", gemini=gemini)
        self.assertEqual((r["status"], r["invoice_id"], r["method"]), ("matched", "i_r", "ai"))
        self.assertTrue(r["note"].startswith("AI: "))
        self.assertEqual(len(gemini.calls), 1)
        prompt = gemini.prompt_text()
        self.assertIn('"id": "t1"', prompt)          # one purchase only
        self.assertNotIn('"id": "t2"', prompt)

    def test_the_ai_judge_may_not_overrule_the_amount(self):
        store = store_with([txn("t1", "2026-09-12", "Www.Retellai.Com", 96.12)],
                           [inv("i_r", "Retell AI Inc", 150.00, "2026-09-11")])
        gemini = FakeGemini(responses=[{"matches": [{"transaction_id": "t1", "invoice_id": "i_r", "confidence": 0.99, "reason": "x"}]}])
        self.assertEqual(self.rematch(store, "t1", gemini=gemini)["status"], "missing")

    def test_rows_that_cannot_be_rematched(self):
        store = store_with([txn("t_waived", "2026-09-04", "A", 1.0, status="waived"),
                            txn("t_fee", "2026-09-04", "Annual Card Fee", 120.0, type_="fee", status="not_required"),
                            txn("t_manual", "2026-09-04", "B", 2.0, status="matched", invoice_id="i1", method="manual")],
                           [inv("i1", "B", 2.0, "2026-09-03")])
        for tid, words in (("t_waived", "undo that first"), ("t_fee", "only purchases"), ("t_manual", "unlink it first"),
                           ("t_nope", "not found")):
            with self.assertRaises(matching.RematchError) as cm:
                self.rematch(store, tid)
            self.assertIn(words, str(cm.exception), tid)
        self.assertEqual(store.get_transaction("t_manual")["invoice_id"], "i1")


class RematchApiTests(ApiTestCase):
    """POST /api/transactions/<id>/rematch starts a job; the row polls it by id."""

    def wait(self, job_id):
        for _ in range(300):
            j = self.client.get(f"/api/jobs/current?id={job_id}").get_json()
            if j["finished"]:
                return j
            time.sleep(0.02)
        self.fail("the rematch job did not finish")

    def start(self, tid, headers=None):
        return self.client.post(f"/api/transactions/{tid}/rematch", headers=headers or {})

    def test_rematch_runs_as_a_job_and_returns_the_updated_row(self):
        self.upload()
        ids = self.ids()
        self.store.upsert_invoices([inv("inv_anth", "Anthropic", 150.00, "2026-09-03")])
        r = self.start(ids["anth"], headers={"X-Dashboard-User": "Kelly"})
        self.assertEqual(r.status_code, 202, r.get_json())
        job = r.get_json()["job"]
        self.assertEqual(job["name"], "rematch")         # it may already have finished: no Gemini in the tests
        j = self.wait(job["id"])
        self.assertIsNone(j["error"])
        res = j["result"]
        self.assertEqual((res["status"], res["invoice_id"], res["changed"]), ("matched", "inv_anth", True))
        self.assertTrue(res["read_skipped"].startswith("AI_DISABLED=1"))      # the tests run without Gemini
        row = res["transaction"]
        self.assertEqual((row["id"], row["invoice_status"], row["invoice"]["vendor"], row["editable"], row["updated_by"]),
                         (ids["anth"], "matched", "Anthropic", True, "Kelly"))
        self.assertNotIn("raw", row)
        self.assertEqual(j["progress"]["transaction_id"], ids["anth"])
        self.assertEqual(self.store.get_transaction(ids["anth"])["invoice_id"], "inv_anth")

    def test_refusals(self):
        self.upload()
        ids = self.ids()
        self.assertEqual(self.start("nope").status_code, 404)
        self.patch(ids["anth"], {"action": "waive", "reason": "lost"})
        r = self.start(ids["anth"])
        self.assertEqual((r.status_code, r.get_json()["error"]),
                         (400, "this purchase is marked as needing no invoice; undo that first"))
        self.assertEqual(self.start(ids["fee"]).status_code, 400)
        self.assertEqual(self.client.get(f"/api/transactions/{ids['amzn']}/rematch").status_code, 405)

    def test_busy_while_another_job_runs(self):
        self.upload()
        ids = self.ids()
        self.assertTrue(main._job_lock.acquire(blocking=False))
        try:
            r = self.start(ids["anth"])
            self.assertEqual((r.status_code, r.get_json()["status"]), (409, "busy"))
        finally:
            main._job_lock.release()

    def test_members_rematch_only_their_own_cards(self):
        self.upload()
        ids = self.ids()
        with mock.patch.object(main, "current_user", return_value=dict(MEMBER)):
            r = self.start(ids["anth"])
            self.assertEqual((r.status_code, r.get_json()["error"]), (403, "you can only rematch transactions of cards you own"))
        self.client.post("/api/cards", json={"last4": "1610", "label": "Yin", "owner_email": MEMBER["email"]})
        with mock.patch.object(main, "current_user", return_value=dict(MEMBER)):
            r = self.start(ids["anth"])
            self.assertEqual(r.status_code, 202, r.get_json())
            self.assertIsNone(self.wait(r.get_json()["job"]["id"])["error"])

    def test_behind_iap_the_page_header_is_required(self):
        self.upload()
        ids = self.ids()
        user = {"email": "ap@superhairpieces.com", "name": "", "picture": "", "role": "ap"}
        with mock.patch.object(main, "AUTH_DISABLED", False), mock.patch.object(main, "current_user", return_value=user):
            self.assertEqual(self.start(ids["anth"]).status_code, 403)
            r = self.start(ids["anth"], headers=PAGE)
            self.assertEqual(r.status_code, 202, r.get_json())
            self.wait(r.get_json()["job"]["id"])

    def test_an_invoice_filed_after_the_reminder_is_read_and_matched_in_one_click(self):
        """The usual case: the owner files the missing invoice in Drive, then clicks Rematch."""
        self.upload()
        ids = self.ids()
        tree = {"root": [folder("m09", "2026 09")], "m09": [folder("v_anth", "Anthropic")], "v_anth": []}
        drive = FakeDrive(tree)
        gemini = FakeGemini(handler=facts_handler)
        main.set_clients(drive=drive, gemini=gemini)
        self.addCleanup(lambda: main._clients.pop("gemini", None))
        with mock.patch.object(main, "AI_DISABLED", False), mock.patch.object(main, "INVOICE_FOLDER_ID", "root"):
            first = self.wait(self.start(ids["anth"]).get_json()["job"]["id"])["result"]
            self.assertEqual((first["status"], first["invoices_read"]), ("missing", 0))
            tree["v_anth"].append(file_("f_a", "a.pdf"))          # filed in Drive after the reminder
            second = self.wait(self.start(ids["anth"]).get_json()["job"]["id"])["result"]
        self.assertEqual((second["invoices_read"], second["status"], second["invoice_id"], second["method"]),
                         (1, "matched", "f_a", "rule"))
        self.assertIn("f_a", drive.downloads)
        self.assertEqual(self.store.get_transaction(ids["anth"])["invoice_id"], "f_a")



class RematchProgressTests(ApiTestCase):
    """The percentage the row shows: 0-5 listing Drive, 5-75 reading new invoices, 78 rules, 85 Gemini, 100 done."""

    def test_the_reader_reports_every_file_including_failures(self):
        tree, files_by_id = invoice_tree()
        drive = FakeDrive(tree, files_by_id, contents={"f_txt": b"Order 123 total 39.00"})
        calls = []
        result = invoices.index_invoices(MemoryStore(), drive, FakeGemini(handler=facts_handler), "root", limit=40,
                                         log=quiet, on_read=lambda *a: calls.append(a))
        batch, new = result["indexed"], 9
        self.assertEqual(calls[0], (0, batch, new))                   # announced before the first read
        self.assertEqual([c[0] for c in calls], list(range(0, batch + 1)))   # then once per file, failures too
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(calls[-1], (batch, batch, new))

    def test_the_total_holds_across_read_rounds(self):
        rounds = iter([8, 8, 4])                 # 20 new files read 8 at a time
        left = {"new": 20}

        def fake_index_month(year, month, limit=None, log=print, on_read=None):
            batch = next(rounds)
            on_read(0, batch, left["new"])
            for done in range(1, batch + 1):
                on_read(done, batch, left["new"])
            left["new"] -= batch
            return {"indexed": batch, "errors": [], "remaining": left["new"], "files_seen": 20, "month_folders": []}

        seen = []
        main._last_job["progress"] = {}
        with mock.patch.object(main, "index_month", fake_index_month):
            out = main.index_until_done(2026, 9, limit=8, on_read=lambda d, t: seen.append((d, t)))
        self.assertEqual(out["indexed"], 20)
        self.assertEqual({t for _, t in seen}, {20})                    # one total for the whole pass
        self.assertEqual([d for d, _ in seen], sorted(d for d, _ in seen))   # never goes back
        self.assertEqual(seen[-1], (20, 20))

    def test_the_percentage_climbs_through_every_stage_to_100(self):
        self.upload()
        ids = self.ids()
        self.store.upsert_invoices([inv("i_r", "Retell AI Inc", 70.40, "2026-09-11", currency="USD", vendor_folder="Retell")])
        main._last_job["progress"] = progress = {}
        trail = []

        def fake_index_until_done(year, month, limit=None, on_read=None):
            for done in range(5):
                on_read(done, 4) if done <= 4 else None
                trail.append(("read", progress.get("percent")))
            return {"indexed": 4, "errors": [], "remaining": 0}

        gemini = FakeGemini(handler=lambda parts, model: (trail.append(("ai", progress.get("percent"))) or
                                                          {"matches": [{"transaction_id": ids["retell"], "invoice_id": "i_r",
                                                                        "confidence": 0.95, "reason": "same vendor"}]}))
        main.set_clients(gemini=gemini)
        self.addCleanup(lambda: main._clients.pop("gemini", None))
        with mock.patch.object(main, "index_until_done", fake_index_until_done), \
                mock.patch.object(main, "AI_DISABLED", False), \
                mock.patch.object(matching, "rule_match", return_value={}):
            result = main.rematch_transaction(ids["retell"], "kelly@superhairpieces.com")
        self.assertEqual(result["status"], "matched")
        self.assertEqual([p for stage, p in trail if stage == "read"], [5, 22, 40, 57, 75])
        self.assertEqual([p for stage, p in trail if stage == "ai"], [85])
        self.assertEqual((progress["stage"], progress["percent"], progress["files_done"], progress["files_total"]),
                         ("done", 100, 4, 4))

    def test_nothing_new_to_read_jumps_to_the_matching_stage(self):
        self.upload()
        ids = self.ids()
        main._last_job["progress"] = progress = {}
        snapshots = []
        real_match = matching.match_transaction

        def spy(*args, **kwargs):
            snapshots.append(dict(progress))
            return real_match(*args, **kwargs)

        with mock.patch.object(main, "index_until_done", lambda y, m, limit=None, on_read=None: (on_read(0, 0), {"indexed": 0})[1]), \
                mock.patch.object(matching, "match_transaction", spy):
            main.rematch_transaction(ids["anth"])
        self.assertEqual((snapshots[0]["files_total"], snapshots[0]["percent"], snapshots[0]["stage"]), (0, 78, "match"))
        self.assertEqual(progress["percent"], 100)

    def test_a_finished_job_shows_100_and_the_files_it_read(self):
        """End to end through the API: the invoice filed after the reminder is read, then matched."""
        self.upload()
        ids = self.ids()
        tree = {"root": [folder("m09", "2026 09")], "m09": [folder("v_anth", "Anthropic")], "v_anth": [file_("f_a", "a.pdf")]}
        main.set_clients(drive=FakeDrive(tree), gemini=FakeGemini(handler=facts_handler))
        self.addCleanup(lambda: main._clients.pop("gemini", None))
        with mock.patch.object(main, "AI_DISABLED", False), mock.patch.object(main, "INVOICE_FOLDER_ID", "root"):
            r = self.client.post(f"/api/transactions/{ids['anth']}/rematch")
            job_id = r.get_json()["job"]["id"]
            for _ in range(300):
                j = self.client.get(f"/api/jobs/current?id={job_id}").get_json()
                if j["finished"]:
                    break
                time.sleep(0.02)
        p = j["progress"]
        self.assertEqual((p["stage"], p["percent"], p["files_done"], p["files_total"]), ("done", 100, 1, 1))
        self.assertEqual(j["result"]["status"], "matched")


def ai_matched(tid, invoice_id, confidence=0.92):
    """A purchase the Gemini judge matched earlier: its invoice is 8% below the charge (tax shown
    separately), which the rules never accept on their own."""
    t = txn(tid, "2026-09-12", "Sp Digital Goods", 50.00, status="matched", invoice_id=invoice_id, method="ai")
    t.update({"match_confidence": confidence, "match_note": "AI: same vendor, tax shown separately"})
    return t


def patreon(iid="i_p"):
    return inv(iid, "Patreon Inc", 46.00, "2026-09-11")


def judge_says(invoice_id, confidence=0.95, reason="same vendor"):
    return {"matches": [{"transaction_id": "t1", "invoice_id": invoice_id, "confidence": confidence, "reason": reason}]}


class RematchSafetyTests(unittest.TestCase):
    """Review fixes: a rematch never loses a good link and never overwrites a change made meanwhile."""

    def rematch(self, store, tid, gemini=None, **kwargs):
        return matching.match_transaction(store, gemini, tid, use_ai=gemini is not None, log=quiet, **kwargs)

    def test_the_fixture_is_out_of_the_rules_reach(self):
        store = store_with([txn("t1", "2026-09-12", "Sp Digital Goods", 50.00)], [patreon()])
        self.assertEqual(matching.rule_match([store.get_transaction("t1")], store.list_invoices()), {})

    def test_an_ai_match_is_kept_when_gemini_fails_or_is_off(self):
        def boom(parts, model):
            raise RuntimeError("Gemini unavailable")
        for gemini in (None, FakeGemini(handler=boom), FakeGemini(responses=[{"matches": []}])):
            store = store_with([ai_matched("t1", "i_p")], [patreon()])
            r = self.rematch(store, "t1", gemini=gemini)
            self.assertEqual((r["outcome"], r["status"], r["invoice_id"], r["changed"]), ("kept", "matched", "i_p", False))
            t = store.get_transaction("t1")
            self.assertEqual((t["invoice_status"], t["invoice_id"], t["match_method"], t["updated_at"]), ("matched", "i_p", "ai", None))

    def test_a_weak_rule_match_does_not_replace_a_good_ai_match(self):
        # The same amount from an unrelated vendor two days later: the rules call that "possible".
        store = store_with([ai_matched("t1", "i_p")], [patreon(), inv("i_other", "Unrelated Ltd", 50.00, "2026-09-14")])
        self.assertEqual(matching.rule_match([store.get_transaction("t1")], [store.get_invoice("i_other")])["t1"][1], "possible")
        r = self.rematch(store, "t1")
        self.assertEqual((r["outcome"], r["invoice_id"]), ("kept", "i_p"))

    def test_gemini_beats_a_weak_rule_match(self):
        store = store_with([txn("t1", "2026-09-12", "Sp Digital Goods", 50.00)],
                           [patreon(), inv("i_other", "Unrelated Ltd", 50.00, "2026-09-14")])
        r = self.rematch(store, "t1", gemini=FakeGemini(responses=[judge_says("i_p")]))
        # An inexact amount stays "possible" (a person confirms it), but it beats the rules' 0.6.
        self.assertEqual((r["outcome"], r["status"], r["invoice_id"], r["method"]), ("found", "possible", "i_p", "ai"))
        self.assertGreater(r["confidence"], 0.6)

    def test_a_change_made_while_gemini_thinks_is_never_overwritten(self):
        for change in ({"invoice_status": "waived", "match_method": "manual", "match_note": "waived: lost"},
                       {"invoice_status": "matched", "invoice_id": "i_other", "match_method": "manual"}):
            store = store_with([txn("t1", "2026-09-12", "Sp Digital Goods", 50.00)],
                               [patreon(), inv("i_other", "Someone Else", 1.00, "2026-09-11")])

            def meanwhile(parts, model, change=change):
                store.update_transaction("t1", change)           # a person clicks while the model runs
                return judge_says("i_p")

            r = self.rematch(store, "t1", gemini=FakeGemini(handler=meanwhile))
            self.assertEqual((r["outcome"], r["changed"]), ("conflict", False))
            t = store.get_transaction("t1")
            self.assertEqual((t["invoice_status"], t["invoice_id"]), (change["invoice_status"], change.get("invoice_id")))
            self.assertEqual((r["status"], r["invoice_id"]), (t["invoice_status"], t["invoice_id"]))   # reports the row as it is

    def test_an_invoice_linked_elsewhere_meanwhile_is_a_conflict_not_a_crash(self):
        store = store_with([txn("t1", "2026-09-12", "Sp Digital Goods", 50.00), txn("t2", "2026-09-20", "Other", 5.00)],
                           [patreon()])

        def meanwhile(parts, model):
            store.update_transaction("t2", {"invoice_status": "matched", "invoice_id": "i_p", "match_method": "manual"})
            return judge_says("i_p")

        r = self.rematch(store, "t1", gemini=FakeGemini(handler=meanwhile))
        self.assertEqual((r["outcome"], r["status"]), ("conflict", "missing"))
        self.assertEqual(store.get_transaction("t2")["invoice_id"], "i_p")

    def test_the_same_invoice_again_is_not_a_change(self):
        store = store_with([ai_matched("t1", "i_p")], [patreon()])
        gemini = FakeGemini(responses=[judge_says("i_p", 0.9, "worded differently this time")])
        r = self.rematch(store, "t1", gemini=gemini, updated_by="kelly@superhairpieces.com")
        self.assertEqual((r["outcome"], r["changed"], r["invoice_id"]), ("same", False, "i_p"))
        t = store.get_transaction("t1")
        self.assertEqual((t["match_note"], t["updated_by"], t["updated_at"]), ("AI: same vendor, tax shown separately", None, None))

    def test_a_possible_match_confirmed_by_the_rules_is_upgraded(self):
        store = store_with([txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00, status="possible", invoice_id="i1", method="rule")],
                           [inv("i1", "Anthropic", 150.00, "2026-09-03")])
        store.update_transaction("t1", {"match_confidence": 0.6})
        r = self.rematch(store, "t1")
        self.assertEqual((r["outcome"], r["status"], r["invoice_id"], r["changed"]), ("found", "matched", "i1", True))

    def test_an_invoice_removed_from_drive_is_let_go_when_nothing_replaces_it(self):
        store = store_with([ai_matched("t1", "i_p")], [patreon()])
        store.upsert_invoices([{"id": "i_p", "removed_at": "2026-10-13T00:00:00Z"}])
        r = self.rematch(store, "t1")
        self.assertEqual((r["outcome"], r["status"], r["invoice_id"], r["changed"]), ("none", "missing", None, True))

    def test_compare_and_set_in_both_stores(self):
        store = store_with([txn("t1", "2026-09-04", "A", 1.0)], [])
        self.assertIsNone(store.update_transaction("t1", {"note": "x"}, expect={"invoice_status": "matched"}))
        self.assertIsNone(store.get_transaction("t1")["note"])
        self.assertEqual(store.update_transaction("t1", {"note": "x"}, expect={"invoice_status": "missing", "invoice_id": None})["note"], "x")
        supa = SupabaseStore("https://example.supabase.co", "key")
        with mock.patch.object(supa, "patch", return_value=[]) as patch:
            self.assertIsNone(supa.update_transaction("t1", {"note": "x"}, expect={"invoice_status": "possible", "invoice_id": None,
                                                                                  "match_method": "rule"}))
        self.assertEqual(patch.call_args[0][1], {"id": "eq.t1", "invoice_status": "eq.possible", "invoice_id": "is.null",
                                                 "match_method": "eq.rule"})
        with mock.patch.object(supa, "patch", return_value=[{"id": "t1"}]) as patch:
            supa.update_transaction("t1", {"note": "x"})
        self.assertEqual(patch.call_args[0][1], {"id": "eq.t1"})         # no expect: exactly as before


class RematchJobEdgeTests(ApiTestCase):
    def wait(self, job_id):
        for _ in range(300):
            j = self.client.get(f"/api/jobs/current?id={job_id}").get_json()
            if j["finished"]:
                return j
            time.sleep(0.02)
        self.fail("the rematch job did not finish")

    def test_a_row_deleted_while_rematch_runs_fails_cleanly(self):
        self.upload()
        ids = self.ids()
        real = matching.match_transaction

        def delete_then_match(store, *args, **kwargs):
            out = real(store, *args, **kwargs)
            store.transactions.pop(ids["anth"])
            return out

        with mock.patch.object(matching, "match_transaction", delete_then_match):
            j = self.wait(self.client.post(f"/api/transactions/{ids['anth']}/rematch").get_json()["job"]["id"])
        self.assertEqual(j["error"], "the transaction was removed while Rematch was running")

    def test_files_that_could_not_be_read_are_named(self):
        self.upload()
        ids = self.ids()
        tree = {"root": [folder("m09", "2026 09")], "m09": [folder("v", "Anthropic")],
                "v": [file_("f_big", "big scan.pdf", size=20 * 1024 * 1024)]}
        main.set_clients(drive=FakeDrive(tree), gemini=FakeGemini(handler=facts_handler))
        self.addCleanup(lambda: main._clients.pop("gemini", None))
        with mock.patch.object(main, "AI_DISABLED", False), mock.patch.object(main, "INVOICE_FOLDER_ID", "root"):
            j = self.wait(self.client.post(f"/api/transactions/{ids['anth']}/rematch").get_json()["job"]["id"])
        r = j["result"]
        self.assertEqual((r["outcome"], r["read_errors"], r["read_error_files"]), ("none", 1, ["big scan.pdf"]))

if __name__ == "__main__":
    unittest.main()
