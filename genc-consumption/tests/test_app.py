"""GenC Consumption: sheet parsing, the combined data view, entry rules and IAP sign-in.

Standard library plus the service's own requirements; no network. From genc-consumption:
    python -m unittest discover -s tests -v
"""
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))
os.environ.setdefault("STORE", "memory")
os.environ.pop("AUTH_DISABLED", None)

import main  # noqa: E402
import sheet  # noqa: E402
from store import MemoryStore  # noqa: E402

AUDIENCE = "/projects/83164121214/locations/northamerica-northeast1/services/genc-consumption"
HEADER = ["Month Ref", "Date", "SKU", "Note", "Qty", "Cost CAD", "Subtotal Amount\nCAD", "Request By",
          "Sent To", "Process Date", "拿货", "出库"]
DETAIL = [
    HEADER,
    [7, 46224, "Skin79_Animal Mask_Panda_1pc", "客户需求", 10, 0.963, 9.63, "Linda", "", 46224, True, True],
    [8, 46238, "SP_Bubble Mailer_Pink_8''x12''", "跨部门", 1, 5.22, 5.22, "Elvin", "包装用", 46238, True, False],
    ["", "", "", "", "", "", "", "", "", "", False, False],
    [8, "2026-08-21", "Typed date row", "视频需要", 2, "", "", "Amber", "", "", "TRUE", "FALSE"],
]
REASONS = [["视频需要"], ["样品需要"], ["客户需求"], ["跨部门"]]
COSTS = [["SKU", "Cost USD", "Cost CAD"], ["Medicube_PDRN Pink Moist Cream_50ml", 8.94, 12.945],
         ["VGR_Clipper 001_BK", 29.69, 43.01], ["VGR_Clipper 001_BK", 1, 99], ["Broken", "", "#N/A"]]


class FakeSheet(sheet.SheetClient):
    def __init__(self, detail=DETAIL, fail=False):
        super().__init__("sheet-id")
        self.title = "office use"
        self.detail_values, self.fail = detail, fail

    def _batch_get(self, ranges):
        if self.fail:
            raise sheet.SheetError("Google Sheets answered 403")
        out = []
        for r in ranges:
            if r.startswith("'明细'"):
                out.append(self.detail_values)
            elif r.startswith("'Note Ref'"):
                out.append(REASONS)
            else:
                out.append(COSTS)
        return out


class ParsingTests(unittest.TestCase):
    def test_detail_rows_by_header_with_serial_and_typed_dates(self):
        rows = sheet.parse_detail(DETAIL)
        self.assertEqual(len(rows), 3, "the blank row is skipped")
        first = rows[0]
        self.assertEqual((first["entry_date"], first["sku"], first["qty"], first["subtotal_cad"], first["sheet_row"]),
                         ("2026-07-21", "Skin79_Animal Mask_Panda_1pc", 10.0, 9.63, 2))
        self.assertEqual((rows[1]["picked_up"], rows[1]["issued"], rows[1]["sent_to"]), (True, False, "包装用"))
        self.assertEqual((rows[2]["entry_date"], rows[2]["unit_cost_cad"], rows[2]["subtotal_cad"], rows[2]["picked_up"]),
                         ("2026-08-21", None, None, True))

    def test_moved_columns_are_still_found(self):
        moved = [[h for h in reversed(HEADER)]] + [list(reversed(r)) for r in DETAIL[1:]]
        self.assertEqual(sheet.parse_detail(moved)[0]["sku"], "Skin79_Animal Mask_Panda_1pc")

    def test_missing_required_header_is_an_error(self):
        with self.assertRaises(sheet.SheetError):
            sheet.parse_detail([["Date", "Qty"], [46224, 1]])

    def test_costs_first_row_wins_and_lookup_is_case_insensitive(self):
        index = sheet.CostIndex(sheet.parse_costs(COSTS))
        self.assertEqual(index.lookup("VGR_Clipper 001_BK"), ("VGR_Clipper 001_BK", 43.01))
        self.assertEqual(index.lookup("vgr_clipper 001_bk"), ("VGR_Clipper 001_BK", 43.01))
        self.assertIsNone(index.lookup("Broken"))
        self.assertEqual([h["sku"] for h in index.search("pdrn cream")], ["Medicube_PDRN Pink Moist Cream_50ml"])
        self.assertEqual(index.search(""), [])

    def test_reasons_fall_back_to_the_four_defaults(self):
        self.assertEqual(sheet.parse_reasons([]), sheet.DEFAULT_REASONS)


class ApiTestCase(unittest.TestCase):
    PAGE = {"X-Requested-With": "XMLHttpRequest"}

    def setUp(self):
        self.store, self.sheet = MemoryStore(), FakeSheet()
        main.set_clients(store=self.store, sheet=self.sheet)
        self.patches = [mock.patch.object(main, "AUTH_DISABLED", True)]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.client = main.app.test_client()

    def post(self, body, headers=None):
        return self.client.post("/api/entries", json=body, headers={**self.PAGE, **(headers or {})})

    def entry(self, **over):
        body = {"entry_date": "2026-10-14", "reason": "客户需求", "request_by": "Amber", "sent_to": "展会",
                "items": [{"sku": "medicube_pdrn pink moist cream_50ml", "qty": 4}]}
        body.update(over)
        return body


