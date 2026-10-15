"""Receipts added from the dashboard, through the API: Add receipt… (POST .../receipt), where it
would go (GET .../receipt-target), Remove… (DELETE .../receipt), the row the page polls
(GET /api/transactions/<id>), the Chat card's Add receipts link (/receipts/<last4>/<YYYY>/<MM>),
the background read, and how an upload meets the invoice index and the month-wide matcher.

The sample statement has cards 1610 and 4894 (and 2301), so receipt upload is switched on for
1610 here instead of the default 4421."""
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import MEMBER, ApiTestCase, FakeDrive, FakeGemini, file_, folder, inv, quiet  # noqa: E402
from test_receipts import BASELINE, PDF, PROGRESSIVE  # noqa: E402

import chat  # noqa: E402
import invoices  # noqa: E402
import main  # noqa: E402
import matching  # noqa: E402
import receipts  # noqa: E402
import store as store_module  # noqa: E402
from drive import DriveError  # noqa: E402
from gemini import GeminiError  # noqa: E402
from store import StoreError, SupabaseStore  # noqa: E402

DEFAULT_RECEIPT_CARDS = set(main.RECEIPT_UPLOAD_CARDS)      # as configured without RECEIPT_UPLOAD_CARDS
PAGE = {"X-Requested-With": "XMLHttpRequest"}
FACTS = {"is_invoice": True, "document_type": "receipt", "vendor": "Anthropic", "invoice_number": "R-1",
         "invoice_date": "2026-09-04", "total": 150.0, "currency": "CAD", "card_last4": "1610",
         "payment_method": "Visa ending 1610", "summary": "Claude Team seats"}


class SyncPool:
    """main._receipt_pool stand-in that reads at once, inside the request."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class DeferredPool:
    """main._receipt_pool stand-in that holds the read until run() (a read still going on)."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args, **kwargs):
        self.jobs.append((fn, args, kwargs))

    def run(self):
        for fn, args, kwargs in self.jobs:
            fn(*args, **kwargs)


class ReceiptApiTestCase(ApiTestCase):
    """The sample statement uploaded; Invoice root "root" in a fake Drive; receipt upload on for 1610."""

    tree = None

    def setUp(self):
        super().setUp()
        self.drive = FakeDrive(self.tree() if self.tree else {"root": []})
        main.set_clients(drive=self.drive)
        for name, value in (("RECEIPT_UPLOAD_CARDS", {"1610"}), ("INVOICE_FOLDER_ID", "root")):
            p = mock.patch.object(main, name, value)
            p.start()
            self.addCleanup(p.stop)
        quiet_print = mock.patch("builtins.print")
        quiet_print.start()
        self.addCleanup(quiet_print.stop)
        self.reset_state()
        self.addCleanup(self.reset_state)
        self.assertEqual(self.upload().status_code, 200)
        self.ids_ = self.ids()
        self.drive.calls.clear()

    @staticmethod
    def reset_state():
        main._month_folder_ids.clear()
        main._month_folder_names.clear()
        main._receipt_times.clear()
        main._reading.clear()
        main._clients.pop("gemini", None)

    def send(self, txn_id, files=(BASELINE,), vendor=None, upload_id=None, headers=None):
        data = {"file": [(io.BytesIO(b), f"IMG_{i}.jpg") for i, b in enumerate(files)]}
        if vendor is not None:
            data["vendor_folder"] = vendor
        if upload_id is not None:
            data["upload_id"] = upload_id
        return self.client.post(f"/api/transactions/{txn_id}/receipt", data=data, content_type="multipart/form-data",
                                headers=headers or {})

    def saved(self, txn_id, **kwargs):
        r = self.send(txn_id, **kwargs)
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()

    def remove(self, txn_id, expect=200, headers=None):
        r = self.client.delete(f"/api/transactions/{txn_id}/receipt", headers=headers or {})
        self.assertEqual(r.status_code, expect, r.get_json())
        return r.get_json()

    def target(self, txn_id, expect=200):
        r = self.client.get(f"/api/transactions/{txn_id}/receipt-target")
        self.assertEqual(r.status_code, expect, r.get_json())
        return r.get_json()

    def row(self, txn_id):
        r = self.client.get(f"/api/transactions/{txn_id}")
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["transaction"]

    def child_folders(self, parent):
        return [f["name"] for f in self.drive.tree.get(parent, []) if f.get("mimeType") == main.FOLDER]

    def folder_id(self, parent, name):
        return next(f["id"] for f in self.drive.tree.get(parent, []) if f.get("name") == name)

    def ai_on(self, handler=None, pool=None):
        """Gemini on: answers from handler (FACTS by default); the read runs in `pool` (at once by default)."""
        gemini = FakeGemini(handler=handler or (lambda parts, model: dict(FACTS)))
        main.set_clients(gemini=gemini)
        for name, value in (("AI_DISABLED", False), ("_receipt_pool", pool or SyncPool())):
            p = mock.patch.object(main, name, value)
            p.start()
            self.addCleanup(p.stop)
        return gemini


