"""Regression tests for the review findings: PostgREST payload shaping, the
two-phase re-linking in match_month, rejected pairs, AI sanity checks,
files removed from Drive, upload removal and error handling."""

import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import ApiTestCase, FakeGemini, inv, quiet, txn  # noqa: E402  (sets the env and imports main)

import invoices  # noqa: E402
import matching  # noqa: E402
import store as store_module  # noqa: E402
from store import MemoryStore, StoreError, SupabaseStore  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, body="[]", headers=None):
        self.status_code, self.text, self.headers = status, body, headers or {}

    def json(self):
        return json.loads(self.text)


class SupabaseRequestShapingTests(unittest.TestCase):
    """What goes over the wire to PostgREST, without PostgREST."""

    def setUp(self):
        self.calls = []
        self.responses = []
        self.store = SupabaseStore("https://x.supabase.co", "service-key")

    def fake_request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "table": url.rsplit("/", 1)[1], "params": dict(params or {}),
                           "json": json, "prefer": (headers or {}).get("Prefer", "")})
        return self.responses.pop(0) if self.responses else FakeResponse()

    def test_select_pages_on_the_exact_count(self):
        self.responses = [FakeResponse(body=json.dumps([{"id": i} for i in range(1000)]), headers={"Content-Range": "0-999/1500"}),
                          FakeResponse(body=json.dumps([{"id": i} for i in range(1000, 1500)]), headers={"Content-Range": "1000-1499/1500"})]
        with mock.patch.object(store_module.requests, "request", self.fake_request):
            rows = self.store.select("transactions", {"year": "eq.2026"})
        self.assertEqual(len(rows), 1500)
        self.assertEqual([c["params"]["offset"] for c in self.calls], [0, 1000])
        self.assertTrue(all(c["prefer"] == "count=exact" for c in self.calls))
        # A short page is final only when the count says so (max-rows below 1000 would otherwise truncate).
        self.calls, self.responses = [], [FakeResponse(body=json.dumps([{"id": 1}] * 100), headers={"Content-Range": "0-99/250"}),
                                          FakeResponse(body=json.dumps([{"id": 2}] * 100), headers={"Content-Range": "100-199/250"}),
                                          FakeResponse(body=json.dumps([{"id": 3}] * 50), headers={"Content-Range": "200-249/250"})]
        with mock.patch.object(store_module.requests, "request", self.fake_request):
            self.assertEqual(len(self.store.select("invoices")), 250)
        self.assertEqual(store_module._content_range_total("*/0", 7), 0)
        self.assertEqual(store_module._content_range_total("0-9/*", 7), 7)
        self.assertEqual(store_module._content_range_total(None, 7), 7)

    def test_transactions_are_inserted_with_uniform_keys_and_null_dates(self):
        rows = [dict(txn("t1", "2026-09-04", "A", 1.0), post_date="", statement_id="s1"),
                dict(txn("t2", "2026-09-05", "B", 2.0), post_date="2026-09-06", note="x")]
        with mock.patch.object(store_module.requests, "request", self.fake_request):
            self.store.insert_transactions(rows)
        sent = self.calls[0]["json"]
        self.assertEqual(self.calls[0]["method"], "POST")
        self.assertEqual({tuple(sorted(r)) for r in sent}, {tuple(sorted(store_module.TRANSACTION_FIELDS))})
        self.assertIsNone(sent[0]["post_date"])
        self.assertEqual(sent[1]["post_date"], "2026-09-06")
        self.assertEqual(sent[0]["rejected_invoice_ids"], [])

    def test_cards_new_rows_are_inserted_and_known_ones_patched(self):
        self.responses = [FakeResponse(body=json.dumps([{"last4": "1610"}]), headers={"Content-Range": "0-0/1"})]
        with mock.patch.object(store_module.requests, "request", self.fake_request):
            self.store.upsert_cards([{"last4": "1610", "holder_name": "Yin Wei"},
                                     {"last4": "4421", "label": "Card ending 4421", "holder_name": "Bo Li", "active": True}])
        methods = [(c["method"], c["table"]) for c in self.calls]
        self.assertEqual(methods, [("GET", "cards"), ("POST", "cards"), ("PATCH", "cards")])
        inserted = self.calls[1]["json"]
        self.assertEqual(tuple(sorted(inserted[0])), tuple(sorted(store_module.CARD_FIELDS)))
        self.assertEqual((inserted[0]["last4"], inserted[0]["active"]), ("4421", True))
        self.assertEqual(self.calls[2]["params"], {"last4": "eq.1610"})
        self.assertEqual(self.calls[2]["json"], {"holder_name": "Yin Wei"})

    def test_statement_dates_and_invoice_id_chunks(self):
        self.responses = [FakeResponse(status=201, body=json.dumps([{"id": "s1"}]))]
        with mock.patch.object(store_module.requests, "request", self.fake_request):
            self.store.insert_statement({"file_name": "x.xlsx", "period_start": "", "period_end": "2026-09-30"})
        self.assertEqual((self.calls[0]["json"][0]["period_start"], self.calls[0]["json"][0]["period_end"]), (None, "2026-09-30"))
        self.calls = []
        self.responses = [FakeResponse(headers={"Content-Range": "*/0"})] * 3
        with mock.patch.object(store_module.requests, "request", self.fake_request):
            self.store.list_invoices(ids=[f"inv{i}" for i in range(250)])
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(self.calls[0]["params"]["id"].startswith('in.("inv0","inv1"'))

    def test_delete_statement_filters(self):
        self.responses = [FakeResponse(body=json.dumps([{"id": "t1"}, {"id": "t2"}])),
                          FakeResponse(body=json.dumps([{"id": "t3"}]), headers={"Content-Range": "0-0/1"}),
                          FakeResponse(body=json.dumps([{"id": "t3"}])),
                          FakeResponse(body=json.dumps([{"id": "s1"}]))]
        with mock.patch.object(store_module.requests, "request", self.fake_request):
            result = self.store.delete_statement("s1")
        self.assertEqual(result, {"deleted": True, "transactions_removed": 2, "transactions_kept": 1})
        first = self.calls[0]
        self.assertEqual(first["method"], "DELETE")
        self.assertEqual(first["params"]["or"], "(match_method.is.null,match_method.neq.manual)")
        self.assertEqual(first["params"]["cost_center"], "is.null")
        self.assertEqual(self.calls[2]["json"], {"statement_id": None})