class EntryTests(ApiTestCase):
    def test_new_line_takes_cost_ref_cost_and_spelling(self):
        r = self.post(self.entry())
        self.assertEqual(r.status_code, 201, r.get_json())
        line = r.get_json()["created"][0]
        self.assertEqual((line["sku"], line["unit_cost_cad"], line["cost_source"], line["subtotal_cad"], line["source"]),
                         ("Medicube_PDRN Pink Moist Cream_50ml", 12.945, "cost_ref", 4 * 12.945, "app"))
        self.assertEqual((line["created_by"], line["picked_up"], line["issued"]), ("local", False, False))

    def test_a_cost_ref_price_cannot_be_overridden_but_unknown_skus_take_a_typed_cost(self):
        r = self.post(self.entry(items=[{"sku": "VGR_Clipper 001_BK", "qty": 1, "unit_cost_cad": 1},
                                        {"sku": "Brand new thing", "qty": 2, "unit_cost_cad": "3.5"},
                                        {"sku": "Unknown, no cost", "qty": 1}]))
        lines = r.get_json()["created"]
        self.assertEqual([(l["unit_cost_cad"], l["cost_source"]) for l in lines],
                         [(43.01, "cost_ref"), (3.5, "manual"), (None, "none")])
        self.assertIsNone(lines[2]["subtotal_cad"])

    def test_validation(self):
        cases = [
            (self.entry(reason="Lunch"), "reason must be one of"),
            (self.entry(items=[]), "add at least one item"),
            (self.entry(items=[{"sku": "x", "qty": 0}]), "qty must be above 0"),
            (self.entry(items=[{"sku": "", "qty": 1}]), "SKU is required"),
            (self.entry(entry_date="2019-12-31"), "out of range"),
            (self.entry(entry_date="soon"), "must be a date"),
            (self.entry(request_by=" "), "requested by is required"),
        ]
        for body, message in cases:
            with self.subTest(message):
                r = self.post(body)
                self.assertEqual(r.status_code, 400)
                self.assertIn(message, r.get_json()["error"])
        self.assertEqual(self.store.list_entries(), [])

    def test_ticking_stocked_out_fills_the_process_date(self):
        line = self.post(self.entry()).get_json()["created"][0]
        r = self.client.patch(f"/api/entries/{line['id']}", json={"issued": True}, headers=self.PAGE)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["entry"]["process_date"], main.today().isoformat())
        self.assertEqual(r.get_json()["entry"]["updated_by"], "local")

    def test_resaving_the_same_sku_does_not_reprice(self):
        line = self.post(self.entry()).get_json()["created"][0]
        COSTS[1][2] = 20.0
        self.sheet._costs = None
        self.addCleanup(lambda: COSTS[1].__setitem__(2, 12.945))
        r = self.client.patch(f"/api/entries/{line['id']}", json={"sku": line["sku"], "qty": 5}, headers=self.PAGE)
        self.assertEqual(r.get_json()["entry"]["unit_cost_cad"], 12.945)
        r = self.client.patch(f"/api/entries/{line['id']}", json={"sku": "VGR_Clipper 001_BK"}, headers=self.PAGE)
        self.assertEqual((r.get_json()["entry"]["unit_cost_cad"], r.get_json()["entry"]["cost_source"]), (43.01, "cost_ref"))

    def test_delete_is_soft(self):
        line = self.post(self.entry()).get_json()["created"][0]
        self.assertEqual(self.client.delete(f"/api/entries/{line['id']}", headers=self.PAGE).status_code, 200)
        self.assertEqual(self.store.list_entries(), [])
        self.assertTrue(self.store.rows[line["id"]]["deleted_at"])
        self.assertEqual(self.client.delete(f"/api/entries/{line['id']}", headers=self.PAGE).status_code, 404)

    def test_sheet_rows_cannot_be_changed_through_the_api(self):
        r = self.client.patch("/api/entries/sheet-2", json={"issued": True}, headers=self.PAGE)
        self.assertEqual(r.status_code, 404)