class SaveReceiptTests(ReceiptApiTestCase):
    def test_happy_path(self):
        anth = self.ids_["anth"]
        body = self.saved(anth, upload_id="rcpt-0001-abcd")
        upload = self.drive.uploads[0]
        file_id = upload["id"]
        # Invoice/2026 09/Anthropic/2026-09-04 Anthropic receipt.jpg
        month_id = self.folder_id("root", "2026 09")
        vendor_id = self.folder_id(month_id, "Anthropic")
        self.assertEqual([(f["parent"], f["name"]) for f in self.drive.created_folders],
                         [("root", "2026 09"), (month_id, "Anthropic")])
        self.assertEqual((upload["parent"], upload["name"], upload["mime"], upload["data"]),
                         (vendor_id, "2026-09-04 Anthropic receipt.jpg", "image/jpeg", BASELINE))
        self.assertEqual(upload["appProperties"], {"apTxn": anth, "apBy": "anonymous", "apUpload": "rcpt-0001-abcd"})
        self.assertIn("card ending 1610", upload["description"])
        self.assertIn("(CAD 150.00) on 2026-09-04", upload["description"])
        self.assertIn("Uploaded from the AP dashboard by anonymous on ", upload["description"])
        self.assertNotIn("150", upload["name"])
        self.assertNotIn("1610", upload["name"])
        self.assertEqual(self.drive.calls[-1], "upload")
        self.assertEqual(self.drive.calls.count("list_children"), 1)        # the root only: the rest is new

        self.assertEqual((body["status"], body["replayed"], body["reading"]), ("saved", False, False))
        self.assertEqual(body["file"], {"id": file_id, "name": "2026-09-04 Anthropic receipt.jpg",
                                        "folder": "2026 09/Anthropic", "folder_created": True,
                                        "web_view_link": f"https://drive.google.com/file/d/{file_id}/view",
                                        "pages": 1, "size": len(BASELINE), "mime_type": "image/jpeg"})
        row = self.store.get_invoice(file_id)
        self.assertEqual((row["month_folder"], row["vendor_folder"], row["folder_path"], row["file_name"], row["size"]),
                         ("2026 09", "Anthropic", "/2026 09/Anthropic", "2026-09-04 Anthropic receipt.jpg", len(BASELINE)))
        for key in ("modified_time", "total", "extracted_at", "extraction_error", "removed_at"):
            self.assertIsNone(row[key], key)
        t = self.store.get_transaction(anth)
        self.assertEqual((t["invoice_status"], t["invoice_id"], t["match_method"], t["match_confidence"],
                          t["match_note"], t["updated_by"], t["rejected_invoice_ids"]),
                         ("matched", file_id, "manual", 1.0, "receipt uploaded by anonymous", "anonymous", []))
        shown = body["transaction"]
        self.assertEqual((shown["invoice_id"], shown["status_label"], shown["editable"]), (file_id, "Invoice found", True))
        self.assertEqual((shown["invoice"]["reading"], shown["invoice"]["total"], shown["invoice"]["vendor_folder"]),
                         (False, None, "Anthropic"))
        self.assertEqual(shown["checks"], [{"kind": "unread", "level": "info", "text": "Receipt not read yet."}])
        # The month folder is cached: the next receipt of the month lists only the month folder.
        with mock.patch.object(self.drive, "list_children", wraps=self.drive.list_children) as listed:
            self.saved(self.ids_["amzn"])
        self.assertEqual([c.args[0] for c in listed.call_args_list], [month_id])
        self.assertEqual(self.child_folders(month_id), ["Anthropic", "Amazon"])

    def test_two_photos_become_one_pdf(self):
        body = self.saved(self.ids_["anth"], files=(BASELINE, PROGRESSIVE))
        self.assertEqual(len(self.drive.uploads), 1)
        upload = self.drive.uploads[0]
        self.assertEqual((upload["mime"], upload["name"]), ("application/pdf", "2026-09-04 Anthropic receipt.pdf"))
        self.assertTrue(upload["data"].startswith(b"%PDF-1.4"))
        self.assertEqual((body["file"]["pages"], body["file"]["mime_type"], body["file"]["size"]),
                         (2, "application/pdf", len(upload["data"])))

    def test_a_pdf_on_its_own_is_kept(self):
        body = self.saved(self.ids_["anth"], files=(PDF,))
        self.assertEqual((self.drive.uploads[0]["data"], body["file"]["name"]), (PDF, "2026-09-04 Anthropic receipt.pdf"))

    def test_refused_uploads_touch_nothing(self):
        anth = self.ids_["anth"]
        cases = [
            ((), None, 400, "Attach the receipt as one or more 'file' parts."),
            ((b"",), None, 400, "The file is empty."),
            ((b"hello, not a photo",), None, 415,
             "That file isn't a photo or a PDF. Choose a JPEG, PNG, WebP or HEIC photo, or a PDF."),
            ((PDF, BASELINE), None, 400, "Several pages must all be photos; add a PDF on its own."),
            ((BASELINE, b"\xff\xd8\xff\xe0 cut"), None, 415, "Page 2 could not be read as a photo; take it again."),
            ((BASELINE,) * 11, None, 400, "At most 10 pages per receipt."),
            ((BASELINE,), "short", 400, "upload_id is malformed."),
            ((BASELINE,), "has spaces in it", 400, "upload_id is malformed."),
        ]
        for files, upload_id, status, error in cases:
            r = self.send(anth, files=files, upload_id=upload_id)
            self.assertEqual((r.status_code, r.get_json()["error"]), (status, error), error)
        r = self.client.post("/api/transactions/nope/receipt", data={"file": (io.BytesIO(BASELINE), "a.jpg")},
                             content_type="multipart/form-data")
        self.assertEqual((r.status_code, r.get_json()["error"]), (404, "transaction not found"))
        self.assertEqual(self.drive.calls, [])
        self.assertEqual(self.store.get_transaction(anth)["invoice_status"], "missing")

    def test_a_photo_over_7_mb_is_refused(self):
        big = BASELINE + b"\x00" * (7 * 1024 * 1024)
        r = self.send(self.ids_["anth"], files=(big,))
        self.assertEqual((r.status_code, r.get_json()["error"]), (413, "That photo is larger than 7 MB."))
        self.assertEqual(self.drive.calls, [])

    def test_a_body_over_the_receipt_limit_is_refused_before_it_is_read(self):
        self.assertEqual(main.RECEIPT_MAX_BODY, 16 * 1024 * 1024)
        big = BASELINE + b"\x00" * main.RECEIPT_MAX_BODY
        with mock.patch.object(receipts, "prepare_upload", side_effect=AssertionError("read anyway")):
            r = self.send(self.ids_["anth"], files=(big,))
        self.assertEqual((r.status_code, r.get_json()), (413, {"error": "The receipt is larger than 15 MB."}))
        self.assertEqual(self.drive.calls, [])
        # Other routes keep MAX_CONTENT_LENGTH and Werkzeug's words, as JSON.
        with mock.patch.dict(main.app.config, {"MAX_CONTENT_LENGTH": 1000}):
            r = self.upload(data=b"x" * 2000)
            self.assertEqual((r.status_code, r.get_json()),
                             (413, {"error": "The data value transmitted exceeds the capacity limit."}))
            self.assertEqual(self.send(self.ids_["anth"]).status_code, 201)     # the receipt route has its own

    def test_the_upload_date_is_when_the_row_was_made_not_its_last_edit(self):
        anth = self.ids_["anth"]
        with mock.patch.object(store_module, "now_iso", return_value="2026-10-15T09:00:00Z"):
            file_id = self.saved(anth)["file"]["id"]
        self.patch(anth, {"cost_center": "Marketing"})                         # updated_at moves on
        invoices.index_invoices(self.store, self.drive, FakeGemini(handler=lambda parts, model: dict(FACTS)), "root",
                                log=quiet)                                    # the row is read and written again
        row = self.row(anth)
        self.assertEqual((row["invoice"]["id"], row["invoice"]["total"], row["invoice"]["created_at"]),
                         (file_id, 150.0, "2026-10-15T09:00:00Z"))
        self.assertNotEqual(row["updated_at"][:10], "2026-10-15T09:00:00Z")