class TwoPhaseRelinkTests(unittest.TestCase):
    def test_invoice_moves_from_a_possible_to_a_better_transaction_without_a_conflict(self):
        store = MemoryStore()
        store.insert_transactions([
            txn("t_old", "2026-09-01", "Sq *Corner Cafe", 33.00, status="possible", invoice_id="inv_x", method="rule"),
            txn("t_new", "2026-09-05", "Dunder Mifflin Paper", 33.00),
        ])
        store.upsert_invoices([inv("inv_x", "Dunder Mifflin", 33.00, "2026-09-05")])
        counts = matching.match_month(store, None, 2026, 9, log=quiet)
        self.assertEqual((counts["matched"], counts["possible"], counts["missing"]), (1, 0, 1))
        self.assertEqual(store.get_transaction("t_new")["invoice_id"], "inv_x")
        self.assertEqual((store.get_transaction("t_old")["invoice_status"], store.get_transaction("t_old")["invoice_id"]),
                         ("missing", None))

    def test_memory_store_refuses_a_double_link(self):
        store = MemoryStore()
        store.insert_transactions([txn("a", "2026-09-01", "A", 1.0, invoice_id="inv_1", status="matched"),
                                   txn("b", "2026-09-01", "B", 1.0)])
        with self.assertRaises(StoreError):
            store.update_transaction("b", {"invoice_id": "inv_1"})


