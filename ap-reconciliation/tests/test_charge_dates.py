"""Bills that state when they are charged to the card (invoices.charge_date): TELUS bills $50.85 every
month and charges each bill about three weeks after its date, so the charge must be paired with the bill
it pays, not with next month's bill dated a few days after it."""
import datetime as dt
import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import FakeDrive, FakeGemini, inv, txn  # noqa: E402
import invoices as invoice_index  # noqa: E402
import main  # noqa: E402
import matching  # noqa: E402
from store import MemoryStore  # noqa: E402

QUIET = dict(use_ai=False, log=lambda *a: None)
# Account 606763692: bills on the 16th, "charged to your credit card on <the 8th or 9th of next month>",
# card 4431 charged a few days after that. No June bill was filed.
BILLS = {"03": "2026-04-08", "04": "2026-05-08", "05": "2026-06-08", "07": "2026-08-08", "08": "2026-09-08",
         "09": "2026-10-09", "10": "2026-11-09"}
CHARGES = ["2026-04-13", "2026-05-11", "2026-06-10", "2026-07-13", "2026-08-10", "2026-09-10", "2026-10-13"]


def telus_bill(month, dated=True):
    row = inv(f"b{month}", "TELUS", 50.85, f"2026-{month}-16", month_folder=f"2026 {month}",
              vendor_folder="TELUS Mobility", file_name=f"606763692_{month}_2026-{month}-16.pdf")
    row["charge_date"] = BILLS[month] if dated else None
    return row


def telus_store(dated=True):
    store = MemoryStore()
    store.upsert_cards([{"last4": "4431"}])
    store.upsert_invoices([telus_bill(m, dated) for m in BILLS])
    store.insert_transactions([txn(f"c{d[5:7]}", d, "TELUS PRE-AUTH PAYME", 50.85, card="4431") for d in CHARGES])
    return store


def pairs(store):
    return {t: (store.get_transaction(t)["invoice_id"]) for t in (f"c{d[5:7]}" for d in CHARGES)}


EXPECTED = {"c04": "b03", "c05": "b04", "c06": "b05", "c07": None, "c08": "b07", "c09": "b08", "c10": "b09"}


class ChargeDateReadingTests(unittest.TestCase):
    def test_a_stated_charge_date_is_kept_within_reach_of_the_bill(self):
        self.assertEqual(invoice_index.charge_date("2026-10-09", "2026-09-16"), "2026-10-09")
        self.assertEqual(invoice_index.charge_date("2026-09-14", "2026-09-16"), "2026-09-14")
        self.assertIsNone(invoice_index.charge_date("2027-10-09", "2026-09-16"))   # a misread year
        self.assertIsNone(invoice_index.charge_date("2026-10-09", None))
        self.assertIsNone(invoice_index.charge_date(None, "2026-09-16"))

    def test_the_reading_asks_for_it_and_stores_it(self):
        self.assertIn("charge_date", invoice_index.INVOICE_PROMPT)
        # Intact prints a due date and lists auto-pay among the ways to pay: that is no charge date.
        self.assertIn("A due\n  date", invoice_index.INVOICE_PROMPT)
        gemini = FakeGemini([{"is_invoice": True, "document_type": "invoice", "vendor": "TELUS",
                              "invoice_date": "2026-09-16", "charge_date": "2026-10-09", "total": 50.85,
                              "currency": "CAD"}])
        record = {"id": "f1", "file_name": "bill.pdf", "mime_type": "application/pdf", "web_view_link": "",
                  "folder_path": "/2026 09/TELUS Mobility", "month_folder": "2026 09", "vendor_folder": "TELUS Mobility",
                  "modified_time": "2026-11-20T17:00:00Z", "size": 10,
                  "_file": {"id": "f1", "mimeType": "application/pdf", "name": "bill.pdf"}}
        row = invoice_index.extract_invoice(gemini, FakeDrive({}), record)
        self.assertEqual((row["invoice_date"], row["charge_date"]), ("2026-09-16", "2026-10-09"))


class ChargeDateMatchingTests(unittest.TestCase):
    def match_all(self, store):
        for d in CHARGES:
            matching.match_month(store, None, int(d[:4]), int(d[5:7]), **QUIET)

    def test_each_charge_pays_the_bill_that_says_it_is_charged_then(self):
        store = telus_store()
        self.match_all(store)
        self.assertEqual(pairs(store), EXPECTED)
        note = store.get_transaction("c05")["match_note"]
        self.assertIn("from the date it is charged (2026-05-08)", note)

    def test_the_charge_whose_bill_is_missing_takes_no_other_months_bill(self):
        store = telus_store()
        self.match_all(store)
        self.assertEqual(store.get_transaction("c07")["invoice_status"], "missing")
        # Neither the Rematch nor the judge's pre-filter offers it next month's bill.
        out = matching.match_transaction(store, None, "c07", **QUIET)
        self.assertEqual(out["outcome"], "none")
        self.assertFalse(matching.plausible(store.get_transaction("c07"), store.get_invoice("b07")))

    def test_without_a_charge_date_bills_still_match_by_their_own_date(self):
        store = telus_store(dated=False)
        self.match_all(store)
        self.assertEqual(store.get_transaction("c05")["invoice_id"], "b05")   # the old pairing: dated after

    def test_the_receipt_check_names_the_charge_date(self):
        store = telus_store()
        checks = matching.invoice_checks(store.get_transaction("c04"), store.get_invoice("b04"))
        self.assertTrue(any("charged on 2026-05-08" in c["text"] for c in checks))