class FolderTests(ReceiptApiTestCase):
    @staticmethod
    def tree():
        return {"root": [folder("m09", "2026-09"), file_("loose", "notes.pdf")],
                "m09": [folder("v_uber", "uber"), folder("v_cost", "Costco")],
                "v_uber": [file_("old", "2026-09-10 uber receipt.jpg", "image/jpeg")], "v_cost": []}

    def test_a_month_folder_named_another_way_and_a_folder_spelt_differently_are_reused(self):
        body = self.saved(self.ids_["amzn"], vendor="Uber")
        self.assertEqual(self.drive.created_folders, [])                  # no "2026 09", no "Uber"
        upload = self.drive.uploads[0]
        self.assertEqual((upload["parent"], upload["name"]), ("v_uber", "2026-09-10 uber receipt (2).jpg"))
        self.assertEqual((body["file"]["folder"], body["file"]["folder_created"]), ("2026-09/uber", False))
        row = self.store.get_invoice(upload["id"])
        self.assertEqual((row["month_folder"], row["vendor_folder"], row["folder_path"]), ("2026 09", "uber", "/2026-09/uber"))

    def test_a_folder_name_that_climbs_out_falls_back_to_the_suggestion(self):
        body = self.saved(self.ids_["anth"], vendor="../../x")
        self.assertEqual([(f["parent"], f["name"]) for f in self.drive.created_folders], [("m09", "Anthropic")])
        self.assertEqual(body["file"]["folder"], "2026-09/Anthropic")
        body = self.saved(self.ids_["amzn"], vendor="2026 10")          # a month is not a vendor folder
        self.assertEqual(body["file"]["folder"], "2026-09/Amazon")

    def test_a_typed_new_name_is_cleaned_and_created(self):
        body = self.saved(self.ids_["anth"], vendor="  Claude: Team / Seats ")
        self.assertEqual(body["file"]["folder"], "2026-09/Claude- Team - Seats")
        self.assertIn("Claude- Team - Seats", self.child_folders("m09"))

    def test_receipt_target(self):
        target = self.target(self.ids_["amzn"])
        self.assertEqual(target, {"month_folder": "2026-09", "month_exists": True, "vendor_folder": "Amazon",
                                  "vendor_exists": False, "source": "new", "folders": ["Costco", "uber"],
                                  "limits": {"max_pages": 10, "max_image_mb": 7, "max_pdf_mb": 15}})
        self.drive.tree["m09"].append(folder("v_amzn", "Amazon.ca"))
        main._month_folder_ids.clear()
        target = self.target(self.ids_["amzn"])
        self.assertEqual((target["vendor_folder"], target["vendor_exists"], target["source"]),
                         ("Amazon.ca", True, "this_month"))
        self.assertEqual(self.drive.created_folders, [])
        self.target("nope", expect=404)

    def test_a_cached_month_folder_that_was_deleted_is_found_again_before_saving(self):
        # Saving checks a cached folder first (_folder_alive): Drive's 404 on get drops it.
        main._month_folder_ids["2026 09"] = "gone"
        real = self.drive.list_children

        def list_children(folder_id):
            if folder_id == "gone":
                raise DriveError("Drive GET files -> 404 File not found: gone.")
            return real(folder_id)

        with mock.patch.object(self.drive, "list_children", side_effect=list_children):
            body = self.saved(self.ids_["amzn"], vendor="Uber")
        self.assertEqual((self.drive.uploads[0]["parent"], body["file"]["folder"]), ("v_uber", "2026-09/uber"))
        self.assertEqual(main._month_folder_ids["2026 09"], "m09")

    def test_receipt_target_with_a_cached_month_folder_deleted_since(self):
        # receipt-target uses a cached id unchecked; listing it fails with a 404 and in_month_folder
        # looks the month up once more.
        main._month_folder_ids["2026 09"] = "gone"
        real = self.drive.list_children

        def list_children(folder_id):
            if folder_id == "gone":
                raise DriveError("Drive GET files -> 404 File not found: gone.")
            return real(folder_id)

        with mock.patch.object(self.drive, "list_children", side_effect=list_children) as listed:
            t = self.target(self.ids_["amzn"])
        self.assertEqual((t["month_folder"], t["month_exists"], t["folders"]), ("2026-09", True, ["Costco", "uber"]))
        self.assertEqual([c.args[0] for c in listed.call_args_list], ["gone", "root", "m09"])
        self.assertEqual(main._month_folder_ids["2026 09"], "m09")
        self.assertEqual(self.drive.created_folders, [])

    def test_a_cached_month_folder_now_in_the_trash_is_looked_up_again(self):
        main._month_folder_ids["2026 09"] = "old"
        real = self.drive.get

        def get(file_id, fields=None):
            if file_id == "old":
                return {"id": "old", "trashed": True}
            return real(file_id, fields=fields)

        with mock.patch.object(self.drive, "get", side_effect=get):
            body = self.saved(self.ids_["amzn"], vendor="Uber")
        self.assertEqual((self.drive.uploads[0]["parent"], body["file"]["folder"]), ("v_uber", "2026-09/uber"))
        self.assertEqual(main._month_folder_ids["2026 09"], "m09")


class TargetWithoutMonthFolderTests(ReceiptApiTestCase):
    def test_nothing_is_created_and_other_months_folders_are_suggested(self):
        self.store.upsert_invoices([inv("aug_1", "Anthropic", 150.0, "2026-08-04", month_folder="2026 08"),
                                    dict(inv("aug_2", "Gone Vendor", 9.0, "2026-08-04", month_folder="2026 08"),
                                         removed_at="2026-09-01T00:00:00Z")])
        target = self.target(self.ids_["anth"])
        self.assertEqual((target["month_folder"], target["month_exists"], target["vendor_folder"],
                          target["vendor_exists"], target["source"], target["folders"]),
                         ("2026 09", False, "Anthropic", False, "earlier_month", []))
        self.assertEqual(self.drive.created_folders, [])
        self.assertNotIn("ensure_folder", self.drive.calls)
        self.assertNotIn("2026 09", main._month_folder_ids)                 # not cached as missing
        self.assertEqual(self.target(self.ids_["amzn"])["source"], "new")


class GatingAndPermissionTests(ReceiptApiTestCase):
    def test_cards_without_receipt_upload_are_refused_before_any_drive_call(self):
        retell = self.ids_["retell"]
        r = self.send(retell)
        self.assertEqual((r.status_code, r.get_json()["error"]), (403, "Receipt upload isn't switched on for card 4894."))
        self.assertEqual(self.target(retell, expect=403)["error"], "Receipt upload isn't switched on for card 4894.")
        self.assertEqual(self.drive.calls, [])
        with mock.patch.object(main, "RECEIPT_UPLOAD_CARDS", {"all"}):
            body = self.saved(retell)
        self.assertEqual(body["file"]["folder"], "2026 09/Retellai")

    def test_the_default_is_card_4421_only(self):
        if "RECEIPT_UPLOAD_CARDS" not in os.environ:
            self.assertEqual(DEFAULT_RECEIPT_CARDS, {"4421"})
        self.client.post("/api/cards", json={"last4": "4421", "label": "Card ending 4421 - Bo Li"})
        with mock.patch.object(main, "RECEIPT_UPLOAD_CARDS", {"4421"}):
            cards = {c["last4"]: c["receipt_upload"] for c in self.client.get("/api/cards").get_json()["cards"]}
            self.assertEqual(cards, {"1610": False, "2301": False, "4421": True, "4894": False})
            months = self.client.get("/api/months").get_json()["cards"]
            self.assertEqual([c["last4"] for c in months if c["receipt_upload"]], ["4421"])
            page = self.transactions()["cards"]
            self.assertEqual([c["last4"] for c in page if c["receipt_upload"]], ["4421"])
            self.assertEqual(self.send(self.ids_["anth"]).status_code, 403)
        with mock.patch.object(main, "RECEIPT_UPLOAD_CARDS", {"*"}):
            self.assertTrue(main.receipt_upload_enabled("2301"))

    def test_only_the_cards_owner_or_ap(self):
        anth = self.ids_["anth"]
        with mock.patch.object(main, "current_user", return_value=dict(MEMBER)):
            r = self.send(anth)
            self.assertEqual((r.status_code, r.get_json()["error"]),
                             (403, "Only the card's owner or accounts payable can add receipts to card 1610."))
            self.target(anth, expect=403)
            self.assertFalse(self.row(anth)["editable"])
        self.assertEqual(self.drive.calls, [])
        r = self.client.post("/api/cards", json={"last4": "1610", "owner_email": "X@superhairpieces.com"})
        self.assertEqual(r.status_code, 200)
        with mock.patch.object(main, "current_user", return_value=dict(MEMBER)):
            self.assertTrue(self.row(anth)["editable"])
            body = self.saved(anth)
            self.assertEqual(body["transaction"]["match_note"], "receipt uploaded by x@superhairpieces.com")
            self.assertEqual(self.drive.uploads[0]["appProperties"]["apBy"], "x@superhairpieces.com")
            # Not the owner of 4894, even with receipt upload switched on for every card.
            with mock.patch.object(main, "RECEIPT_UPLOAD_CARDS", {"all"}):
                self.assertEqual(self.send(self.ids_["retell"]).status_code, 403)

    def test_no_user_is_401(self):
        anth = self.ids_["anth"]
        with mock.patch.object(main, "current_user", return_value=None):
            self.assertEqual(self.send(anth).status_code, 401)
            self.assertEqual(self.client.get(f"/api/transactions/{anth}/receipt-target").status_code, 401)
            self.assertEqual(self.client.delete(f"/api/transactions/{anth}/receipt").status_code, 401)
            self.assertEqual(self.client.get(f"/api/transactions/{anth}").status_code, 401)
        self.assertEqual(self.drive.calls, [])

    def test_get_one_transaction(self):
        anth = self.ids_["anth"]
        row = self.row(anth)
        self.assertEqual((row["id"], row["invoice_status"], row["editable"], row["checks"], row["invoice"]),
                         (anth, "missing", True, [], None))
        self.assertNotIn("raw", row)
        self.assertEqual(self.client.get("/api/transactions/nope").status_code, 404)
        # The PATCH route on the same path is unchanged.
        self.assertEqual(self.patch(anth, {"usage": "seats"})["transaction"]["usage"], "seats")