class RejectedPairsAndAiSanityTests(unittest.TestCase):
    def test_rejected_invoice_is_never_proposed_again(self):
        t = dict(txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00), rejected_invoice_ids=["i1"])
        i = inv("i1", "Anthropic", 150.00, "2026-09-03")
        self.assertEqual(matching.rule_match([t], [i]), {})
        judge = FakeGemini(responses=[{"matches": [{"transaction_id": "t1", "invoice_id": "i1", "confidence": 0.95}]}])
        self.assertEqual(matching.ai_match(judge, [t], [i]), {})

    def test_ai_proposals_are_checked_against_the_numbers(self):
        t = txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00, card="1610")
        proposals = [{"transaction_id": "t1", "invoice_id": "i_other_card", "confidence": 0.95},
                     {"transaction_id": "t1", "invoice_id": "i_far_off", "confidence": 0.95},
                     {"transaction_id": "t1", "invoice_id": "i_near", "confidence": 0.95},
                     {"transaction_id": "t1", "invoice_id": "i_exact", "confidence": 0.95}]
        invs = [inv("i_other_card", "Anthropic", 150.00, "2026-09-03", card="9999"),
                inv("i_far_off", "Anthropic", 200.00, "2026-09-03"),
                inv("i_near", "Anthropic", 155.00, "2026-09-03"),
                inv("i_exact", "Anthropic", 150.00, "2026-09-03")]
        out = matching.ai_match(FakeGemini(responses=[{"matches": proposals[:1]}]), [t], invs[:1])
        self.assertEqual(out, {})
        out = matching.ai_match(FakeGemini(responses=[{"matches": proposals[1:2]}]), [t], invs[1:2])
        self.assertEqual(out, {})
        out = matching.ai_match(FakeGemini(responses=[{"matches": proposals[2:3]}]), [t], invs[2:3])
        self.assertEqual(out["t1"][1:3], ("possible", 0.84))
        out = matching.ai_match(FakeGemini(responses=[{"matches": proposals[3:]}]), [t], invs[3:])
        self.assertEqual(out["t1"][1:3], ("matched", 0.95))
        self.assertEqual(matching.amount_deviation({"amount": 100.0, "currency": "CAD"}, {"total": 110.0, "currency": "EUR"}), None)

    def test_exact_foreign_amount_beats_a_misread_currency_label(self):
        # A USD 113 receipt that Gemini labelled CAD, charged as CAD 164.26 (USD 113.00).
        t = txn("t1", "2026-09-29", "Anthropic", 164.26, source_amount=113.00, source_currency="USD")
        i = inv("i1", "Anthropic, PBC", 113.00, "2026-09-29", currency="CAD")
        score, note = matching.amount_score(t, i)
        self.assertEqual(score, 1.0)
        self.assertIn("invoice currency read as CAD", note)
        self.assertEqual(matching.rule_match([t], [i])["t1"][1], "matched")
        self.assertEqual(matching.amount_deviation(t, i), 0.0)
        # A near miss on the foreign amount with the wrong label is still not comparable.
        near = inv("i2", "Anthropic, PBC", 114.00, "2026-09-29", currency="CAD")
        self.assertEqual(matching.amount_score(t, near)[0], 0.0)

    def test_credit_notes_and_removed_files_stay_out_of_the_pool(self):
        store = MemoryStore()
        store.insert_transactions([txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00)])
        store.upsert_invoices([dict(inv("i_credit", "Anthropic", 150.00, "2026-09-03"), document_type="credit_note"),
                               dict(inv("i_gone", "Anthropic", 150.00, "2026-09-03"), removed_at="2026-10-01T00:00:00Z")])
        counts = matching.match_month(store, None, 2026, 9, log=quiet)
        self.assertEqual((counts["matched"], counts["invoices_available"]), (0, 0))


