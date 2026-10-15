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
import skuvault  # noqa: E402
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

    def test_marking_stocked_out_by_hand_fills_the_process_date(self):
        line = self.post(self.entry()).get_json()["created"][0]
        r = self.client.patch(f"/api/entries/{line['id']}", json={"stock_out": "manual"}, headers=self.PAGE)
        self.assertEqual(r.status_code, 200, r.get_json())
        entry = r.get_json()["entry"]
        self.assertEqual((entry["issued"], entry["skuvault_status"], entry["process_date"], entry["updated_by"]),
                         (True, "manual", main.today().isoformat(), "local"))
        r = self.client.patch(f"/api/entries/{line['id']}", json={"stock_out": "no"}, headers=self.PAGE)
        self.assertEqual((r.get_json()["entry"]["issued"], r.get_json()["entry"]["skuvault_status"]), (False, "none"))

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
        r = self.client.patch("/api/entries/sheet-2", json={"stock_out": "manual"}, headers=self.PAGE)
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


class FakeSkuVault:
    """Bins per SKU; records every removal; fail_after makes the nth removal fail."""

    def __init__(self, bins=None, fail_after=None, lookup_error=None):
        self.stock = bins if bins is not None else {
            "Medicube_PDRN Pink Moist Cream_50ml": [("WH1", "Z1[Z1G09]", 78), ("WH1", "Z1[Z1A03]", 2)],
            "VGR_Clipper 001_BK": [("WH1", "Z1[Z1A12]", 8)],
        }
        self.removals, self.fail_after, self.lookup_error, self.lookups = [], fail_after, lookup_error, 0

    def bins(self, skus):
        self.lookups += 1
        if self.lookup_error:
            raise skuvault.SkuVaultError(self.lookup_error)
        return {s: [{"warehouse_code": w, "warehouse_id": 1, "location_code": loc, "quantity": q}
                    for w, loc, q in self.stock.get(s, [])] for s in skus}

    def remove(self, sku, warehouse_id, location_code, qty, reason):
        if self.fail_after is not None and len(self.removals) >= self.fail_after:
            raise skuvault.SkuVaultError(f"SkuVault refused the removal from {location_code}: Reason not found")
        self.removals.append((sku, location_code, qty, reason))


class SkuVaultFlowTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.sv = FakeSkuVault()
        main.set_clients(store=self.store, sheet=self.sheet, skuvault=self.sv)

    def test_least_stock_first_spilling_into_the_next_bin(self):
        r = self.post(self.entry(stock_out="skuvault", items=[{"sku": "Medicube_PDRN Pink Moist Cream_50ml", "qty": 5}]))
        self.assertEqual(r.status_code, 201, r.get_json())
        line = r.get_json()["created"][0]
        self.assertEqual(self.sv.removals, [("Medicube_PDRN Pink Moist Cream_50ml", "Z1[Z1A03]", 2, "Customer Request"),
                                            ("Medicube_PDRN Pink Moist Cream_50ml", "Z1[Z1G09]", 3, "Customer Request")])
        self.assertEqual((line["skuvault_status"], line["issued"], line["process_date"], line["skuvault_error"]),
                         ("removed", True, main.today().isoformat(), None))
        self.assertEqual([(p["location"], p["qty"]) for p in line["skuvault_removed"]], [("Z1[Z1A03]", 2), ("Z1[Z1G09]", 3)])
        self.assertEqual(r.get_json()["skuvault"], {"removed": 1, "problems": []})

    def test_one_lookup_for_every_item_of_an_entry(self):
        self.post(self.entry(stock_out="skuvault", items=[{"sku": "VGR_Clipper 001_BK", "qty": 1},
                                                         {"sku": "Medicube_PDRN Pink Moist Cream_50ml", "qty": 1}]))
        self.assertEqual(self.sv.lookups, 1)
        self.assertEqual([x[1] for x in self.sv.removals], ["Z1[Z1A12]", "Z1[Z1A03]"])

    def test_not_enough_stock_saves_the_line_without_touching_skuvault(self):
        r = self.post(self.entry(stock_out="skuvault", items=[{"sku": "VGR_Clipper 001_BK", "qty": 9}]))
        line = r.get_json()["created"][0]
        self.assertEqual((r.status_code, line["skuvault_status"], line["issued"]), (201, "failed", False))
        self.assertIn("only 8 on hand", line["skuvault_error"])
        self.assertEqual(self.sv.removals, [])
        self.assertEqual(r.get_json()["skuvault"]["problems"][0]["status"], "failed")

    def test_a_partial_removal_is_recorded_and_the_retry_takes_only_the_rest(self):
        self.sv.fail_after = 1
        line = self.post(self.entry(stock_out="skuvault", items=[{"sku": "Medicube_PDRN Pink Moist Cream_50ml", "qty": 5}])).get_json()["created"][0]
        self.assertEqual((line["skuvault_status"], line["issued"]), ("partial", False))
        self.assertIn("Reason not found", line["skuvault_error"])
        self.sv.fail_after = None
        self.sv.stock["Medicube_PDRN Pink Moist Cream_50ml"] = [("WH1", "Z1[Z1G09]", 78)]     # the small bin is empty now
        r = self.client.patch(f"/api/entries/{line['id']}", json={"stock_out": "skuvault"}, headers=self.PAGE)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.sv.removals[-1], ("Medicube_PDRN Pink Moist Cream_50ml", "Z1[Z1G09]", 3, "Customer Request"))
        self.assertEqual(r.get_json()["entry"]["skuvault_status"], "removed")

    def test_removed_lines_keep_their_sku_and_quantity_and_stay_stocked_out(self):
        line = self.post(self.entry(stock_out="skuvault", items=[{"sku": "VGR_Clipper 001_BK", "qty": 2}])).get_json()["created"][0]
        url = f"/api/entries/{line['id']}"
        for body in ({"qty": 3}, {"sku": "Medicube_PDRN Pink Moist Cream_50ml"}, {"stock_out": "no"},
                     {"stock_out": "manual"}, {"stock_out": "skuvault"}):
            with self.subTest(body):
                self.assertEqual(self.client.patch(url, json=body, headers=self.PAGE).status_code, 400)
        r = self.client.patch(url, json={"sent_to": "展会", "picked_up": True}, headers=self.PAGE)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.sv.removals), 1)

    def test_lines_marked_by_hand_are_never_removed_again(self):
        line = self.post(self.entry(stock_out="manual")).get_json()["created"][0]
        self.assertEqual((line["issued"], line["skuvault_status"]), (True, "manual"))
        r = self.client.patch(f"/api/entries/{line['id']}", json={"stock_out": "skuvault"}, headers=self.PAGE)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.sv.removals, [])

    def test_whole_units_and_a_mapped_reason_are_needed_before_anything_is_saved(self):
        r = self.post(self.entry(stock_out="skuvault", items=[{"sku": "VGR_Clipper 001_BK", "qty": 1.5}]))
        self.assertEqual(r.status_code, 400)
        self.assertIn("whole units", r.get_json()["error"])
        with mock.patch.dict(main.SKUVAULT_REASONS, {}, clear=True):
            r = self.post(self.entry(stock_out="skuvault"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.store.list_entries(), [])

    def test_a_failed_lookup_leaves_the_line_failed(self):
        self.sv.lookup_error = "SkuVault is busy (rate limit shared with the other integrations); try again in a minute"
        line = self.post(self.entry(stock_out="skuvault")).get_json()["created"][0]
        self.assertEqual((line["skuvault_status"], line["skuvault_error"][:16]), ("failed", "SkuVault is busy"))

    def test_two_claims_on_one_line_cannot_both_win(self):
        line = self.post(self.entry()).get_json()["created"][0]
        stale_before = "2000-01-01T00:00:00Z"
        self.assertIsNotNone(self.store.claim_skuvault(line["id"], "a", stale_before))
        self.assertIsNone(self.store.claim_skuvault(line["id"], "b", stale_before))
        r = self.client.patch(f"/api/entries/{line['id']}", json={"stock_out": "skuvault"}, headers=self.PAGE)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.sv.removals, [])

    def test_without_skuvault_credentials_removal_is_refused(self):
        main.set_clients(store=self.store, sheet=self.sheet)
        with mock.patch.object(main, "SKUVAULT_EMAIL", ""):
            r = self.post(self.entry(stock_out="skuvault"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("not set up", r.get_json()["error"])


class SkuVaultClientTests(unittest.TestCase):
    def test_plan_least_stock_first(self):
        bins = [{"warehouse_code": "WH1", "location_code": "B", "quantity": 78},
                {"warehouse_code": "WH1", "location_code": "A", "quantity": 2},
                {"warehouse_code": "WH1", "location_code": "Z", "quantity": 0}]
        self.assertEqual([(b["location_code"], q) for b, q in skuvault.plan_removal(bins, 1)], [("A", 1)])
        self.assertEqual([(b["location_code"], q) for b, q in skuvault.plan_removal(bins, 80)], [("A", 2), ("B", 78)])
        with self.assertRaises(skuvault.NotEnoughStock):
            skuvault.plan_removal(bins, 81)

    def client(self, responses):
        session = mock.Mock()
        session.post.side_effect = responses
        return skuvault.SkuVaultClient("e", "p", warehouses=["WH1"], session=session), session

    @staticmethod
    def response(status=200, body=None, headers=None):
        r = mock.Mock(status_code=status, headers=headers or {}, text=str(body))
        r.json.return_value = body if body is not None else {}
        return r

    def test_bins_only_from_allowed_warehouses_and_not_reserved(self):
        c, _ = self.client([
            self.response(body={"TenantToken": "t", "UserToken": "u"}),
            self.response(body={"Items": {"SKU-1": [
                {"WarehouseCode": "WH1", "LocationCode": "Z1[Z1A03]", "Quantity": 19, "Reserve": False},
                {"WarehouseCode": "IT1", "LocationCode": "PKO", "Quantity": 5, "Reserve": False},
                {"WarehouseCode": "WH1", "LocationCode": "Z1[R]", "Quantity": 9, "Reserve": True}]}, "Errors": []}),
            self.response(body={"Warehouses": [{"Id": "1", "Code": "WH1"}, {"Id": "4", "Code": "IT1"}]}),
        ])
        self.assertEqual(c.bins(["sku-1"]), {"sku-1": [{"warehouse_code": "WH1", "warehouse_id": 1,
                                                       "location_code": "Z1[Z1A03]", "quantity": 19}]})

    def test_remove_must_say_success_and_a_short_throttle_is_waited_out(self):
        c, session = self.client([
            self.response(body={"TenantToken": "t", "UserToken": "u"}),
            self.response(status=429, headers={"X-RateLimit-Reset": "10"}),
            self.response(body={"RemoveItemStatus": "Success"}),
            self.response(body={"RemoveItemStatus": "NotEnoughQuantity"}),
        ])
        with mock.patch.object(skuvault.time, "sleep"):
            c.remove("SKU-1", 1, "Z1[Z1A03]", 2, "Sample")
        sent = session.post.call_args_list[2].kwargs["json"]
        self.assertEqual((sent["Sku"], sent["WarehouseId"], sent["LocationCode"], sent["Quantity"], sent["Reason"], sent["TenantToken"]),
                         ("SKU-1", 1, "Z1[Z1A03]", 2, "Sample", "t"))
        with self.assertRaises(skuvault.SkuVaultError):
            c.remove("SKU-1", 1, "Z1[Z1A03]", 2, "Sample")

    def test_a_long_throttle_fails_instead_of_waiting(self):
        c, _ = self.client([self.response(body={"TenantToken": "t", "UserToken": "u"}),
                            self.response(status=429, headers={"X-RateLimit-Reset": "45000"})])
        with self.assertRaises(skuvault.SkuVaultError) as ctx:
            c.remove("SKU-1", 1, "Z1", 1, "Sample")
        self.assertIn("busy", str(ctx.exception))


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