class EligibilityTests(ReceiptApiTestCase):
    def setUp(self):
        super().setUp()
        self.inject_invoices()
        r = self.client.post("/api/match?year=2026&month=9")
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_purchases_that_need_no_receipt_upload_nothing(self):
        anth = self.ids_["anth"]
        self.assertEqual(self.store.get_transaction(anth)["invoice_id"], "inv_anth")
        r = self.send(anth)
        body = r.get_json()
        self.assertEqual((r.status_code, body["status"], body["error"]),
                         (409, "not_saved", "This purchase already has its invoice (Anthropic · CAD 150.00); nothing was saved."))
        self.assertEqual(body["transaction"]["invoice_id"], "inv_anth")
        r = self.send(self.ids_["fee"])
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "Only purchases need a receipt."))
        with mock.patch.object(main, "RECEIPT_UPLOAD_CARDS", {"all"}):
            self.patch(self.ids_["costco"], {"action": "waive"})
            r = self.send(self.ids_["costco"])
        self.assertEqual((r.status_code, r.get_json()["status"], r.get_json()["error"]),
                         (409, "not_saved", "This purchase is marked as having no invoice; undo that first."))
        self.assertEqual(self.drive.uploads, [])
        self.assertNotIn("ensure_folder", self.drive.calls)

    def test_a_possible_match_is_replaced_and_remembered_as_rejected(self):
        amzn = self.ids_["amzn"]
        self.assertEqual(self.store.get_transaction(amzn)["invoice_status"], "possible")
        body = self.saved(amzn)
        t = self.store.get_transaction(amzn)
        self.assertEqual((t["invoice_status"], t["invoice_id"], t["match_method"], t["rejected_invoice_ids"]),
                         ("matched", self.drive.uploads[0]["id"], "manual", ["inv_far"]))
        self.assertEqual(body["file"]["folder"], "2026 09/Amazon")
        # The month-wide matcher leaves the upload alone, and inv_far is free for others again.
        r = self.client.post("/api/match?year=2026&month=9&card=1610").get_json()
        self.assertEqual(r["checked"], 0)
        self.assertEqual(self.store.get_transaction(amzn)["invoice_id"], self.drive.uploads[0]["id"])
        self.assertNotIn("inv_far", self.store.used_invoice_ids())


class ReplayTests(ReceiptApiTestCase):
    def test_a_retry_with_the_same_upload_id_gets_the_first_answer(self):
        anth = self.ids_["anth"]
        first = self.saved(anth, upload_id="0123456789abcdef")
        r = self.send(anth, upload_id="0123456789abcdef")
        self.assertEqual(r.status_code, 200, r.get_json())
        again = r.get_json()
        self.assertEqual(len(self.drive.uploads), 1)
        self.assertEqual((again["status"], again["replayed"], again["reading"]), ("saved", True, False))
        self.assertEqual({k: v for k, v in again["file"].items() if k != "folder_created"},
                         {k: v for k, v in first["file"].items() if k != "folder_created"})
        self.assertEqual(again["transaction"]["invoice_id"], first["file"]["id"])
        # Another receipt for the same purchase is refused, whoever sends it.
        for upload_id in ("fedcba9876543210", None):
            r = self.send(anth, upload_id=upload_id)
            self.assertEqual((r.status_code, r.get_json()["status"]), (409, "not_saved"))
            self.assertEqual(r.get_json()["error"], "This purchase already has its invoice (Anthropic); nothing was saved.")
        with mock.patch.object(main, "current_user", return_value={**MEMBER, "role": "ap"}):
            self.assertEqual(self.send(anth, upload_id="0123456789abcdef").status_code, 409)
        self.assertEqual(len(self.drive.uploads), 1)

    def race(self, retry_upload_id):
        """Send the Anthropic receipt with one upload id; while its file goes up to Drive, a second
        request (the page's retry after a lost connection, or another receipt) runs to the end.
        -> (outer response, inner response, outer file id, inner file id)."""
        anth, inner = self.ids_["anth"], []

        def second_request(entry):
            self.drive.on_upload = None
            inner.append(self.send(anth, upload_id=retry_upload_id))

        self.drive.on_upload = second_request
        outer = self.send(anth, upload_id="0123456789abcdef")
        return outer, inner[0], self.drive.uploads[0]["id"], self.drive.uploads[1]["id"]

    def test_a_retry_that_arrives_while_the_first_save_is_still_running_gets_the_saved_answer(self):
        anth = self.ids_["anth"]
        first, retry, first_id, retry_id = self.race("0123456789abcdef")
        # The retry finished first and linked its copy; the first request's copy is the duplicate.
        self.assertEqual(retry.status_code, 201, retry.get_json())
        self.assertEqual(first.status_code, 200, first.get_json())
        body = first.get_json()
        self.assertEqual((body["status"], body["replayed"]), ("saved", True))
        self.assertEqual((body["file"]["id"], body["file"]["folder"], body["transaction"]["invoice_id"]),
                         (retry_id, "2026 09/Anthropic", retry_id))
        t = self.store.get_transaction(anth)
        self.assertEqual((t["invoice_status"], t["invoice_id"], t["match_note"]),
                         ("matched", retry_id, "receipt uploaded by anonymous"))
        self.assertEqual(self.drive.trashed, [first_id])
        row = self.store.get_invoice(first_id)
        self.assertTrue(row["removed_at"])
        self.assertEqual(row["extraction_error"], main.WITHDRAWN)
        self.assertIsNone(self.store.get_invoice(retry_id)["removed_at"])

    def test_another_receipt_in_the_same_race_is_still_not_kept(self):
        first, other, first_id, other_id = self.race("fedcba9876543210")
        self.assertEqual(other.status_code, 201, other.get_json())
        self.assertEqual((first.status_code, first.get_json()["withdrawn"]), (409, True))
        self.assertEqual(self.store.get_transaction(self.ids_["anth"])["invoice_id"], other_id)
        self.assertEqual(self.drive.trashed, [first_id])