class RemovedFilesTests(unittest.TestCase):
    def test_files_missing_from_drive_are_stamped_removed(self):
        from test_pipeline import FakeDrive, facts_handler, invoice_tree  # noqa: WPS433
        tree, files_by_id = invoice_tree()
        drive = FakeDrive(tree, files_by_id)
        store = MemoryStore()
        gemini = FakeGemini(handler=facts_handler)
        first = invoices.index_invoices(store, drive, gemini, "root", limit=50, workers=1, log=quiet)
        self.assertGreater(first["indexed"], 2)
        self.assertEqual(first["removed"], 0)
        victim = next(f for f in tree["v_anth"] if f.get("name") == "a.pdf")
        tree["v_anth"].remove(victim)
        second = invoices.index_invoices(store, drive, gemini, "root", limit=50, workers=1, log=quiet)
        self.assertEqual((second["indexed"], second["removed"]), (0, 1))
        self.assertIsNotNone(store.get_invoice(victim["id"])["removed_at"])
        # Only the walked month folders are judged: a walk of 2026 10 alone says nothing about it.
        tree["v_anth"].append(victim)          # it comes back: the stamp is cleared without another read
        third = invoices.index_invoices(store, drive, gemini, "root", limit=50, workers=1, log=quiet)
        self.assertEqual(third["indexed"], 0)
        self.assertIsNone(store.get_invoice(victim["id"])["removed_at"])
        tree["v_anth"].remove(victim)
        fourth = invoices.index_invoices(store, drive, gemini, "root", month_folders={"2026 10"}, limit=50, workers=1, log=quiet)
        self.assertEqual(fourth["removed"], 0)
        self.assertIsNone(store.get_invoice(victim["id"])["removed_at"])


class ApiReviewTests(ApiTestCase):
    def test_unlink_remembers_the_rejection_and_link_clears_it(self):
        self.upload()
        ids = self.ids()
        self.inject_invoices()
        self.client.post("/api/match?year=2026&month=9")
        row = self.patch(ids["anth"], {"action": "unlink"})["transaction"]
        self.assertEqual(row["rejected_invoice_ids"], ["inv_anth"])
        self.client.post("/api/match?year=2026&month=9")
        self.assertEqual(self.store.get_transaction(ids["anth"])["invoice_status"], "missing")
        row = self.patch(ids["anth"], {"action": "link", "invoice_id": "inv_anth"})["transaction"]
        self.assertEqual((row["invoice_status"], row["rejected_invoice_ids"]), ("matched", []))

    def test_non_string_json_values_do_not_crash(self):
        self.upload()
        ids = self.ids()
        row = self.patch(ids["anth"], {"cost_center": 42, "usage": None})["transaction"]
        self.assertEqual((row["cost_center"], row["usage"]), ("42", None))
        r = self.client.patch("/api/transactions/" + ids["anth"], data="[1,2]", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_delete_statement(self):
        self.upload()
        ids = self.ids()
        self.patch(ids["anth"], {"usage": "keep me"})
        statement_id = self.transactions()["statements"][0]["id"]
        r = self.client.delete("/api/statements/" + statement_id)
        self.assertEqual(r.status_code, 200, r.get_json())
        result = r.get_json()
        self.assertEqual((result["transactions_removed"], result["transactions_kept"]), (9, 1))
        self.assertEqual(self.client.delete("/api/statements/" + statement_id).status_code, 404)
        self.assertEqual(self.transactions()["statements"], [])
        self.assertEqual(self.store.get_transaction(ids["anth"])["statement_id"], None)

    def test_healthz_hides_job_output_and_api_errors_are_json(self):
        import main
        main._last_job.update({"name": "match", "result": {"secret": "card data"}})
        body = self.client.get("/healthz").get_json()
        self.assertEqual(set(body["last_job"]), {"name", "started", "finished"})
        r = self.client.get("/api/does-not-exist")
        self.assertEqual((r.status_code, r.get_json()["error"][:8]), (404, "The requ"))
        self.assertEqual(self.client.get("/api/invoices?year=2026&month=9&all=1").get_json()["month_folders"], "all")

    def test_member_cannot_link_an_invoice_printed_with_another_card(self):
        import main
        self.upload()
        ids = self.ids()
        self.client.post("/api/cards", json={"last4": "1610", "owner_email": "yin@superhairpieces.com"})
        self.store.upsert_invoices([inv("inv_9999", "Anthropic", 150.00, "2026-09-03", card="9999"),
                                    inv("inv_none", "Anthropic", 150.00, "2026-09-03")])
        member = {"email": "yin@superhairpieces.com", "name": "Yin", "role": "member", "picture": ""}
        with mock.patch.object(main, "current_user", return_value=member):
            self.patch(ids["anth"], {"action": "link", "invoice_id": "inv_9999"}, expect=403)
            self.patch(ids["anth"], {"action": "link", "invoice_id": "inv_none"})


if __name__ == "__main__":
    unittest.main()