class RereadChargeDatesTests(unittest.TestCase):
    """python main.py reread-charge-dates --vendor telus: date the bills read before charge_date existed,
    let go of the links that fall outside it, and pair again."""

    def setUp(self):
        self.store = telus_store(dated=False)
        for b in BILLS:                       # as stored today: read, but without a charge date
            self.store.invoices[f"b{b}"]["charge_date"] = None
        # The matcher paired each charge with the bill dated just after it (the bug); one link by hand.
        wrong = {"c04": "b04", "c05": "b05", "c07": "b07", "c08": "b08", "c09": "b09", "c10": "b10"}
        for tid, iid in wrong.items():
            self.store.update_transaction(tid, {"invoice_status": "matched", "invoice_id": iid,
                                                "match_method": "rule", "match_confidence": 0.9})
        self.store.insert_transactions([txn("other", "2026-09-20", "STAPLES", 50.85, card="4431")])

        def answer(parts, model):
            name = re.search(r'file name "([^"]+)"', parts[-1]["text"]).group(1)
            month = name.split("_")[1]
            return {"is_invoice": True, "document_type": "invoice", "vendor": "TELUS", "total": 50.85,
                    "currency": "CAD", "invoice_date": f"2026-{month}-16", "charge_date": BILLS[month]}
        self.gemini = FakeGemini(handler=answer)
        main.set_clients(store=self.store, drive=FakeDrive({}), gemini=self.gemini)

    def test_bills_are_dated_and_every_charge_is_paired_with_the_bill_it_pays(self):
        with mock.patch.object(main, "AI_DISABLED", False):
            out = main.reread_charge_dates("telus", log=lambda *a: None)
        self.assertEqual((out["to_read"], out["dated"], out["failed"]), (7, 7, 0))
        self.assertEqual({i: self.store.get_invoice(f"b{i}")["charge_date"] for i in BILLS}, BILLS)
        self.assertEqual(pairs(self.store), EXPECTED)
        self.assertEqual(self.store.get_transaction("other")["invoice_status"], "missing")
        with mock.patch.object(main, "AI_DISABLED", False):
            again = main.reread_charge_dates("telus", log=lambda *a: None)
        self.assertEqual(again["to_read"], 0)                    # done is not read again

    def test_a_link_made_by_hand_stays(self):
        self.store.update_transaction("c05", {"match_method": "manual"})
        with mock.patch.object(main, "AI_DISABLED", False):
            main.reread_charge_dates("telus", log=lambda *a: None)
        self.assertEqual(self.store.get_transaction("c05")["invoice_id"], "b05")

    def test_a_run_cut_short_after_dating_the_bills_is_finished_by_the_next(self):
        real = self.store.transactions_for_invoice
        calls = {"n": 0}

        def flaky(iid):
            calls["n"] += 1
            if calls["n"] == 1:
                raise main.StoreError("GET transactions -> 503")
            return real(iid)
        with mock.patch.object(main, "AI_DISABLED", False), \
                mock.patch.object(self.store, "transactions_for_invoice", side_effect=flaky):
            with self.assertRaises(main.StoreError):
                main.reread_charge_dates("telus", log=lambda *a: None)
        self.assertEqual(self.store.get_invoice("b03")["charge_date"], BILLS["03"])   # dated, not re-paired
        with mock.patch.object(main, "AI_DISABLED", False):
            out = main.reread_charge_dates("telus", log=lambda *a: None)
        self.assertEqual((out["to_read"], out["dated"]), (0, 0))
        self.assertEqual(pairs(self.store), EXPECTED)

    def test_the_command_line_needs_a_vendor(self):
        import subprocess
        env = dict(os.environ, STORE="memory", AI_DISABLED="1", AUTH_DISABLED="1")
        out = subprocess.run([sys.executable, os.path.join(HERE, "..", "cloud_run", "main.py"), "reread-charge-dates"],
                             capture_output=True, text=True, env=env, timeout=120)
        self.assertIn("name a vendor", out.stdout, out.stderr[-500:])

    def test_a_vendor_must_be_named(self):
        with mock.patch.object(main, "AI_DISABLED", False):
            self.assertIn("skipped", main.reread_charge_dates("  ", log=lambda *a: None))


if __name__ == "__main__":
    unittest.main()