class WithdrawTests(ReceiptApiTestCase):
    def test_a_purchase_linked_meanwhile_keeps_its_link_and_the_upload_is_taken_back(self):
        anth = self.ids_["anth"]
        self.inject_invoices()

        def linked_meanwhile(entry):
            self.store.update_transaction(anth, {"invoice_status": "matched", "invoice_id": "inv_anth",
                                                 "match_confidence": 1.0, "match_method": "manual",
                                                 "match_note": "linked by ap@superhairpieces.com"})

        self.drive.on_upload = linked_meanwhile
        r = self.send(anth)
        body = r.get_json()
        file_id = self.drive.uploads[0]["id"]
        self.assertEqual((r.status_code, body["status"], body["withdrawn"]), (409, "not_saved", True))
        self.assertEqual(body["error"], "This purchase changed while your receipt was saving (now: Invoice found), so "
                                        "the receipt was not kept. Reload to see it.")
        self.assertEqual(body["transaction"]["invoice_id"], "inv_anth")
        self.assertEqual(self.drive.trashed, [file_id])
        row = self.store.get_invoice(file_id)
        self.assertTrue(row["removed_at"])
        self.assertEqual(row["extraction_error"], "upload withdrawn: the purchase changed while it was saving")
        t = self.store.get_transaction(anth)
        self.assertEqual((t["invoice_id"], t["match_note"]), ("inv_anth", "linked by ap@superhairpieces.com"))
        self.assertNotIn(file_id, [i["id"] for i in matching.candidate_invoices(self.store, 2026, 9)])

    def test_a_refused_link_is_withdrawn_too(self):
        anth = self.ids_["anth"]
        with mock.patch.object(self.store, "update_transaction", side_effect=StoreError("duplicate key")):
            r = self.send(anth)
        self.assertEqual((r.status_code, r.get_json()["withdrawn"]), (409, True))
        self.assertIn("(now: Invoice missing)", r.get_json()["error"])
        self.assertEqual(self.drive.trashed, [self.drive.uploads[0]["id"]])
        self.assertEqual(self.store.get_transaction(anth)["invoice_status"], "missing")

    def test_a_withdrawn_upload_whose_trash_failed_never_joins_the_pool(self):
        anth = self.ids_["anth"]
        self.inject_invoices()

        def linked_meanwhile(entry):
            self.store.update_transaction(anth, {"invoice_status": "matched", "invoice_id": "inv_anth",
                                                 "match_method": "manual", "match_note": "linked by ap@superhairpieces.com"})

        self.drive.on_upload = linked_meanwhile
        with mock.patch.object(self.drive, "trash", side_effect=DriveError("Drive PATCH files -> 500 backend")):
            self.assertEqual(self.send(anth).status_code, 409)
        file_id = self.drive.uploads[0]["id"]
        gemini = FakeGemini(handler=lambda parts, model: dict(FACTS))
        result = invoices.index_invoices(self.store, self.drive, gemini, "root", log=quiet)
        self.assertEqual(result["indexed"], 0)
        self.assertNotIn(file_id, self.drive.downloads)
        row = self.store.get_invoice(file_id)
        self.assertTrue(row["removed_at"])
        self.assertTrue(row["extraction_error"].startswith("upload withdrawn"))
        self.assertNotIn(file_id, [i["id"] for i in matching.candidate_invoices(self.store, 2026, 9)])

    def test_a_network_error_storing_the_placeholder_trashes_the_upload(self):
        supabase = SupabaseStore("https://x.supabase.co", "k")
        reset = requests.ConnectionError("connection reset")
        with mock.patch.object(store_module.requests, "request", side_effect=reset), \
                mock.patch.object(self.store, "upsert_invoices", side_effect=supabase.upsert_invoices):
            r = self.send(self.ids_["anth"])
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json()["error"], "database error: POST invoices -> ConnectionError: connection reset")
        self.assertEqual(self.drive.trashed, [self.drive.uploads[0]["id"]])

    def test_a_link_whose_answer_was_lost_is_kept(self):
        anth = self.ids_["anth"]
        real = self.store.update_transaction

        def lands_then_times_out(txn_id, patch, expect=None):
            real(txn_id, patch, expect=expect)
            raise StoreError("PATCH transactions -> ReadTimeout: read timed out")

        with mock.patch.object(self.store, "update_transaction", side_effect=lands_then_times_out):
            r = self.send(anth)
        self.assertEqual(r.status_code, 201, r.get_json())
        file_id = self.drive.uploads[0]["id"]
        self.assertEqual((r.get_json()["status"], r.get_json()["transaction"]["invoice_id"]), ("saved", file_id))
        self.assertEqual(self.drive.trashed, [])
        row = self.store.get_invoice(file_id)
        self.assertEqual((row["removed_at"], row["extraction_error"]), (None, None))
        t = self.store.get_transaction(anth)
        self.assertEqual((t["invoice_status"], t["invoice_id"]), ("matched", file_id))

    def test_a_store_failure_after_the_upload_trashes_the_file(self):
        anth = self.ids_["anth"]
        with mock.patch.object(self.store, "upsert_invoices", side_effect=StoreError("POST invoices -> 503")):
            r = self.send(anth)
        self.assertEqual(r.status_code, 502)
        self.assertIn("database error", r.get_json()["error"])
        self.assertEqual(self.drive.trashed, [self.drive.uploads[0]["id"]])
        t = self.store.get_transaction(anth)
        self.assertEqual((t["invoice_status"], t["invoice_id"]), ("missing", None))


class BackgroundReadTests(ReceiptApiTestCase):
    def read(self, **facts):
        """Upload the Anthropic receipt with Gemini answering `facts` (over FACTS); the row as the page polls it."""
        self.ai_on(handler=lambda parts, model: dict(FACTS, **facts))
        body = self.saved(self.ids_["anth"])
        self.assertTrue(body["reading"])
        self.assertEqual(self.drive.downloads, [])                          # read from the bytes in hand
        return self.row(self.ids_["anth"])

    def test_a_receipt_that_agrees_has_no_checks(self):
        row = self.read()
        self.assertEqual(row["checks"], [])
        self.assertEqual((row["invoice"]["total"], row["invoice"]["reading"], row["invoice"]["card_last4"],
                          row["invoice"]["is_invoice"], row["invoice"]["summary"]),
                         (150.0, False, "1610", True, "Claude Team seats"))
        self.assertTrue(row["invoice"]["extracted_at"])
        stored = self.store.get_invoice(row["invoice_id"])
        self.assertEqual((stored["modified_time"], stored["removed_at"], stored["folder_path"]),
                         ("2026-10-15T12:00:00.000Z", None, "/2026 09/Anthropic"))
        prompt = main.get_gemini().prompt_text()
        self.assertIn('file name "2026-09-04 Anthropic receipt.jpg"', prompt)
        self.assertNotIn("1610", prompt.split("Extract the facts")[0])
        self.assertEqual((row["invoice_status"], row["match_method"]), ("matched", "manual"))

    def test_another_amount(self):
        row = self.read(total=45.00)
        self.assertEqual(row["checks"], [{"kind": "amount", "level": "warn",
                                          "text": "Receipt reads CAD 45.00; the charge is CAD 150.00."}])
        self.assertEqual(row["invoice_status"], "matched")                  # shown, never a status change

    def test_another_card(self):
        row = self.read(card_last4="4894")
        self.assertEqual(row["checks"], [{"kind": "card", "level": "warn",
                                          "text": "The receipt shows card ending 4894, not 1610."}])

    def test_not_a_receipt(self):
        row = self.read(is_invoice=False, summary="a bank statement")
        self.assertEqual([(c["kind"], c["level"], c["text"]) for c in row["checks"]],
                         [("not_invoice", "warn", "This doesn't look like a receipt (a bank statement).")])

    def test_a_gemini_error_is_stored_for_the_index_to_retry(self):
        def failing(parts, model):
            raise GeminiError("Vertex AI 429 quota")

        self.ai_on(handler=failing)
        self.saved(self.ids_["anth"])
        row = self.row(self.ids_["anth"])
        self.assertEqual(row["checks"], [{"kind": "unreadable", "level": "info",
                                          "text": "Couldn't read the receipt yet; it is read again with the next invoice run."}])
        self.assertIn("quota", row["invoice"]["extraction_error"])
        self.assertEqual(self.drive.downloads, [])
        self.assertEqual(main._reading, set())

    def test_while_it_reads(self):
        pool = DeferredPool()
        self.ai_on(pool=pool)
        body = self.saved(self.ids_["anth"])
        file_id = body["file"]["id"]
        self.assertTrue(body["reading"])
        self.assertTrue(body["transaction"]["invoice"]["reading"])
        self.assertEqual(body["transaction"]["checks"], [{"kind": "unread", "level": "info", "text": "Reading the receipt…"}])
        self.assertEqual(main._reading, {file_id})
        pool.run()
        self.assertEqual(main._reading, set())
        row = self.row(self.ids_["anth"])
        self.assertEqual((row["invoice"]["reading"], row["checks"]), (False, []))

    def test_a_link_by_hand_gets_checks_too_but_a_matcher_link_does_not(self):
        self.inject_invoices()
        anth, amzn = self.ids_["anth"], self.ids_["amzn"]
        self.patch(anth, {"action": "link", "invoice_id": "inv_spare"})        # 999.99 against 150.00
        self.assertEqual([c["kind"] for c in self.row(anth)["checks"]], ["amount"])
        self.client.post("/api/match?year=2026&month=9")
        self.assertEqual(self.store.get_transaction(amzn)["match_method"], "rule")
        self.assertEqual(self.row(amzn)["checks"], [])
        self.patch(amzn, {"action": "confirm"})                                # "confirmed by": no checks
        self.assertEqual(self.row(amzn)["checks"], [])