class DataTests(ApiTestCase):
    def test_both_sources_in_one_list(self):
        self.post(self.entry())
        data = self.client.get("/api/data").get_json()
        self.assertEqual([r["source"] for r in data["rows"]], ["sheet", "sheet", "sheet", "app"])
        self.assertEqual(data["reasons"], ["视频需要", "样品需要", "客户需求", "跨部门"])
        self.assertTrue(data["rows"][0]["sheet_url"].endswith("#gid=0&range=A2:L2"))
        self.assertEqual(data["sheet"]["title"], "office use")
        self.assertEqual(data["warnings"], [])

    def test_sheet_down_still_shows_new_lines_and_warns(self):
        self.post(self.entry())
        main.set_clients(store=self.store, sheet=FakeSheet(fail=True))
        data = self.client.get("/api/data").get_json()
        self.assertEqual([r["source"] for r in data["rows"]], ["app"])
        self.assertIn("Google Sheet could not be read", data["warnings"][0])
        # ...but no new line can be priced, so none is accepted.
        r = self.post(self.entry())
        self.assertEqual(r.status_code, 503)

    def test_the_last_good_sheet_read_is_served_when_a_refresh_fails(self):
        self.sheet.detail()
        self.sheet.fail = True
        self.sheet._detail = (time.time() - 3600,) + self.sheet._detail[1:]
        rows, _, _ = self.sheet.detail()
        self.assertEqual(len(rows), 3)

    def test_sku_search(self):
        self.assertEqual(self.client.get("/api/skus?q=clipper").get_json()["skus"],
                         [{"sku": "VGR_Clipper 001_BK", "unit_cost_cad": 43.01}])
        self.assertEqual(self.client.get("/api/skus?q=c").get_json()["skus"], [])

    def test_health_deep_reports_counts_only(self):
        body = self.client.get("/health?deep=1").get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["checks"]["sheet"], {"ok": True, "rows": 3, "reasons": 4})
        self.assertEqual(body["checks"]["cost_ref"], {"ok": True, "skus": 2})


class IapTests(ApiTestCase):
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
        cls.signer = es256.ES256Signer.from_string(private_pem, key_id="k1")
        cls.certs = {"k1": public_pem}

    def setUp(self):
        super().setUp()
        main._token_cache.clear()
        main._iap_keys.update({"fetched": 0.0, "keys": {}})
        for p in (mock.patch.object(main, "AUTH_DISABLED", False), mock.patch.object(main, "IAP_AUDIENCE", AUDIENCE),
                  mock.patch.object(main, "fetch_iap_keys", mock.Mock(return_value=self.certs))):
            p.start()
            self.addCleanup(p.stop)

    def token(self, email, **claims):
        from google.auth import jwt
        now = int(time.time())
        payload = {"iss": "https://cloud.google.com/iap", "aud": AUDIENCE, "email": email, "iat": now, "exp": now + 600}
        payload.update(claims)
        return jwt.encode(self.signer, payload).decode()

    def as_user(self, email):
        return {"X-Goog-IAP-JWT-Assertion": self.token(email)}

    def test_no_assertion_no_data(self):
        self.assertEqual(self.client.get("/api/data").status_code, 401)
        self.assertEqual(self.client.get("/api/skus?q=clip").status_code, 401)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_assertions_that_do_not_fit_are_rejected(self):
        for headers in ({"X-Goog-IAP-JWT-Assertion": self.token("someone@gmail.com")},
                        {"X-Goog-IAP-JWT-Assertion": self.token("amber@superhairpieces.com", aud="/projects/1/other")},
                        {"X-Goog-IAP-JWT-Assertion": self.token("amber@superhairpieces.com", iss="https://evil")},
                        {"X-Goog-IAP-JWT-Assertion": "not-a-jwt"}):
            self.assertEqual(self.client.get("/api/me", headers=headers).status_code, 401)

    def test_me(self):
        me = self.client.get("/api/me", headers=self.as_user("Wen.Yu@superhairpieces.com")).get_json()
        self.assertEqual(me, {"email": "wen.yu@superhairpieces.com", "name": "Wen Yu", "admin": False})
        self.assertTrue(self.client.get("/api/me", headers=self.as_user("anais@superhairpieces.com")).get_json()["admin"])

    def test_changes_need_the_page_header(self):
        r = self.client.post("/api/entries", json=self.entry(), headers=self.as_user("amber@superhairpieces.com"))
        self.assertEqual(r.status_code, 403)

    def test_only_the_author_or_an_admin_changes_a_line_but_anyone_ticks_status(self):
        amber, jacob, admin = (self.as_user(e) for e in ("amber@superhairpieces.com", "jacob@superhairpieces.com",
                                                         "manne@superhairpieces.com"))
        line = self.post(self.entry(), amber).get_json()["created"][0]
        self.assertEqual(line["created_by"], "amber@superhairpieces.com")
        url = f"/api/entries/{line['id']}"
        r = self.client.patch(url, json={"qty": 9}, headers={**self.PAGE, **jacob})
        self.assertEqual(r.status_code, 403)
        r = self.client.patch(url, json={"picked_up": True}, headers={**self.PAGE, **jacob})
        self.assertEqual((r.status_code, r.get_json()["entry"]["picked_up"], r.get_json()["entry"]["can_edit"]), (200, True, False))
        self.assertEqual(self.client.delete(url, headers={**self.PAGE, **jacob}).status_code, 403)
        data = self.client.get("/api/data", headers=jacob).get_json()
        self.assertFalse([r for r in data["rows"] if r["source"] == "app"][0]["can_edit"])
        self.assertEqual(self.client.patch(url, json={"qty": 9}, headers={**self.PAGE, **admin}).status_code, 200)
        self.assertEqual(self.client.delete(url, headers={**self.PAGE, **amber}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