class RemoveTests(ReceiptApiTestCase):
    def test_remove_unlinks_and_trashes(self):
        anth = self.ids_["anth"]
        file_id = self.saved(anth)["file"]["id"]
        body = self.remove(anth)
        self.assertEqual(body["trashed"], True)
        self.assertNotIn("warning", body)
        row = body["transaction"]
        self.assertEqual((row["invoice_status"], row["invoice_id"], row["invoice"], row["match_method"],
                          row["match_note"], row["checks"], row["editable"]),
                         ("missing", None, None, None, None, [], True))
        self.assertEqual(self.store.get_transaction(anth)["rejected_invoice_ids"], [])      # not "Not this"
        self.assertEqual(self.drive.trashed, [file_id])
        row = self.store.get_invoice(file_id)
        self.assertTrue(row["removed_at"])
        self.assertEqual(row["extraction_error"], "upload withdrawn: removed with Remove…")
        self.assertEqual(self.remove(anth, expect=409)["error"], "This invoice wasn't added with Add receipt…; use Unlink.")
        self.saved(anth)                                                      # and a new receipt can follow

    def test_a_link_made_by_hand_is_not_removed(self):
        self.inject_invoices()
        anth = self.ids_["anth"]
        self.patch(anth, {"action": "link", "invoice_id": "inv_spare"})
        self.assertEqual(self.remove(anth, expect=409)["error"], "This invoice wasn't added with Add receipt…; use Unlink.")
        self.assertEqual(self.store.get_transaction(anth)["invoice_id"], "inv_spare")
        self.assertEqual(self.drive.trashed, [])

    def test_a_file_that_belongs_to_another_purchase_is_not_removed(self):
        anth = self.ids_["anth"]
        file_id = self.saved(anth)["file"]["id"]
        _, entry = self.drive._find(file_id)
        entry["appProperties"]["apTxn"] = "someone-else"
        self.assertEqual(self.remove(anth, expect=409)["error"], "This invoice wasn't added with Add receipt…; use Unlink.")
        self.assertEqual(self.store.get_transaction(anth)["invoice_id"], file_id)
        self.assertEqual(self.drive.trashed, [])

    def remove_with_a_failing_trash(self, read_first):
        """Upload the Anthropic receipt (read at once when read_first, else left for the index), then
        Remove… it while Drive's trash fails. -> (response body, file id)."""
        if read_first:
            self.ai_on()
        anth = self.ids_["anth"]
        file_id = self.saved(anth)["file"]["id"]
        with mock.patch.object(self.drive, "trash", side_effect=DriveError("Drive PATCH files/x -> 500 backend error")):
            body = self.remove(anth)
        return body, file_id

    def test_a_trash_failure_still_unlinks(self):
        body, file_id = self.remove_with_a_failing_trash(read_first=False)
        self.assertEqual(body["trashed"], False)
        self.assertEqual(body["warning"], "Unlinked, but the file could not be moved to the Drive trash: "
                                          "Drive PATCH files/x -> 500 backend error. It won't be matched again; "
                                          "delete it from Invoice/2026 09/Anthropic by hand.")
        self.assertEqual(self.store.get_transaction(self.ids_["anth"])["invoice_status"], "missing")
        row = self.store.get_invoice(file_id)
        self.assertTrue(row["removed_at"])
        self.assertEqual(row["extraction_error"], main.REMOVED)

    def index_and_match_after_a_failed_trash(self, read_first):
        """The file is still in Invoice/2026 09/Anthropic: neither the next index run nor the month's
        match run may bring it back to the purchase."""
        _, file_id = self.remove_with_a_failing_trash(read_first)
        gemini = FakeGemini(handler=lambda parts, model: dict(FACTS))
        result = invoices.index_invoices(self.store, self.drive, gemini, "root", log=quiet)
        self.assertEqual((result["indexed"], result["removed"]), (0, 0))
        row = self.store.get_invoice(file_id)
        self.assertTrue(row["removed_at"])
        self.assertEqual(row["extraction_error"], main.REMOVED)
        self.assertEqual(row["total"], 150.0 if read_first else None)
        matching.match_month(self.store, None, 2026, 9, log=quiet)
        t = self.store.get_transaction(self.ids_["anth"])
        self.assertEqual((t["invoice_status"], t["invoice_id"]), ("missing", None))
        self.assertNotIn(file_id, self.store.used_invoice_ids())

    def test_a_read_file_whose_trash_failed_never_comes_back(self):
        self.index_and_match_after_a_failed_trash(read_first=True)

    def test_an_unread_file_whose_trash_failed_is_never_read_or_matched(self):
        self.index_and_match_after_a_failed_trash(read_first=False)

    def test_an_index_run_that_listed_before_the_remove_cannot_put_it_back_in_the_pool(self):
        self.ai_on()
        anth = self.ids_["anth"]
        file_id = self.saved(anth)["file"]["id"]
        stale = self.store.invoice_index()                                    # an index run starts...
        with mock.patch.object(self.drive, "trash", side_effect=DriveError("Drive PATCH files/x -> 500 backend error")):
            self.remove(anth)                                                 # ...Remove… lands, the trash fails...
        with mock.patch.object(self.store, "invoice_index", return_value=stale):
            invoices.index_invoices(self.store, self.drive, FakeGemini(), "root", log=quiet)
        row = self.store.get_invoice(file_id)                                 # ...and it refreshes the file as moved
        self.assertEqual((row["removed_at"], row["extraction_error"], row["total"]), (None, main.REMOVED, 150.0))
        self.assertNotIn(file_id, [i["id"] for i in matching.candidate_invoices(self.store, 2026, 9)])
        matching.match_month(self.store, None, 2026, 9, log=quiet)
        self.assertEqual(self.store.get_transaction(anth)["invoice_status"], "missing")

    def test_a_match_run_while_the_file_goes_to_the_trash_cannot_link_it(self):
        self.ai_on()                                                          # read: it has a total
        anth = self.ids_["anth"]
        file_id = self.saved(anth)["file"]["id"]
        real = self.drive.trash

        def trash_slowly(fid):
            matching.match_month(self.store, None, 2026, 9, log=quiet)     # the weekly job, or Re-run matching
            return real(fid)

        with mock.patch.object(self.drive, "trash", side_effect=trash_slowly):
            body = self.remove(anth)
        self.assertEqual(body["transaction"]["invoice_status"], "missing")
        t = self.store.get_transaction(anth)
        self.assertEqual((t["invoice_status"], t["invoice_id"]), ("missing", None))
        self.assertEqual(self.drive.trashed, [file_id])

    def test_a_remove_during_an_index_read_keeps_it_removed(self):
        anth = self.ids_["anth"]
        file_id = self.saved(anth)["file"]["id"]                              # AI off: the index reads it

        def read_while_removed(parts, model):
            self.remove(anth)
            return dict(FACTS)

        result = invoices.index_invoices(self.store, self.drive, FakeGemini(handler=read_while_removed), "root",
                                         workers=1, log=quiet)
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(self.drive.trashed, [file_id])
        row = self.store.get_invoice(file_id)
        self.assertTrue(row["removed_at"])
        self.assertEqual(row["extraction_error"], main.REMOVED)
        matching.match_month(self.store, None, 2026, 9, log=quiet)
        self.assertEqual(self.store.get_transaction(anth)["invoice_status"], "missing")

    def test_a_file_already_gone_from_drive(self):
        anth = self.ids_["anth"]
        file_id = self.saved(anth)["file"]["id"]
        parent, entry = self.drive._find(file_id)
        self.drive.tree[parent].remove(entry)
        body = self.remove(anth)
        self.assertEqual((body["trashed"], body["transaction"]["invoice_status"]), (True, "missing"))
        self.assertNotIn("trash", self.drive.calls)
        self.assertTrue(self.store.get_invoice(file_id)["removed_at"])

    def test_who_may_remove_and_after_the_card_is_switched_off(self):
        anth = self.ids_["anth"]
        self.saved(anth)
        with mock.patch.object(main, "current_user", return_value=dict(MEMBER)):
            self.assertEqual(self.remove(anth, expect=403)["error"],
                             "Only the card's owner or accounts payable can add receipts to card 1610.")
        self.remove("nope", expect=404)
        with mock.patch.object(main, "RECEIPT_UPLOAD_CARDS", set()):
            self.remove(anth)

    def test_a_purchase_changed_meanwhile(self):
        anth = self.ids_["anth"]
        self.saved(anth)
        real = self.store.update_transaction
        file_id = self.store.get_transaction(anth)["invoice_id"]
        with mock.patch.object(self.store, "update_transaction",
                               side_effect=lambda tid, patch, expect=None: None if expect else real(tid, patch)):
            self.assertEqual(self.remove(anth, expect=409)["error"], "This purchase changed meanwhile; reload.")
        self.assertEqual(self.drive.trashed, [])
        row = self.store.get_invoice(file_id)                                  # the stamp is taken back
        self.assertEqual((row["removed_at"], row["extraction_error"]), (None, None))

    def test_a_read_that_finishes_after_remove_keeps_it_removed(self):
        pool = DeferredPool()
        self.ai_on(pool=pool)
        anth = self.ids_["anth"]
        file_id = self.saved(anth)["file"]["id"]
        self.remove(anth)
        pool.run()
        row = self.store.get_invoice(file_id)
        self.assertEqual(row["total"], 150.0)                                   # the read did land...
        self.assertTrue(row["removed_at"])                                      # ...without bringing it back
        self.assertEqual(row["extraction_error"], main.REMOVED)
        self.assertNotIn(file_id, [i["id"] for i in matching.candidate_invoices(self.store, 2026, 9)])
        self.assertEqual(self.store.get_transaction(anth)["invoice_status"], "missing")

    def test_a_read_that_fails_after_remove_keeps_the_marker(self):
        def failing(parts, model):
            raise GeminiError("Vertex AI 429 quota")

        pool = DeferredPool()
        self.ai_on(handler=failing, pool=pool)
        anth = self.ids_["anth"]
        file_id = self.saved(anth)["file"]["id"]
        self.remove(anth)
        pool.run()
        row = self.store.get_invoice(file_id)
        self.assertEqual(row["extraction_error"], main.REMOVED)
        self.assertTrue(row["removed_at"])


class IndexAndMatcherTests(ReceiptApiTestCase):
    def test_the_index_reads_an_upload_once_and_not_before_its_placeholder(self):
        anth = self.ids_["anth"]
        file_id = self.saved(anth)["file"]["id"]
        self.assertNotIn(file_id, [i["id"] for i in matching.candidate_invoices(self.store, 2026, 9)])
        gemini = FakeGemini(handler=lambda parts, model: dict(FACTS))
        first = invoices.index_invoices(self.store, self.drive, gemini, "root", log=quiet)
        self.assertEqual((first["indexed"], first["removed"]), (1, 0))
        self.assertEqual(self.drive.downloads, [file_id])
        second = invoices.index_invoices(self.store, self.drive, gemini, "root", log=quiet)
        self.assertEqual((second["indexed"], second["removed"]), (0, 0))
        self.assertEqual(self.drive.downloads, [file_id])
        row = self.store.get_invoice(file_id)
        self.assertEqual((row["total"], row["folder_path"], row["removed_at"]), (150.0, "/2026 09/Anthropic", None))
        self.assertEqual(self.row(anth)["checks"], [])
        self.assertEqual(self.store.get_transaction(anth)["invoice_id"], file_id)

    def test_after_a_background_read_the_index_reads_nothing(self):
        self.ai_on()
        file_id = self.saved(self.ids_["anth"])["file"]["id"]
        result = invoices.index_invoices(self.store, self.drive, FakeGemini(), "root", log=quiet)
        self.assertEqual((result["indexed"], result["removed"]), (0, 0))
        self.assertEqual(self.drive.downloads, [])
        self.assertEqual(self.store.get_invoice(file_id)["total"], 150.0)

    def test_a_match_run_from_before_the_upload_leaves_it_alone(self):
        anth = self.ids_["anth"]
        self.store.upsert_invoices([inv("inv_anth", "Anthropic", 150.00, "2026-09-03")])
        stale = self.store.list_transactions(2026, 9)
        file_id = self.saved(anth)["file"]["id"]
        with mock.patch.object(self.store, "list_transactions", return_value=stale):
            counts = matching.match_month(self.store, None, 2026, 9, log=quiet)
        self.assertEqual(counts["skipped"], 1)
        t = self.store.get_transaction(anth)
        self.assertEqual((t["invoice_id"], t["match_method"]), (file_id, "manual"))


class RateLimitTests(ReceiptApiTestCase):
    def test_the_third_receipt_in_the_window_is_refused(self):
        anth = self.ids_["anth"]
        with mock.patch.object(main, "RECEIPT_MAX", 2):
            self.assertEqual(self.send(anth).status_code, 201)
            self.assertEqual(self.send(anth).status_code, 409)               # counted too
            r = self.send(self.ids_["amzn"])
            self.assertEqual((r.status_code, r.get_json()["error"]),
                             (429, "Too many receipts in ten minutes; wait a few minutes and try again."))
            self.assertEqual(len(self.drive.uploads), 1)
            # Someone else is not held up, and the window moves on.
            with mock.patch.object(main, "current_user", return_value={**MEMBER, "role": "ap"}):
                self.assertEqual(self.send(self.ids_["amzn"]).status_code, 201)
            main._receipt_times["anonymous"] = [time.time() - main.RECEIPT_WINDOW - 1] * 2
            self.assertNotEqual(self.send(anth).status_code, 429)


class RedirectTests(ReceiptApiTestCase):
    def test_the_chat_link_redirects_to_the_receipts_list(self):
        for path, location in (("/receipts/4421/2026/09", "/#/2026/09?card=4421&view=receipts"),
                               ("/receipts/1610/2026/9", "/#/2026/09?card=1610&view=receipts"),
                               ("/receipts/0001/2100/12", "/#/2100/12?card=0001&view=receipts")):
            r = self.client.get(path)
            self.assertEqual((r.status_code, r.headers["Location"]), (302, location), path)
        with mock.patch.object(main, "current_user", return_value=None):      # IAP guards the path itself
            self.assertEqual(self.client.get("/receipts/4421/2026/09").status_code, 302)

    def test_anything_else_is_404(self):
        for path in ("/receipts/44a1/2026/09", "/receipts/4421/2026/13", "/receipts/4421/2026/0",
                     "/receipts/4421/2026/009", "/receipts/44211/2026/09", "/receipts/442/2026/09",
                     "/receipts/4421/1999/09", "/receipts/4421/2101/01", "/receipts/4421/26/09",
                     "/receipts/%D9%A4%D9%A4%D9%A2%D9%A1/2026/09", "/receipts/4421/2026/09/extra",
                     "/receipts/4421/2026/-1"):
            self.assertEqual(self.client.get(path).status_code, 404, path)


class ChatReceiptsTests(ReceiptApiTestCase):
    def card(self, **kwargs):
        missing = self.store.list_transactions(2026, 9, "1610", statuses=["missing"])
        return chat.missing_invoices_card({"last4": "1610", "label": "Visa 1610"}, 2026, 9, missing, [],
                                          "https://ap.test", "https://drive.google.com/drive/folders/abc", **kwargs)

    def test_add_receipts_replaces_the_invoice_folder_button(self):
        with_receipts = self.card(receipts_url="https://ap.test/receipts/1610/2026/09")
        plain = self.card()
        sections = with_receipts["cardsV2"][0]["card"]["sections"]
        widgets = sections[-1]["widgets"]
        buttons = widgets[1]["buttonList"]["buttons"]
        self.assertEqual([b["text"] for b in buttons], ["Add receipts", "Open dashboard"])
        self.assertEqual(buttons[0], {"text": "Add receipts",
                                      "onClick": {"openLink": {"url": "https://ap.test/receipts/1610/2026/09"}},
                                      "color": {"red": 0.0, "green": 0.478, "blue": 1.0, "alpha": 1.0}})
        self.assertEqual(buttons[1], {"text": "Open dashboard",
                                      "onClick": {"openLink": {"url": "https://ap.test/#/2026/09?card=1610"}}})
        self.assertEqual(widgets[0]["textParagraph"]["text"],
                         "Tap Add receipts to photograph or upload each receipt from your phone or computer: it is "
                         "saved in this month's Invoice folder under the vendor's name and linked to the purchase. "
                         "Open the dashboard to confirm possible matches, add the cost centre and usage, or mark a "
                         "charge as having no invoice.")
        # Head, sections and card id are the same as without it.
        self.assertEqual(sections[:-1], plain["cardsV2"][0]["card"]["sections"][:-1])
        self.assertEqual(with_receipts["cardsV2"][0]["cardId"], plain["cardsV2"][0]["cardId"])
        self.assertEqual([b["text"] for b in plain["cardsV2"][0]["card"]["sections"][-1]["widgets"][1]["buttonList"]["buttons"]],
                         ["Open dashboard", "Invoice folder"])

    def test_notify_links_cards_with_receipt_upload_to_their_receipts_list(self):
        with mock.patch.object(chat, "post", side_effect=AssertionError("dry run must not post")):
            r = self.client.post("/api/notify?year=2026&month=9&dry_run=1")
        results = {c["card"]: c for c in r.get_json()["cards"]}
        p1610 = results["1610"]["payload"]
        self.assertIn('"https://ap.test/receipts/1610/2026/09"', json.dumps(p1610))
        buttons = p1610["cardsV2"][0]["card"]["sections"][-1]["widgets"][1]["buttonList"]["buttons"]
        self.assertEqual([b["text"] for b in buttons], ["Add receipts", "Open dashboard"])
        p4894 = results["4894"]["payload"]
        self.assertNotIn("/receipts/", json.dumps(p4894))
        widgets = p4894["cardsV2"][0]["card"]["sections"][-1]["widgets"]
        self.assertEqual([b["text"] for b in widgets[1]["buttonList"]["buttons"]], ["Open dashboard"])
        self.assertTrue(widgets[0]["textParagraph"]["text"].startswith("Upload the invoice or receipt"))

    def test_sent_cards_and_the_fallback_without_a_dashboard_url(self):
        with mock.patch.object(chat, "post", return_value=(True, "sent")) as post:
            main.notify_month(2026, 9, card="1610", sent_by="test")
            main.notify_month(2026, 9, card="1610", sent_by="test", dashboard="")
        with_base, without = (call.args[1] for call in post.call_args_list)
        self.assertEqual([b["text"] for b in with_base["cardsV2"][0]["card"]["sections"][-1]["widgets"][1]["buttonList"]["buttons"]],
                         ["Add receipts", "Open dashboard"])
        buttons = without["cardsV2"][0]["card"]["sections"][-1]["widgets"][1]["buttonList"]["buttons"]
        self.assertEqual([b["text"] for b in buttons], ["Invoice folder"])
        self.assertEqual(buttons[0]["onClick"]["openLink"]["url"], "https://drive.google.com/drive/folders/root")


class IapReceiptTests(ReceiptApiTestCase):
    """Behind IAP the upload rides on a cookie, so it must come from the page (X-Requested-With)."""

    AUDIENCE = "/projects/1/locations/us-central1/services/ap-reconciliation"

    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from google.auth.crypt import es256
        key = ec.generate_private_key(ec.SECP256R1())
        private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()
        public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        cls.signer = es256.ES256Signer.from_string(private_pem, key_id="iap-test-key")
        cls.certs = {"iap-test-key": public_pem}

    def setUp(self):
        super().setUp()
        main._token_cache.clear()
        main._iap_keys.update({"fetched": 0.0, "keys": {}})
        for name, value in (("AUTH_DISABLED", False), ("IAP_AUDIENCE", self.AUDIENCE),
                            ("fetch_iap_keys", mock.Mock(return_value=self.certs))):
            p = mock.patch.object(main, name, value)
            p.start()
            self.addCleanup(p.stop)

    def signed(self, email="manne@superhairpieces.com"):
        from google.auth import jwt
        now = int(time.time())
        token = jwt.encode(self.signer, {"iss": "https://cloud.google.com/iap", "aud": self.AUDIENCE, "sub": "s",
                                         "email": email, "iat": now, "exp": now + 600}).decode()
        return {"X-Goog-IAP-JWT-Assertion": token}

    def test_writes_need_the_page_header(self):
        anth, ap = self.ids_["anth"], self.signed()
        r = self.send(anth, headers=ap)
        self.assertEqual((r.status_code, r.get_json()["error"]),
                         (403, "changes must come from the dashboard page (X-Requested-With header)"))
        self.assertEqual(self.drive.calls, [])
        self.assertEqual(self.client.get(f"/api/transactions/{anth}/receipt-target", headers=ap).status_code, 200)
        body = self.saved(anth, headers={**ap, **PAGE})
        self.assertEqual(body["transaction"]["match_note"], "receipt uploaded by manne@superhairpieces.com")
        self.assertEqual(self.remove(anth, expect=403, headers=ap)["error"],
                         "changes must come from the dashboard page (X-Requested-With header)")
        self.remove(anth, headers={**ap, **PAGE})
        member = self.signed("yin.wei@superhairpieces.com")
        self.assertEqual(self.send(anth, headers={**member, **PAGE}).status_code, 403)
        self.assertEqual(self.send(anth).status_code, 401)


if __name__ == "__main__":
    unittest.main()
