"""The accounts-payable admin pages through the API (/admin, /api/admin/*), and what the coding split
changes elsewhere: the card's create flag, default cost centre and receipt upload switch on POST
/api/cards, the use-case snap and the card-default fill on PATCH /api/transactions/<id>, "uncoded" as
"no use case", the use-case list and suggestions on the dashboard's reads, and the audit.

Everything runs on store.MemoryStore in open mode (AUTH_DISABLED=1), where every caller is AP and the
X-Dashboard-User header names who is typing (the audit's actor)."""
import io
import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import MEMBER, ApiTestCase, inv, txn  # noqa: E402  (sets the env and imports main)

import admin  # noqa: E402
import assistant  # noqa: E402
import chat  # noqa: E402
import main  # noqa: E402
from store import MemoryStore, StoreError  # noqa: E402

STATIC = os.path.join(HERE, "..", "cloud_run", "static")
WHO = {"X-Dashboard-User": "manne"}
PAGE = {"X-Requested-With": "XMLHttpRequest"}
AP_IAP = {"email": "manne@superhairpieces.com", "name": "", "role": "ap", "picture": ""}
UUID_A = "00000000-0000-4000-8000-00000000000a"
UUID_B = "00000000-0000-4000-8000-00000000000b"
MISSING_TABLE = StoreError("GET use_cases -> 404 Could not find the table", status=404, code="PGRST205")


class AdminApiTestCase(ApiTestCase):
    """A fresh store per test (ApiTestCase) and an empty admin cache."""

    def setUp(self):
        super().setUp()
        admin.invalidate()

    def call(self, method, path, body=None, expect=200, headers=None):
        r = self.client.open(path, method=method, json=body, headers={**WHO, **(headers or {})})
        self.assertEqual(r.status_code, expect, (method, path, r.get_json()))
        return r.get_json()

    def get(self, path, expect=200):
        return self.call("GET", path, expect=expect)

    def card_save(self, body, expect=200):
        return self.call("POST", "/api/cards", body, expect)

    def audits(self, **filters):
        return self.store.list_audit(limit=200, **filters)

    def use_case(self, name):
        return next(u for u in self.store.list_use_cases() if u["name"] == name)

    def card(self, last4):
        return next(c for c in self.store.list_cards() if c["last4"] == last4)

    def months_sept(self):
        return self.get("/api/months")["months"][1]


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------
ROUTES = [
    ("GET", "/api/admin/settings", None),
    ("PUT", "/api/admin/settings/coding", {"suggestions": True}),
    ("GET", "/api/admin/cards", None),
    ("GET", "/api/admin/card-numbers/5758", None),
    ("POST", "/api/admin/card-numbers/5758/ignore", {"reason": "x"}),
    ("DELETE", "/api/admin/card-numbers/5758/ignore", None),
    ("DELETE", "/api/admin/cards/1610", None),
    ("POST", "/api/admin/cards/1610/fill-default", None),
    ("GET", "/api/admin/coding", None),
    ("POST", "/api/admin/cost-centers", {"name": "Warehouse"}),
    ("PATCH", "/api/admin/cost-centers?name=US", {"description": "x"}),
    ("POST", "/api/admin/cost-centers/order", {"names": []}),
    ("POST", "/api/admin/use-cases", {"name": "Training"}),
    ("PATCH", f"/api/admin/use-cases/{UUID_A}", {"name": "x"}),
    ("POST", "/api/admin/use-cases/order", {"ids": []}),
    ("GET", "/api/admin/vendors", None),
    ("GET", "/api/admin/vendors/folder?name=Esso", None),
    ("GET", "/api/admin/registry", None),
    ("POST", "/api/admin/vendors", {"name": "Esso"}),
    ("PATCH", f"/api/admin/vendors/{UUID_A}", {"name": "x"}),
    ("POST", f"/api/admin/vendors/{UUID_A}/folders", {"folder": "Esso"}),
    ("DELETE", f"/api/admin/vendor-folders/{UUID_A}", None),
    ("POST", f"/api/admin/vendors/{UUID_A}/patterns", {"pattern": "esso gas"}),
    ("PATCH", f"/api/admin/vendor-patterns/{UUID_A}", {"status": "off"}),
    ("DELETE", f"/api/admin/vendor-patterns/{UUID_A}", None),
    ("PUT", f"/api/admin/vendors/{UUID_A}/coding", {"group_name": "", "use_case_id": None}),
    ("GET", "/api/admin/proposals", None),
    ("POST", "/api/admin/proposals/dismiss", {"id": "x"}),
    ("GET", "/api/admin/people", None),
    ("PATCH", f"/api/admin/people/{UUID_A}", {"name": "x"}),
    ("POST", f"/api/admin/people/{UUID_A}/merge", {"into": UUID_B}),
    ("DELETE", f"/api/admin/people/{UUID_A}", None),
    ("POST", "/api/admin/people/link-holders", None),
    ("GET", "/api/admin/audit", None),
]


class AdminAccessTests(AdminApiTestCase):
    def test_every_admin_route_is_listed_here(self):
        """A new /api/admin route must be added to ROUTES, so the role checks below cover it."""
        served = {(rule.rule, method) for rule in main.app.url_map.iter_rules() if rule.rule.startswith("/api/admin")
                  for method in rule.methods - {"HEAD", "OPTIONS"}}
        listed = set()
        for method, path, _ in ROUTES:
            adapter = main.app.url_map.bind("localhost")
            rule, _args = adapter.match(path.split("?")[0], method=method, return_rule=True)
            listed.add((rule.rule, method))
        self.assertEqual(served, listed)

    def test_signed_out_is_401_and_a_member_403(self):
        self.upload()
        with mock.patch.object(main, "current_user", return_value=None):
            for method, path, body in ROUTES:
                self.assertEqual(self.client.open(path, method=method, json=body).status_code, 401, (method, path))
        with mock.patch.object(main, "current_user", return_value=dict(MEMBER)):
            for method, path, body in ROUTES:
                r = self.client.open(path, method=method, json=body)
                self.assertEqual(r.status_code, 403, (method, path))
                self.assertEqual(r.get_json()["error"], "only the accounts-payable team can do this")
        self.assertEqual(self.store.audit, [])
        self.assertIn("1610", {c["last4"] for c in self.store.list_cards()})

    def page_status(self):
        with self.client.get("/admin") as r:
            return r.status_code

    def test_the_page(self):
        with self.client.get("/admin") as r:
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.content_type.startswith("text/html"))
            self.assertIn("no-store", r.headers["Cache-Control"])
            self.assertEqual(r.headers["X-Frame-Options"], "DENY")
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("<title>AP Admin</title>", r.get_data(as_text=True))

    def test_behind_iap(self):
        with mock.patch.object(main, "AUTH_DISABLED", False), mock.patch.object(main, "IAP_AUDIENCE", "/projects/1/x"):
            self.assertEqual(self.page_status(), 200)                             # IAP guards the path itself
            self.assertEqual(self.client.get("/api/admin/cards").status_code, 401)
            with mock.patch.object(main, "current_user", return_value=dict(AP_IAP)):
                self.assertEqual(self.client.get("/api/admin/cards").status_code, 200)
                r = self.client.post("/api/admin/cost-centers", json={"name": "Warehouse"})
                self.assertEqual(r.status_code, 403)                                # not from the page
                r = self.client.post("/api/admin/cost-centers", json={"name": "Warehouse"}, headers=PAGE)
                self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(self.audits(entity="cost_center")[0]["actor"], "manne@superhairpieces.com")
        self.assertEqual(self.audits(entity="cost_center")[0]["auth_mode"], "iap")

    def test_writes_are_refused_on_a_real_database_open_to_anyone(self):
        with mock.patch.object(main, "STORE_KIND", "supabase"):
            r = self.client.post("/api/admin/cost-centers", json={"name": "Warehouse"})
            self.assertEqual(r.status_code, 403)
            self.assertIn("open to anyone", r.get_json()["error"])
            self.assertEqual(self.client.get("/api/admin/coding").status_code, 200)
        self.assertNotIn("Warehouse", [r["name"] for r in self.store.list_cost_center_rows()])


# ---------------------------------------------------------------------------
# Cards: POST /api/cards
# ---------------------------------------------------------------------------
class CardsCreateFlagTests(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload().status_code, 200)

    def test_an_unknown_card_needs_create(self):
        r = self.card_save({"last4": "1234", "label": "typo"}, expect=404)
        self.assertEqual(r["error"], "card 1234 is not on the dashboard; add it with create: true")
        self.assertNotIn("1234", {c["last4"] for c in self.store.list_cards()})
        r = self.card_save({"last4": "1234", "create": True})
        self.assertEqual((r["card"]["last4"], r["card"]["label"], r["card"]["active"]), ("1234", "Card ending 1234", True))
        self.assertEqual(self.card("1234")["active"], True)
        rows = self.audits(entity="card", entity_id="1234")
        self.assertEqual([(a["action"], a["before"], a["actor"]) for a in rows], [("create", None, "manne")])
        self.assertEqual(rows[0]["after"]["label"], "Card ending 1234")
        self.assertEqual(self.card_save({"last4": "1234", "create": True}, expect=409)["error"], "card 1234 already exists")

    def test_create_with_a_holder_labels_and_links_them(self):
        r = self.card_save({"last4": "1234", "create": True, "holder_name": "Linda", "owner_email": "Linda@superhairpieces.com",
                            "default_cost_center": "us", "receipt_upload": True})
        c = r["card"]
        self.assertEqual((c["label"], c["holder_name"], c["owner_email"], c["default_cost_center"], c["receipt_upload_setting"]),
                         ("Card ending 1234 - Linda", "Linda", "linda@superhairpieces.com", "US", True))
        self.assertEqual(c["person_id"], next(p["id"] for p in r["people"] if p["name"] == "Linda"))
        self.assertEqual(r["fillable"], 0)          # a new card with a default: nothing to fill yet

    def test_updates_are_audited_and_a_save_changing_nothing_is_not(self):
        self.card_save({"last4": "1610", "label": "Visa 1610", "owner_email": "yin@superhairpieces.com"})
        rows = self.audits(entity="card", entity_id="1610")
        self.assertEqual([a["action"] for a in rows], ["update"])
        self.assertEqual((rows[0]["before"]["label"], rows[0]["after"]["label"]), ("Card ending 1610 - Yin Wei", "Visa 1610"))
        self.assertEqual(rows[0]["after"]["owner_email"], "yin@superhairpieces.com")
        self.assertEqual(set(rows[0]["after"]), set(main.CARD_FIELDS))
        self.card_save({"last4": "1610", "label": "Visa 1610", "owner_email": "yin@superhairpieces.com"})
        self.assertEqual(len(self.audits(entity="card", entity_id="1610")), 1)

    def test_a_failed_audit_never_fails_the_save(self):
        with mock.patch.object(self.store, "insert_audit", side_effect=StoreError("audit table gone")), \
                mock.patch("builtins.print"):
            self.card_save({"last4": "1610", "label": "Visa 1610"})
        self.assertEqual(self.card("1610")["label"], "Visa 1610")

    def test_a_new_card_taking_receipt_numbers_from_another_is_said_first(self):
        self.store.upsert_cards([{"last4": "3922", "label": "Card ending 3922"}])
        r = self.card_save({"last4": "3914", "create": True}, expect=409)
        self.assertEqual({c["number"] for c in r["alias_changes"]}, {"3906", "3914", "3930"})
        self.assertTrue(all(c["before"] == "3922" for c in r["alias_changes"]))
        self.assertIn("send confirm: true", r["error"])
        self.assertNotIn("3914", {c["last4"] for c in self.store.list_cards()})
        self.card_save({"last4": "3914", "create": True, "confirm": True})
        self.assertIn("3914", {c["last4"] for c in self.store.list_cards()})

    def test_expect_card_number(self):
        self.card_save({"last4": "1610", "card_number": "4449"})
        r = self.card_save({"last4": "1610", "card_number": "5758", "expect_card_number": ""}, expect=409)
        self.assertEqual(r["card_number"], "4449")
        self.assertTrue(r["error"].startswith("card_number: card 1610's number is now 4449"))
        self.assertEqual(self.card("1610")["card_number"], "4449")
        self.card_save({"last4": "1610", "card_number": "5758", "expect_card_number": "4449"})
        self.assertEqual(self.card("1610")["card_number"], "5758")
        self.card_save({"last4": "1610", "card_number": "4449"})                  # without it: as before
        self.assertEqual(self.card("1610")["card_number"], "4449")

    def test_setting_a_default_counts_the_purchases_it_could_fill(self):
        ids = self.ids()
        for key in ("anth", "amzn"):
            self.store.update_transaction(ids[key], {"usage": "Marketing"})
        r = self.card_save({"last4": "1610", "default_cost_center": "Salon - STC"})
        self.assertEqual(r["fillable"], 2)
        self.assertNotIn("fillable", self.card_save({"last4": "1610", "default_cost_center": "Salon - STC"}))
        self.assertNotIn("fillable", self.card_save({"last4": "1610", "label": "Visa"}))


class CardSettingsTests(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload().status_code, 200)

    def test_default_cost_center(self):
        r = self.card_save({"last4": "1610", "default_cost_center": "salon - dufferin"})
        self.assertEqual(r["card"]["default_cost_center"], "Salon - Dufferin")
        self.assertEqual(self.card("1610")["default_cost_center"], "Salon - Dufferin")
        for bad in ("Nowhere", "Marketing"):              # unknown, and retired by the coding split
            r = self.card_save({"last4": "1610", "default_cost_center": bad}, expect=400)
            self.assertEqual(r["error"], "default_cost_center: not an active cost centre")
        self.assertEqual(self.card("1610")["default_cost_center"], "Salon - Dufferin")
        self.card_save({"last4": "1610", "default_cost_center": ""})
        self.assertIsNone(self.card("1610")["default_cost_center"])
        self.card_save({"last4": "1610", "default_cost_center": None})
        self.assertIsNone(self.card("1610")["default_cost_center"])

    def test_receipt_upload_switch(self):
        for value in (True, False, None):
            r = self.card_save({"last4": "4894", "receipt_upload": value})
            self.assertEqual(r["card"]["receipt_upload_setting"], value)
        self.assertEqual(self.card_save({"last4": "4894", "receipt_upload": "yes"}, expect=400)["error"],
                         "receipt_upload: true, false or null")
        cards = {c["last4"]: c for c in self.get("/api/cards")["cards"]}
        self.assertEqual((cards["4894"]["default_cost_center"], cards["4894"]["receipt_upload_setting"]), ("", None))

    def test_the_cards_own_switch_wins_over_the_env(self):
        self.store.upsert_cards([{"last4": "1610", "receipt_upload": False}, {"last4": "4894", "receipt_upload": True}])
        with mock.patch.object(main, "RECEIPT_UPLOAD_CARDS", {"all"}):
            self.assertFalse(main.receipt_upload_enabled("1610"))
            self.assertFalse(main.receipt_upload_enabled({"last4": "1610", "receipt_upload": False}))
            self.assertTrue(main.receipt_upload_enabled("2301"))                  # null follows the env
            self.assertTrue(main.receipt_upload_env("1610"))
        with mock.patch.object(main, "RECEIPT_UPLOAD_CARDS", {"4421"}):
            self.assertTrue(main.receipt_upload_enabled("4894"))
            self.assertFalse(main.receipt_upload_enabled("2301"))
            self.assertTrue(main.receipt_upload_enabled("4421"))                  # no row: the env
            cards = {c["last4"]: c["receipt_upload"] for c in self.get("/api/cards")["cards"]}
            self.assertEqual(cards, {"1610": False, "2301": False, "4894": True})

    def test_reminders_and_receipt_access_follow_the_row(self):
        with mock.patch.object(main, "RECEIPT_UPLOAD_CARDS", {"4421"}):
            with self.assertRaises(main.AuthError):
                main.receipt_access(AP_IAP, {"card_last4": "4894"})
            self.card_save({"last4": "4894", "receipt_upload": True})
            main.receipt_access(AP_IAP, {"card_last4": "4894"})                    # no longer refused
            with mock.patch.object(chat, "post", side_effect=AssertionError("dry run must not post")):
                r = self.call("POST", "/api/notify?year=2026&month=9&dry_run=1")
            payload = {c["card"]: c for c in r["cards"]}["4894"]["payload"]
            buttons = payload["cardsV2"][0]["card"]["sections"][-1]["widgets"][1]["buttonList"]["buttons"]
            self.assertEqual([b["text"] for b in buttons], ["Add receipts", "Open dashboard"])
        with mock.patch.object(main, "RECEIPT_UPLOAD_CARDS", {"all"}):
            self.card_save({"last4": "1610", "receipt_upload": False})
            with self.assertRaises(main.AuthError):
                main.receipt_access(AP_IAP, {"card_last4": "1610"})


# ---------------------------------------------------------------------------
# Cards: the card as issued (bank, network, product), drawn by the Cards gallery
# ---------------------------------------------------------------------------
class CardFaceTests(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload().status_code, 200)

    def face(self, last4):
        c = self.card(last4)
        return c.get("bank"), c.get("network"), c.get("product")

    def test_round_trip_cleaned_and_cleared(self):
        r = self.card_save({"last4": "1610", "bank": " RBC ", "network": "Visa", "product": "  Avion \t Business "})
        self.assertEqual((r["card"]["bank"], r["card"]["network"], r["card"]["product"]), ("rbc", "visa", "Avion Business"))
        self.assertEqual(self.face("1610"), ("rbc", "visa", "Avion Business"))
        listed = {c["last4"]: c for c in self.get("/api/cards")["cards"]}
        self.assertEqual((listed["1610"]["bank"], listed["1610"]["network"], listed["1610"]["product"]),
                         ("rbc", "visa", "Avion Business"))
        self.assertEqual((listed["2301"]["bank"], listed["2301"]["network"], listed["2301"]["product"]), ("", "", ""))
        admin_cards = {c["last4"]: c for c in self.get("/api/admin/cards")["cards"]}
        self.assertEqual(admin_cards["1610"]["bank"], "rbc")
        self.assertEqual(admin_cards["1610"]["product"], "Avion Business")
        # Only what is sent changes.
        self.card_save({"last4": "1610", "product": "U.S. Dollar"})
        self.assertEqual(self.face("1610"), ("rbc", "visa", "U.S. Dollar"))
        self.card_save({"last4": "1610", "bank": "", "network": None, "product": "   "})
        self.assertEqual(self.face("1610"), (None, None, None))
        self.card_save({"last4": "1610", "product": "x" * 80})
        self.assertEqual(self.face("1610")[2], "x" * 60)
        self.card_save({"last4": "1610", "product": "a" * 59 + " b"})        # cut at 60, no trailing space
        self.assertEqual(self.face("1610")[2], "a" * 59)

    def test_refused_values_change_nothing(self):
        self.card_save({"last4": "1610", "bank": "chase", "network": "mastercard", "product": "Ink"})
        for body, error in (
                ({"bank": "td"}, "bank: 'td' is not one of rbc, chase"),
                ({"bank": 5}, "bank: rbc, chase, or empty"),
                ({"bank": ["rbc"]}, "bank: rbc, chase, or empty"),
                ({"network": "discover"}, "network: 'discover' is not one of visa, mastercard, amex"),
                ({"network": True}, "network: visa, mastercard, amex, or empty"),
                ({"product": {"name": "Avion"}}, "product: text, at most 60 characters"),
                ({"product": 7}, "product: text, at most 60 characters")):
            r = self.card_save(dict(body, last4="1610", label="Changed"), expect=400)
            self.assertEqual(r["error"], error)
        self.assertEqual(self.face("1610"), ("chase", "mastercard", "Ink"))
        self.assertEqual(self.card("1610")["label"], "Card ending 1610 - Yin Wei")

    def test_create_takes_them_too(self):
        r = self.card_save({"last4": "8100", "create": True, "holder_name": "George", "bank": "rbc", "network": "visa",
                            "product": "U.S. Dollar"})
        self.assertEqual((r["card"]["bank"], r["card"]["network"], r["card"]["product"]), ("rbc", "visa", "U.S. Dollar"))
        self.assertEqual(self.audits(entity="card", entity_id="8100")[0]["after"]["product"], "U.S. Dollar")
        self.assertEqual(self.card_save({"last4": "8101", "create": True, "network": "diners"}, expect=400)["error"],
                         "network: 'diners' is not one of visa, mastercard, amex")
        self.assertNotIn("8101", {c["last4"] for c in self.store.list_cards()})

    def test_changes_are_audited_once(self):
        self.card_save({"last4": "4894", "bank": "chase", "network": "visa"})
        rows = self.audits(entity="card", entity_id="4894")
        self.assertEqual([a["action"] for a in rows], ["update"])
        self.assertEqual((rows[0]["before"]["bank"], rows[0]["after"]["bank"]), (None, "chase"))
        self.assertEqual((rows[0]["before"]["network"], rows[0]["after"]["network"]), (None, "visa"))
        self.assertIsNone(rows[0]["after"]["product"])
        self.card_save({"last4": "4894", "bank": "CHASE", "network": "visa"})               # the same, as stored
        self.assertEqual(len(self.audits(entity="card", entity_id="4894")), 1)
        self.card_save({"last4": "4894", "product": "Ink Business"})
        rows = self.audits(entity="card", entity_id="4894")
        self.assertEqual((rows[0]["before"]["product"], rows[0]["after"]["product"]), (None, "Ink Business"))

    def test_the_bank_registry(self):
        r = self.get("/api/admin/cards")
        self.assertEqual([b["key"] for b in r["banks"]], list(main.BANKS))
        self.assertEqual([b["key"] for b in r["banks"]], ["rbc", "chase"])
        self.assertEqual(r["networks"], ["visa", "mastercard", "amex"])
        for b in r["banks"]:
            self.assertEqual(set(b), {"key", "name", "short", "logo", "logo_on_dark"})
            self.assertTrue(b["short"] and len(b["short"]) <= 12, b)
            for url in (b["logo"], b["logo_on_dark"]):
                # The page draws only logos under /static/banks/, served same-origin and cacheable.
                self.assertTrue(url.startswith("/static/banks/") and url.endswith(".svg"), url)
                self.assertTrue(os.path.isfile(os.path.join(STATIC, url[len("/static/"):])), url)
                with self.client.get(url) as resp:
                    self.assertEqual(resp.status_code, 200, url)
                    self.assertEqual(resp.mimetype, "image/svg+xml")
                    self.assertEqual(resp.headers["Cache-Control"], "private, max-age=86400")
                    self.assertIn("default-src 'none'", resp.headers["Content-Security-Policy"])
                    svg = resp.get_data(as_text=True)
                for active in ("<script", "href", "<foreignObject", " on"):
                    self.assertNotIn(active, svg.replace(" one", ""), url)
        with self.client.get("/admin") as resp:
            self.assertEqual(resp.headers["Cache-Control"], "no-cache, no-store, must-revalidate")
        with self.client.get("/static/banks/none.svg") as resp:
            self.assertEqual(resp.status_code, 404)

    def test_the_store_refuses_what_the_check_constraints_refuse(self):
        for row in ({"last4": "1610", "network": "discover"}, {"last4": "1610", "product": "x" * 61}):
            with self.assertRaises(StoreError) as ctx:
                self.store.upsert_cards([row])
            self.assertEqual(ctx.exception.code, "23514")
            self.assertFalse(ctx.exception.missing)
        self.assertEqual(self.face("1610"), (None, None, None))
        self.store.upsert_cards([{"last4": "1610", "network": "amex", "product": "x" * 60}])
        self.assertEqual(self.face("1610"), (None, "amex", "x" * 60))


# ---------------------------------------------------------------------------
# Cards: delete, fill the default
# ---------------------------------------------------------------------------
class DeleteCardTests(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload().status_code, 200)

    def test_a_used_card_or_a_primary_is_kept(self):
        r = self.call("DELETE", "/api/admin/cards/1610", expect=409)
        self.assertEqual(r["counts"]["transactions"], 5)
        self.assertIn("only an unused card can be deleted", r["error"])
        self.card_save({"last4": "5555", "create": True})
        self.card_save({"last4": "6666", "create": True, "primary_last4": "5555"})
        r = self.call("DELETE", "/api/admin/cards/5555", expect=409)
        self.assertEqual(r["counts"], {"transactions": 0, "statements": 0, "supplementary": 1})
        self.assertIn("5555", {c["last4"] for c in self.store.list_cards()})

    def test_an_unused_card_is_deleted_and_audited(self):
        self.card_save({"last4": "1234", "create": True})
        r = self.call("DELETE", "/api/admin/cards/1234")
        self.assertEqual(r["deleted"], "1234")
        self.assertNotIn("1234", {c["last4"] for c in r["cards"]})
        self.assertNotIn("1234", {c["last4"] for c in self.get("/api/cards")["cards"]})
        rows = self.audits(entity="card", entity_id="1234")
        self.assertEqual([a["action"] for a in rows], ["delete", "create"])
        self.assertEqual(rows[0]["before"]["last4"], "1234")
        self.call("DELETE", "/api/admin/cards/1234", expect=404)
        self.call("DELETE", "/api/admin/cards/12x4", expect=400)

    def test_a_delete_that_moves_receipt_numbers_asks_first(self):
        self.store.upsert_cards([{"last4": "3914", "label": "Card ending 3914"}, {"last4": "3922", "label": "Card ending 3922"}])
        r = self.call("DELETE", "/api/admin/cards/3914", expect=409)
        self.assertEqual({c["number"] for c in r["alias_changes"]}, {"3906", "3914", "3930"})
        self.assertIn("they would count as card 3922's", r["error"])
        self.assertIn("3914", {c["last4"] for c in self.store.list_cards()})
        self.call("DELETE", "/api/admin/cards/3914?confirm=1")
        self.assertNotIn("3914", {c["last4"] for c in self.store.list_cards()})
        self.assertIn("alias changes confirmed", self.audits(entity="card", entity_id="3914")[0]["note"])

    def test_not_while_a_job_holds_the_lease(self):
        self.card_save({"last4": "1234", "create": True})
        with mock.patch.object(main, "take_lease", return_value=None):
            r = self.call("DELETE", "/api/admin/cards/1234", expect=409)
        self.assertEqual(r["error"], "an import or match is running; try again in a minute")
        self.assertIn("1234", {c["last4"] for c in self.store.list_cards()})
        with main._job_lock:                        # a job of this instance
            self.call("DELETE", "/api/admin/cards/1234", expect=409)
        self.call("DELETE", "/api/admin/cards/1234")

    def test_a_card_listed_on_an_upload_is_not_deletable(self):
        self.card_save({"last4": "1234", "create": True})
        self.store.insert_statement({"file_name": "Other Export.xlsx", "cards": ["1234"], "months": ["2026 09"],
                                     "uploaded_at": "2026-10-01T00:00:00Z"})
        cards = {c["last4"]: c for c in self.get("/api/admin/cards")["cards"]}
        self.assertFalse(cards["1234"]["deletable"])
        self.assertEqual(cards["1234"]["delete_blockers"], ["listed on upload Other Export.xlsx"])
        self.assertEqual(cards["1610"]["delete_blockers"], ["5 transactions", "listed on upload Transaction Export.xlsx"])
        r = self.call("DELETE", "/api/admin/cards/1234", expect=409)
        self.assertEqual(r["counts"]["statements"], 1)


class FillDefaultTests(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload().status_code, 200)
        self.ids_ = self.ids()
        self.extra = txn("t_extra", "2026-09-21", "Staples", 12.0, card="1610")
        self.store.insert_transactions([self.extra])
        self.store.update_transaction(self.ids_["anth"], {"usage": "Marketing"})                      # coded, no cost centre
        self.store.update_transaction(self.ids_["amzn"], {"usage": "Travel", "cost_center": "EU"})   # coded, has one
        # t_extra: no use case

    def test_fill_into_coded_purchases_without_a_cost_centre(self):
        self.card_save({"last4": "1610", "default_cost_center": "Salon - Dufferin"})
        r = self.call("POST", "/api/admin/cards/1610/fill-default")
        self.assertEqual(r, {"filled": 1, "cost_center": "Salon - Dufferin"})
        anth = self.store.get_transaction(self.ids_["anth"])
        self.assertEqual((anth["cost_center"], anth["updated_by"]), ("Salon - Dufferin", "manne"))
        self.assertEqual(self.store.get_transaction(self.ids_["amzn"])["cost_center"], "EU")
        self.assertIsNone(self.store.get_transaction("t_extra")["cost_center"])
        rows = self.audits(entity="card", entity_id="1610")
        self.assertEqual(rows[0]["action"], "fill_default")
        self.assertEqual(rows[0]["after"], {"cost_center": "Salon - Dufferin", "transactions": [self.ids_["anth"]]})
        self.assertEqual(self.call("POST", "/api/admin/cards/1610/fill-default")["filled"], 0)

    def test_no_active_default(self):
        r = self.call("POST", "/api/admin/cards/1610/fill-default", expect=400)
        self.assertEqual(r["error"], "default_cost_center: the card has no active default")
        self.card_save({"last4": "1610", "default_cost_center": "Salon - Dufferin"})
        self.call("PATCH", "/api/admin/cost-centers?name=Salon - Dufferin", {"active": False})
        self.call("POST", "/api/admin/cards/1610/fill-default", expect=400)
        self.assertIsNone(self.store.get_transaction(self.ids_["anth"])["cost_center"])
        self.call("POST", "/api/admin/cards/9999/fill-default", expect=404)


# ---------------------------------------------------------------------------
# Use cases and cost centres
# ---------------------------------------------------------------------------
class CodingAdminTests(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload().status_code, 200)
        self.ids_ = self.ids()

    def test_the_lists_after_the_split(self):
        r = self.get("/api/admin/coding")
        self.assertEqual(len(r["cost_centers"]), 21)
        self.assertEqual([c["name"] for c in r["cost_centers"] if c["active"]][:2], ["Head office", "Salon - Dufferin"])
        self.assertEqual(sum(1 for c in r["cost_centers"] if c["active"]), 10)
        self.assertEqual(len(r["use_cases"]), 11)
        self.assertEqual(r["use_cases"][-1]["name"], "Other")
        self.assertTrue(all(u["uses"] == 0 for u in r["use_cases"]))
        self.assertEqual(r["schema_missing"], [])
        self.patch(self.ids_["anth"], {"usage": "Ticktick"})
        self.patch(self.ids_["amzn"], {"usage": "travel"})
        self.patch(self.ids_["retell"], {"cost_center": "Marketing"})
        r = self.get("/api/admin/coding")
        self.assertEqual(r["usage_not_listed"], [{"value": "Ticktick", "uses": 1}])
        self.assertEqual({u["name"]: u["uses"] for u in r["use_cases"]}["Travel"], 1)
        self.assertEqual(r["cost_centers_like_use_cases"], [{"value": "Marketing", "uses": 1, "use_case": "Marketing"}])

    def test_cost_centres(self):
        r = self.call("POST", "/api/admin/cost-centers", {"name": "  Warehouse ", "description": "Brampton stock"}, expect=201)
        added = r["cost_center"]
        self.assertEqual((added["name"], added["active"], added["sort"]), ("Warehouse", True, 1009))
        r = self.call("POST", "/api/admin/cost-centers", {"name": "warehouse"}, expect=409)
        self.assertEqual(r["existing"], {"name": "Warehouse", "active": True})
        r = self.call("POST", "/api/admin/cost-centers", {"name": "marketing"}, expect=409)      # the retired type
        self.assertEqual(r["existing"], {"name": "Marketing", "active": False})
        self.call("POST", "/api/admin/cost-centers", {"name": ""}, expect=400)
        self.call("POST", "/api/admin/cost-centers", {"name": "x", "sort": "ten"}, expect=400)
        self.call("PATCH", "/api/admin/cost-centers?name=Nowhere", {"active": False}, expect=404)

        # Renamed while purchases carry it: refused, they would keep the old name.
        self.patch(self.ids_["anth"], {"cost_center": "Salon - Dufferin"})
        r = self.call("PATCH", "/api/admin/cost-centers?name=Salon - Dufferin", {"name": "Dufferin"}, expect=409)
        self.assertEqual(r["uses"], 1)
        self.assertIn("keep Salon - Dufferin", r["error"])
        self.call("PATCH", "/api/admin/cost-centers?name=Salon - Dufferin", {"name": "salon - DUFFERIN"})   # capitals only
        self.call("PATCH", "/api/admin/cost-centers?name=salon - DUFFERIN", {"name": "Salon - Dufferin"})

        # Unused: renamed, and the card defaulting to it follows.
        self.card_save({"last4": "2301", "default_cost_center": "Salon - Ridgeway"})
        r = self.call("PATCH", "/api/admin/cost-centers?name=Salon - Ridgeway", {"name": "Ridgeway"})
        self.assertEqual(r["cost_center"]["name"], "Ridgeway")
        self.assertEqual(self.card("2301")["default_cost_center"], "Ridgeway")
        self.call("PATCH", "/api/admin/cost-centers?name=Ridgeway", {"name": "us"}, expect=409)

        # Retired: gone from the dashboard's list, kept in the admin list, and the card is named.
        r = self.call("PATCH", "/api/admin/cost-centers?name=Ridgeway", {"active": False})
        self.assertEqual(r["warnings"], ["Card 2301 (Card ending 2301 - Sam Lee) defaults to Ridgeway: its purchases are "
                                         "no longer filled until it gets another default."])
        self.assertNotIn("Ridgeway", self.get("/api/months")["cost_centers"])
        self.assertFalse({c["name"]: c for c in self.get("/api/admin/coding")["cost_centers"]}["Ridgeway"]["active"])
        coding = self.get("/api/admin/coding")
        self.assertEqual({c["name"]: c["card_defaults"] for c in coding["cost_centers"]}["Ridgeway"], ["2301"])
        self.call("PATCH", "/api/admin/cost-centers?name=Ridgeway", {"active": True})
        self.assertIn("Ridgeway", self.get("/api/months")["cost_centers"])

        # Order: every name, retired ones too.
        names = [c["name"] for c in self.store.list_cost_center_rows()]
        r = self.call("POST", "/api/admin/cost-centers/order", {"names": list(reversed(names))})
        self.assertEqual([c["name"] for c in r["cost_centers"]], list(reversed(names)))
        self.assertEqual([c["sort"] for c in r["cost_centers"]][:3], [10, 20, 30])
        self.assertEqual(self.get("/api/months")["cost_centers"][0], "Warehouse")
        self.call("POST", "/api/admin/cost-centers/order", {"names": ["Nowhere"]}, expect=400)

        actions = [(a["entity_id"], a["action"]) for a in reversed(self.audits(entity="cost_center"))]
        self.assertEqual(actions, [("Warehouse", "create"), ("salon - DUFFERIN", "rename"), ("Salon - Dufferin", "rename"),
                                   ("Ridgeway", "rename"), ("Ridgeway", "retire"), ("Ridgeway", "restore"), ("*", "reorder")])

    def test_use_cases(self):
        self.card_save({"last4": "1610", "group_name": "Hair"})
        r = self.call("POST", "/api/admin/use-cases", {"name": "Hair samples", "group_name": "hair"}, expect=201)
        samples = r["use_case"]
        self.assertEqual((samples["group_name"], samples["active"], samples["created_by"]), ("Hair", True, "manne"))
        r = self.call("POST", "/api/admin/use-cases", {"name": "Nail art", "group_name": "Nails"}, expect=400)
        self.assertEqual(r["error"], "group_name: no card is in a group named 'Nails'")
        r = self.call("POST", "/api/admin/use-cases", {"name": "hair SAMPLES"}, expect=409)
        self.assertEqual(r["existing"], {"id": samples["id"], "name": "Hair samples", "active": True})
        self.call("POST", "/api/admin/use-cases", {"name": "x" * 81}, expect=400)
        names = [u["name"] for u in self.get("/api/admin/coding")["use_cases"]]
        self.assertEqual(names[-2:], ["Hair samples", "Other"])             # before Other without a sort

        # Rename: refused while purchases carry it (they keep it); capitals only is fine.
        travel = self.use_case("Travel")
        self.patch(self.ids_["anth"], {"usage": "Travel"})
        r = self.call("PATCH", f"/api/admin/use-cases/{travel['id']}", {"name": "Trips"}, expect=409)
        self.assertEqual(r["uses"], 1)
        self.assertIn("Purchases already coded Travel keep Travel", r["error"])
        self.call("PATCH", f"/api/admin/use-cases/{travel['id']}", {"name": "TRAVEL"})
        self.call("PATCH", f"/api/admin/use-cases/{travel['id']}", {"name": "Travel", "description": "flights, hotels"})
        self.call("PATCH", f"/api/admin/use-cases/{samples['id']}", {"name": "Other"}, expect=409)
        self.call("PATCH", f"/api/admin/use-cases/{UUID_A}", {"name": "x"}, expect=404)
        self.call("PATCH", "/api/admin/use-cases/not-a-uuid", {"name": "x"}, expect=404)

        # Retire and restore.
        r = self.call("PATCH", f"/api/admin/use-cases/{samples['id']}", {"active": False})
        self.assertFalse(r["use_case"]["active"])
        self.assertNotIn("Hair samples", [u["name"] for u in self.get("/api/months")["use_cases"]])
        self.call("PATCH", f"/api/admin/use-cases/{samples['id']}", {"active": True})
        self.assertIn("Hair samples", [u["name"] for u in self.get("/api/months")["use_cases"]])

        # Order.
        ids = [u["id"] for u in self.store.list_use_cases()]
        r = self.call("POST", "/api/admin/use-cases/order", {"ids": list(reversed(ids))})
        self.assertEqual([u["id"] for u in r["use_cases"]], list(reversed(ids)))
        self.call("POST", "/api/admin/use-cases/order", {"ids": [UUID_A]}, expect=400)

        actions = [a["action"] for a in reversed(self.audits(entity="use_case"))]
        self.assertEqual(actions, ["create", "rename", "rename", "retire", "restore", "reorder"])

    def test_schema_not_applied(self):
        status = dict(self.store.schema_status(), **{"cost_centers.active": False})
        with mock.patch.object(self.store, "schema_status", return_value=status):
            self.assertEqual(self.get("/api/admin/coding")["schema_missing"], ["cost_centers.active"])
        with mock.patch.object(self.store, "list_use_cases", side_effect=MISSING_TABLE):
            r = self.get("/api/admin/coding")
        self.assertEqual((r["use_cases"], r["schema_missing"]), ([], ["use_cases"]))


class StoreErrorAnswerTests(AdminApiTestCase):
    def test_a_missing_table_and_a_duplicate(self):
        with mock.patch.object(self.store, "list_cost_center_rows", side_effect=MISSING_TABLE), mock.patch("builtins.print"):
            r = self.get("/api/admin/coding", expect=502)
        self.assertTrue(r["schema_missing"])
        self.assertIn("apply schema.sql with setup_db.py", r["error"])
        dup = StoreError('POST cost_centers -> 409 {"code":"23505"}', status=409, code="23505")
        with mock.patch.object(self.store, "list_cost_center_rows", side_effect=dup), mock.patch("builtins.print"):
            r = self.get("/api/admin/coding", expect=409)
        self.assertEqual(r["error"], "that already exists (someone may have just added it); reload")
        with mock.patch.object(self.store, "list_cost_center_rows", side_effect=StoreError("boom", status=500)), \
                mock.patch("builtins.print"):
            self.assertEqual(self.get("/api/admin/coding", expect=502)["error"], "database error: boom")


# ---------------------------------------------------------------------------
# The dashboard's coding: PATCH /api/transactions/<id>, uncoded, the lists it reads
# ---------------------------------------------------------------------------
class FillRuleTests(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload().status_code, 200)
        self.ids_ = self.ids()
        self.store.insert_transactions([txn("t_extra", "2026-09-21", "Staples", 12.0, card="1610")])
        self.card_save({"last4": "1610", "default_cost_center": "Salon - Dufferin"})

    def test_the_card_default_fills_an_empty_cost_centre(self):
        r = self.patch(self.ids_["anth"], {"usage": "marketing"})
        self.assertEqual((r["transaction"]["usage"], r["transaction"]["cost_center"]), ("Marketing", "Salon - Dufferin"))
        self.assertEqual(r["filled_cost_center"], "Salon - Dufferin")
        self.assertNotIn("hint", r)

    def test_when_it_does_not_fill(self):
        r = self.patch(self.ids_["anth"], {"usage": "Travel", "cost_center": "US"})          # the body says one
        self.assertEqual((r["transaction"]["cost_center"], "filled_cost_center" in r), ("US", False))
        self.store.update_transaction(self.ids_["amzn"], {"cost_center": "EU"})
        r = self.patch(self.ids_["amzn"], {"usage": "Travel"})                               # the row has one
        self.assertEqual((r["transaction"]["cost_center"], "filled_cost_center" in r), ("EU", False))
        r = self.patch(self.ids_["fee"], {"usage": "Other"})                                 # not a purchase
        self.assertEqual((r["transaction"]["cost_center"], "filled_cost_center" in r), (None, False))
        self.call("PATCH", "/api/admin/cost-centers?name=Salon - Dufferin", {"active": False})
        r = self.patch("t_extra", {"usage": "Office & Supplies"})                            # a retired default
        self.assertEqual((r["transaction"]["cost_center"], "filled_cost_center" in r), (None, False))

    def test_free_text_and_a_missing_use_case_table(self):
        r = self.patch(self.ids_["anth"], {"usage": "Ticktick"})
        self.assertEqual((r["transaction"]["usage"], r["filled_cost_center"]), ("Ticktick", "Salon - Dufferin"))
        with mock.patch.object(self.store, "list_use_cases", side_effect=MISSING_TABLE), mock.patch("builtins.print"):
            r = self.patch(self.ids_["amzn"], {"usage": "marketing"})
        self.assertEqual(r["transaction"]["usage"], "marketing")                 # stored as typed

    def test_a_cost_centre_spelled_another_way_is_snapped(self):
        r = self.patch(self.ids_["retell"], {"cost_center": "salon - ridgeway"})
        self.assertEqual(r["transaction"]["cost_center"], "Salon - Ridgeway")

    def test_a_use_case_typed_as_cost_centre_gets_a_hint(self):
        r = self.patch(self.ids_["retell"], {"cost_center": "Marketing"})
        self.assertEqual(r["transaction"]["cost_center"], "Marketing")           # nothing is moved
        self.assertEqual(r["hint"], "Marketing is a use case now: put it in the Use case box")
        self.patch(self.ids_["costco"], {"usage": "Travel"})
        self.assertNotIn("hint", self.patch(self.ids_["costco"], {"cost_center": "Marketing"}))

    def test_a_cost_centre_set_meanwhile_is_kept(self):
        # The Cost centre box's save lands between this request's read and its write: the use case is
        # still saved, the cost centre set meanwhile stays, and nothing claims a fill.
        stale = self.store.get_transaction(self.ids_["anth"])
        self.store.update_transaction(self.ids_["anth"], {"cost_center": "EU"})
        with mock.patch.object(self.store, "get_transaction", return_value=stale):
            r = self.patch(self.ids_["anth"], {"usage": "Travel"})
        self.assertEqual((r["transaction"]["usage"], r["transaction"]["cost_center"]), ("Travel", "EU"))
        self.assertNotIn("filled_cost_center", r)
        self.assertEqual(self.store.get_transaction(self.ids_["anth"])["cost_center"], "EU")

    def test_the_retry_without_the_fill_keeps_the_requests_own_conditions(self):
        # Confirm reads a proposal; if a match run moved it meanwhile the whole request still answers 409.
        self.store.update_transaction(self.ids_["anth"], {"invoice_status": "possible", "invoice_id": "inv-x"})
        stale = self.store.get_transaction(self.ids_["anth"])
        self.store.update_transaction(self.ids_["anth"], {"cost_center": "EU", "invoice_status": "missing", "invoice_id": None})
        with mock.patch.object(self.store, "get_transaction", return_value=stale), \
                mock.patch.object(self.store, "get_invoice", return_value={"id": "inv-x"}):
            self.patch(self.ids_["anth"], {"usage": "Travel", "action": "confirm"}, expect=409)
        self.assertIsNone(self.store.get_transaction(self.ids_["anth"])["usage"])


class UncodedSemanticsTests(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload().status_code, 200)
        self.ids_ = self.ids()

    def test_uncoded_means_no_use_case(self):
        self.assertEqual(self.months_sept()["uncoded"], 6)
        self.patch(self.ids_["retell"], {"cost_center": "US"})
        self.assertEqual(self.months_sept()["uncoded"], 6)                      # a cost centre alone codes nothing
        self.patch(self.ids_["retell"], {"usage": "Software"})
        self.assertEqual(self.months_sept()["uncoded"], 5)
        self.assertEqual(self.months_sept()["cards"]["4894"]["uncoded"], 2)
        self.assertEqual(assistant.build_context(self.store, 2026, 9)["totals"]["no_use_case"], 5)

    def test_reminders_count_purchases_without_a_use_case_and_stay_about_receipts(self):
        self.patch(self.ids_["retell"], {"usage": "Software", "cost_center": "US"})
        tims = [t["id"] for t in self.transactions(card="4894")["transactions"] if t["description"] == "Tim Hortons #123"]
        self.patch(tims[0], {"cost_center": "US"})
        self.patch(self.ids_["costco"], {"action": "waive"})
        with mock.patch.object(chat, "post", side_effect=AssertionError("dry run must not post")):
            r = self.call("POST", "/api/notify?year=2026&month=9&dry_run=1")
        results = {c["card"]: c for c in r["cards"]}
        head = results["4894"]["payload"]["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
        self.assertIn("<b>Without use case:</b> 2", head)
        self.assertNotIn("cost centre", head)
        # Every receipt of 2301 is in (waived) though Costco has no use case: still no card.
        self.assertEqual(results["2301"], {"card": "2301", "sent": False, "detail": "nothing missing"})

    def test_export_header_and_chat_footers(self):
        from openpyxl import load_workbook
        r = self.client.get("/api/export/missing?year=2026&month=9")
        self.assertEqual(r.status_code, 200)
        head = [c.value for c in load_workbook(io.BytesIO(r.data)).active[1]]
        self.assertIn("Use case", head)
        self.assertNotIn("Usage", head)
        missing = self.store.list_transactions(2026, 9, "1610", statuses=["missing"])
        for kwargs in ({"receipts_url": "https://ap.test/receipts/1610/2026/09"}, {}):
            payload = chat.missing_invoices_card({"last4": "1610"}, 2026, 9, missing, [], "https://ap.test",
                                                 "https://drive.google.com/drive/folders/abc", **kwargs)
            footer = payload["cardsV2"][0]["card"]["sections"][-1]["widgets"][0]["textParagraph"]["text"]
            self.assertIn("say what each purchase was for (its use case)", footer)
            self.assertNotIn("cost centre and usage", footer)


class PayloadTests(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload().status_code, 200)

    def test_use_cases_on_the_dashboards_reads(self):
        self.store.update_use_case(self.use_case("Travel")["id"], {"active": False})
        for page in (self.get("/api/months"), self.transactions()):
            names = [u["name"] for u in page["use_cases"]]
            self.assertEqual((len(names), names[-1]), (10, "Other"))
            self.assertNotIn("Travel", names)
            self.assertEqual(set(page["use_cases"][0]), {"id", "name", "group_name", "description"})
        page = self.transactions()
        self.assertIs(page["suggestions"], False)
        self.assertFalse(any("suggestion" in t for t in page["transactions"]))

    def test_a_missing_use_case_table_leaves_the_list_empty(self):
        with mock.patch.object(self.store, "list_use_cases", side_effect=MISSING_TABLE), mock.patch("builtins.print"):
            self.assertEqual(self.get("/api/months")["use_cases"], [])
            self.assertEqual(self.transactions()["use_cases"], [])

    def test_suggestions_read_each_list_once(self):
        vendor = self.call("POST", "/api/admin/vendors", {"name": "Anthropic", "patterns": [{"pattern": "anthropic claude"}]},
                           expect=201)["vendor"]
        self.call("PUT", f"/api/admin/vendors/{vendor['id']}/coding",
                  {"group_name": "", "use_case_id": self.use_case("IT & Software")["id"]})
        self.call("PUT", "/api/admin/settings/coding", {"suggestions": True})
        with mock.patch.object(self.store, "list_cost_centers", wraps=self.store.list_cost_centers) as cost_centers, \
                mock.patch.object(self.store, "list_use_cases", wraps=self.store.list_use_cases) as use_cases:
            page = self.transactions()
        self.assertLessEqual((cost_centers.call_count, use_cases.call_count), (1, 1))
        self.assertEqual((cost_centers.call_count, use_cases.call_count), (1, 1))
        self.assertTrue(page["suggestions"])
        anth = next(t for t in page["transactions"] if t["description"] == "Anthropic* Claude Team")
        self.assertEqual(anth["suggestion"]["usage"], "IT & Software")


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------
class VendorAdminTests(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.store.upsert_cards([{"last4": "1610", "label": "Card ending 1610", "owner_email": MEMBER["email"]},
                                 {"last4": "4894", "label": "Card ending 4894", "group_name": "Hair"}])
        self.store.upsert_invoices([
            inv("p1", "ParkWhiz Inc", 20.0, "2026-09-02", vendor_folder="Parkwhiz,"),
            inv("p2", "ParkWhiz Inc", 21.0, "2026-09-03", vendor_folder="Parkwhiz,"),
            inv("c1", "Canada Post", 11.0, "2026-09-04", vendor_folder="Canada Post"),
            inv("c2", "Canada Post", 12.0, "2026-09-05", vendor_folder="canada post"),
            inv("c3", "Canada Post", 13.0, "2026-09-06", vendor_folder="Canada Post"),
            inv("c4", "Canada Post Corp", 14.0, "2026-09-07", vendor_folder="Canada Post"),
            inv("e1", "Esso", 50.0, "2026-09-08", vendor_folder="Esso/Gas"),
        ])

        def linked(tid, desc, amount, invoice_id, card="4894", method="rule", **kw):
            return dict(txn(tid, "2026-09-10", desc, amount, card=card, status="matched", invoice_id=invoice_id,
                            method=method), **kw)
        self.store.insert_transactions([
            linked("t_p1", "PARKWHIZ, INC 1234", 20.0, "p1"),
            linked("t_c1", "CPC / SCP 123456", 11.0, "c1", usage="Shipping & Logistics"),
            linked("t_c2", "CPC / SCP 123457", 12.0, "c2"),
            linked("t_c3", "CPC / SCP 123458", 13.0, "c3", method="manual"),
            linked("t_e1", "ESSO 1234 TORONTO", 50.0, "e1", method="manual"),
            txn("t_miss", "2026-09-11", "CPC / SCP 999999", 9.99, card="4894"),
            txn("t_mine", "2026-09-12", "CPC / SCP 888888", 8.88, card="1610"),
        ])

    def register_canada_post(self):
        return self.call("POST", "/api/admin/vendors", {"name": "Canada Post", "folders": ["Canada Post"],
                                                        "patterns": [{"pattern": "CPC / SCP"}]}, expect=201)["vendor"]

    def test_catalogue(self):
        r = self.get("/api/admin/vendors?per=2&sort=name")
        self.assertEqual((r["total"], r["pages"], r["page"], r["per"]), (3, 2, 1, 2))
        self.assertEqual([row["folder"] for row in r["rows"]], ["Canada Post", "Esso/Gas"])
        self.assertEqual([row["folder"] for row in self.get("/api/admin/vendors?per=2&sort=name&page=2")["rows"]], ["Parkwhiz,"])
        rows = {row["folder"]: row for row in self.get("/api/admin/vendors?sort=volume")["rows"]}
        post = rows["Canada Post"]
        self.assertEqual((post["docs"], post["matched"], post["unmatched_docs"], post["missing_by_prefix"]), (4, 3, 1, 2))
        self.assertTrue(r["generated_at"])
        self.assertTrue(r["registry_available"])
        self.assertEqual(self.get("/api/admin/vendors?q=parkwhiz")["total"], 1)
        self.assertEqual(self.get("/api/admin/vendors?registered=yes")["total"], 0)

    def test_folder_detail(self):
        r = self.get("/api/admin/vendors/folder?name=Parkwhiz%2C")
        self.assertEqual((r["folder"], r["docs"]), ("Parkwhiz,", 2))
        self.assertEqual([d["id"] for d in r["documents_unmatched"]], ["p2"])
        self.assertEqual(self.get("/api/admin/vendors/folder?name=Nowhere", expect=404)["error"],
                         "no documents in a folder named Nowhere")
        self.get("/api/admin/vendors/folder?name=", expect=400)
        r = self.get("/api/admin/vendors/folder?name=canada%20post")
        self.assertEqual([p["id"] for p in r["proposals"]], ["alias:canada post:cpc scp"])
        self.register_canada_post()
        r = self.get("/api/admin/vendors/folder?name=Canada%20Post")
        prefix = next(p for p in r["prefixes"] if p["prefix"] == "cpc scp")
        self.assertIsNone(prefix["problem"])
        self.assertEqual(prefix["owner"]["name"], "Canada Post")
        self.assertEqual(r["vendor"]["name"], "Canada Post")

    def test_register_a_vendor_and_its_clashes(self):
        vendor = self.register_canada_post()
        registry = self.get("/api/admin/registry")
        self.assertTrue(registry["available"])
        self.assertEqual(registry["vendors"], [vendor])
        self.assertEqual([f["folder"] for f in vendor["folders"]], ["Canada Post"])
        self.assertEqual([(p["pattern"], p["status"], p["source"]) for p in vendor["patterns"]],
                         [("cpc scp", "probation", "admin")])
        self.assertEqual(vendor["resolves"], {"purchases": 5, "uncoded": 4, "missing": 2})
        self.assertEqual(registry["folder_names"], ["Canada Post", "Esso/Gas", "Parkwhiz,"])
        self.assertEqual(registry["kinds"], ["supplier", "processor", "agent", "not_vendor"])

        r = self.call("POST", "/api/admin/vendors", {"name": "Postes Canada", "folders": ["canada  post"]}, expect=409)
        self.assertEqual((r["clash"], r["value"], r["existing"]),
                         ("folder", "canada post", {"id": vendor["id"], "name": "Canada Post"}))
        r = self.call("POST", "/api/admin/vendors", {"name": "canada post"}, expect=409)
        self.assertEqual((r["clash"], r["existing"]["id"]), ("name", vendor["id"]))
        r = self.call("POST", "/api/admin/vendors", {"name": "Postes", "patterns": ["CPC SCP"]}, expect=409)
        self.assertEqual((r["clash"], r["value"], r["kind"]), ("pattern", "cpc scp", "descriptor"))
        self.assertEqual(len(self.get("/api/admin/registry")["vendors"]), 1)          # nothing half saved

        r = self.call("POST", f"/api/admin/vendors/{vendor['id']}/patterns", {"pattern": "sq"}, expect=400)
        self.assertTrue(r["error"].startswith("pattern: "))
        r = self.call("POST", f"/api/admin/vendors/{vendor['id']}/patterns", {"pattern": "Canada Post Corp",
                                                                               "kind": "invoice_vendor"}, expect=201)
        pattern = next(p for p in r["vendor"]["patterns"] if p["kind"] == "invoice_vendor")
        self.assertEqual((pattern["pattern"], pattern["match_type"]), ("canada post corp", "exact"))
        self.call("PATCH", f"/api/admin/vendor-patterns/{pattern['id']}", {"status": "trusted"}, expect=400)
        r = self.call("PATCH", f"/api/admin/vendor-patterns/{pattern['id']}", {"status": "off"})
        self.assertEqual(next(p for p in r["vendor"]["patterns"] if p["id"] == pattern["id"])["status"], "off")
        r = self.call("DELETE", f"/api/admin/vendor-patterns/{pattern['id']}")
        self.assertNotIn(pattern["id"], [p["id"] for p in r["vendor"]["patterns"]])
        self.call("DELETE", f"/api/admin/vendor-patterns/{pattern['id']}", expect=404)

        r = self.call("POST", f"/api/admin/vendors/{vendor['id']}/folders", {"folders": ["Parkwhiz,", "Canada Post"]}, expect=201)
        folder = next(f for f in r["vendor"]["folders"] if f["folder"] == "Parkwhiz,")
        r = self.call("DELETE", f"/api/admin/vendor-folders/{folder['id']}")
        self.assertEqual([f["folder"] for f in r["vendor"]["folders"]], ["Canada Post"])
        self.call("DELETE", f"/api/admin/vendor-folders/{folder['id']}", expect=404)

        r = self.call("PATCH", f"/api/admin/vendors/{vendor['id']}", {"note": "the post office", "kind": "nope"}, expect=400)
        self.call("PATCH", f"/api/admin/vendors/{UUID_A}", {"note": "x"}, expect=404)
        self.call("PATCH", "/api/admin/vendors/junk", {"note": "x"}, expect=404)
        entities = {a["entity"] for a in self.audits()}
        self.assertEqual(entities, {"vendor", "vendor_pattern", "vendor_folder"})

    def test_registry_writes_count_versions(self):
        self.assertEqual(self.get("/api/admin/registry")["version"], 0)
        vendor = self.register_canada_post()
        self.call("PATCH", f"/api/admin/vendors/{vendor['id']}", {"note": "the post office"})
        self.assertEqual(self.get("/api/admin/registry")["version"], 2)
        self.assertEqual(self.store.get_state("vendor_registry")["by"], "manne")
        self.assertEqual(self.get("/api/admin/settings")["registry_version"], 2)

    def test_coding_defaults(self):
        vendor = self.register_canada_post()
        shipping = self.use_case("Shipping & Logistics")
        r = self.call("PUT", f"/api/admin/vendors/{vendor['id']}/coding", {"group_name": "", "use_case_id": shipping["id"]})
        self.assertEqual([(c["use_case"], c["group_label"]) for c in r["vendor"]["coding"]],
                         [("Shipping & Logistics", "Any group")])
        r = self.call("PUT", f"/api/admin/vendors/{vendor['id']}/coding", {"group_name": "hair", "use_case_id": shipping["id"]})
        self.assertEqual([c["group_label"] for c in r["vendor"]["coding"]], ["Any group", "Hair"])
        self.call("PUT", f"/api/admin/vendors/{vendor['id']}/coding", {"group_name": "Nails", "use_case_id": shipping["id"]},
                  expect=400)
        self.call("PUT", f"/api/admin/vendors/{vendor['id']}/coding", {"group_name": "", "use_case_id": UUID_A}, expect=400)
        r = self.call("PUT", f"/api/admin/vendors/{vendor['id']}/coding", {"group_name": "Hair", "use_case_id": None})
        self.assertEqual([c["group_label"] for c in r["vendor"]["coding"]], ["Any group"])
        self.assertEqual([a["action"] for a in reversed(self.audits(entity="vendor_coding"))], ["update", "update", "delete"])

        amazon = self.call("POST", "/api/admin/vendors", {"name": "Amazon", "code_each": True}, expect=201)["vendor"]
        r = self.call("PUT", f"/api/admin/vendors/{amazon['id']}/coding", {"group_name": "", "use_case_id": shipping["id"]},
                      expect=400)
        self.assertEqual(r["error"], "Amazon is coded purchase by purchase (code each), so it has no default")
        r = self.call("PATCH", f"/api/admin/vendors/{vendor['id']}", {"kind": "processor"})
        self.assertEqual(len(r["warnings"]), 1)
        self.call("PUT", f"/api/admin/vendors/{vendor['id']}/coding", {"group_name": "", "use_case_id": shipping["id"]},
                  expect=400)
        self.call("PATCH", f"/api/admin/vendors/{vendor['id']}", {"kind": "supplier"})
        r = self.call("PATCH", f"/api/admin/use-cases/{shipping['id']}", {"active": False})
        self.assertEqual(r["warnings"], ["Vendor Canada Post suggests it for all cards"])

    def test_a_default_that_no_longer_applies_can_still_be_removed(self):
        # Code each, another kind, or a group without cards stop a default being set, not removed:
        # the row would otherwise linger and come back when the vendor or the group changes again.
        vendor = self.register_canada_post()
        shipping = self.use_case("Shipping & Logistics")
        url = f"/api/admin/vendors/{vendor['id']}/coding"
        for change, group in (({"code_each": True}, ""), ({"kind": "processor"}, "Hair")):
            self.call("PUT", url, {"group_name": group, "use_case_id": shipping["id"]})
            r = self.call("PATCH", f"/api/admin/vendors/{vendor['id']}", change)
            self.assertIn("will be ignored", r["warnings"][0])
            self.call("PUT", url, {"group_name": group, "use_case_id": shipping["id"]}, expect=400)
            r = self.call("PUT", url, {"group_name": group.lower(), "use_case_id": None})
            self.assertEqual(r["vendor"]["coding"], [])
            self.call("PATCH", f"/api/admin/vendors/{vendor['id']}", {"code_each": False, "kind": "supplier"})
        self.call("PUT", url, {"group_name": "Hair", "use_case_id": shipping["id"]})
        self.card_save({"last4": "4894", "group_name": ""})                       # Hair has no card now
        self.call("PUT", url, {"group_name": "Hair", "use_case_id": shipping["id"]}, expect=400)
        r = self.call("PUT", url, {"group_name": "Hair", "use_case_id": None})
        self.assertEqual(r["vendor"]["coding"], [])
        self.assertEqual([a["action"] for a in self.audits(entity="vendor_coding")].count("delete"), 3)

    def test_proposals_and_dismissing_them(self):
        r = self.get("/api/admin/proposals")
        self.assertTrue(r["available"])
        alias = next(p for p in r["proposals"] if p["id"] == "alias:canada post:cpc scp")
        self.assertEqual(alias["accept"]["path"], "/api/admin/vendors")
        self.call("POST", "/api/admin/proposals/dismiss", {"id": alias["id"]})
        r = self.get("/api/admin/proposals")
        self.assertNotIn(alias["id"], [p["id"] for p in r["proposals"]])
        self.assertEqual(r["dismissed"], 1)
        self.call("POST", "/api/admin/proposals/dismiss", {"id": alias["id"], "undo": True})
        r = self.get("/api/admin/proposals")
        self.assertIn(alias["id"], [p["id"] for p in r["proposals"]])
        # Accepting it is one request; afterwards the pattern is held, so it is no longer proposed.
        self.call(alias["accept"]["method"], alias["accept"]["path"], alias["accept"]["body"], expect=201)
        self.assertNotIn(alias["id"], [p["id"] for p in self.get("/api/admin/proposals")["proposals"]])
        self.call("POST", "/api/admin/proposals/dismiss", {"id": ""}, expect=400)
        self.assertEqual([a["action"] for a in reversed(self.audits(entity="proposal"))], ["dismiss", "undismiss"])

    def test_suggestions_only_when_switched_on(self):
        vendor = self.register_canada_post()
        self.call("PUT", f"/api/admin/vendors/{vendor['id']}/coding",
                  {"group_name": "", "use_case_id": self.use_case("Shipping & Logistics")["id"]})
        rows = {t["id"]: t for t in self.transactions()["transactions"]}
        self.assertFalse(any("suggestion" in t for t in rows.values()))
        self.call("PUT", "/api/admin/settings/coding", {"suggestions": True})
        rows = {t["id"]: t for t in self.transactions()["transactions"]}
        self.assertEqual(rows["t_miss"]["suggestion"]["usage"], "Shipping & Logistics")
        self.assertEqual(rows["t_miss"]["suggestion"]["vendor"], "Canada Post")
        self.assertNotIn("suggestion", rows["t_c1"])                     # it has a use case
        with mock.patch.object(main, "current_user", return_value=dict(MEMBER)):
            rows = {t["id"]: t for t in self.transactions()["transactions"]}
        self.assertNotIn("suggestion", rows["t_miss"])                   # not the member's card
        self.assertEqual(rows["t_mine"]["suggestion"]["usage"], "Shipping & Logistics")

    def test_another_store_never_sees_these_folders(self):
        self.assertEqual(self.get("/api/admin/vendors")["total"], 3)          # cached for this store
        other = MemoryStore()
        other.upsert_invoices([inv("x1", "Lone", 1.0, "2026-09-01", vendor_folder="Lone Folder")])
        main._clients["store"] = other                                       # not set_clients: the cache stays
        r = self.get("/api/admin/vendors")
        self.assertEqual([row["folder"] for row in r["rows"]], ["Lone Folder"])

    def test_without_the_registry_tables(self):
        with mock.patch.object(self.store, "list_vendor_registry", side_effect=MISSING_TABLE):
            admin.invalidate()
            self.assertEqual(self.get("/api/admin/registry")["available"], False)
            self.assertEqual(self.get("/api/admin/proposals"), {"proposals": [], "dismissed": 0, "available": False})
            self.assertFalse(self.get("/api/admin/vendors")["registry_available"])
            r = self.call("POST", "/api/admin/vendors", {"name": "Esso"}, expect=502)
            self.assertTrue(r["schema_missing"])

    def test_a_registry_read_that_fails_is_not_a_missing_schema(self):
        # A timeout or a 5xx on a write route is a database error a retry may clear, not "apply schema.sql".
        for exc in (StoreError("GET vendors -> ReadTimeout: timed out"), StoreError("GET vendors -> 503 unavailable", status=503)):
            admin.invalidate()
            with mock.patch.object(self.store, "list_vendor_registry", side_effect=exc), mock.patch("builtins.print"):
                r = self.call("POST", "/api/admin/vendors", {"name": "Esso"}, expect=502)
            self.assertEqual(r, {"error": f"database error: {exc}"})
        admin.invalidate()
        self.call("POST", "/api/admin/vendors", {"name": "Esso"}, expect=201)


# ---------------------------------------------------------------------------
# Card numbers on receipts that no card claims
# ---------------------------------------------------------------------------
class CardNumberAdminTests(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload().status_code, 200)
        self.card_save({"last4": "1610", "card_number": "4449"})
        self.store.upsert_invoices([
            inv("r1", "Anthropic", 150.0, "2026-09-03", card="5758"),
            inv("r2", "Costco", 88.0, "2026-09-19", card="5758"),
            inv("r3", "Amazon", 45.10, "2026-09-08", card="5758"),
            inv("r4", "Esso", 10.0, "2026-09-08", card="4449"),       # the number entered on 1610
            inv("r5", "Esso", 10.0, "2026-09-08", card="1610"),
        ])

    def test_unclaimed_numbers_and_the_likely_card(self):
        unclaimed = {u["number"]: u for u in self.get("/api/admin/cards")["unclaimed"]}
        self.assertEqual(list(unclaimed), ["5758"])
        self.assertEqual(unclaimed["5758"]["invoices"], 3)
        r = self.get("/api/admin/card-numbers/5758")
        self.assertEqual(r["considered"], 3)
        likely = r["likely"][0]
        self.assertEqual({k: likely[k] for k in ("last4", "hits", "card_number", "card_number_receipts", "months")},
                         {"last4": "1610", "hits": 2, "card_number": "4449", "card_number_receipts": 1, "months": ["2026-09"]})
        self.assertEqual([c["last4"] for c in r["likely"]], ["1610", "2301"])
        self.assertIsNone(r["ignored"])
        self.get("/api/admin/card-numbers/9999", expect=404)
        self.get("/api/admin/card-numbers/57x8", expect=400)

    def test_ignore_and_restore(self):
        r = self.call("POST", "/api/admin/card-numbers/5758/ignore", {"reason": " a supplier's own card "})
        self.assertEqual((r["ignored"]["reason"], r["ignored"]["by"]), ("a supplier's own card", "manne"))
        unclaimed = {u["number"]: u for u in self.get("/api/admin/cards")["unclaimed"]}
        self.assertEqual(unclaimed["5758"]["ignored"]["reason"], "a supplier's own card")
        self.assertEqual(self.get("/api/admin/card-numbers/5758")["ignored"]["by"], "manne")
        self.call("POST", "/api/admin/card-numbers/5758/ignore", {"reason": ""}, expect=400)
        self.call("POST", "/api/admin/card-numbers/57x8/ignore", {"reason": "x"}, expect=400)
        self.assertEqual(self.call("DELETE", "/api/admin/card-numbers/5758/ignore"), {"ok": True})
        self.assertIsNone({u["number"]: u for u in self.get("/api/admin/cards")["unclaimed"]}["5758"]["ignored"])
        self.call("DELETE", "/api/admin/card-numbers/5758/ignore", expect=404)
        self.assertEqual([a["action"] for a in reversed(self.audits(entity="card_number"))], ["ignore", "unignore"])

    def test_the_cards_list(self):
        r = self.get("/api/admin/cards")
        cards = {c["last4"]: c for c in r["cards"]}
        c = cards["1610"]
        self.assertEqual(c["activity"]["transactions"], 5)
        self.assertEqual(c["activity"]["missing_last_month"], "2026-09")       # the gallery's link to the dashboard
        self.assertEqual(cards["2301"]["activity"]["missing_last_month"], "2026-10")
        # 2301: one of its two missing invoices is in October, the month the link opens.
        self.assertEqual((cards["2301"]["activity"]["missing_newest"], cards["2301"]["activity"]["missing_months"]), (1, 2))
        self.store.upsert_cards([{"last4": "3922", "label": "Card ending 3922"}])     # no statement line yet
        idle = {c["last4"]: c for c in self.get("/api/admin/cards")["cards"]}["3922"]["activity"]
        self.assertEqual(idle, main.NO_ACTIVITY)
        self.assertIsNone(idle["missing_last_month"])
        self.assertEqual(c["last_change"]["actor"], "manne")
        self.assertIn("no_owner", c["flags"])
        self.assertNotIn("no_webhook", c["flags"])                   # 1610 has its own
        self.assertIn("never_reviewed", cards["2301"]["flags"])
        self.assertEqual(r["counts"]["all"], 3)
        self.assertEqual(r["counts"]["no_owner"], 3)
        self.assertEqual(r["flag_labels"]["no_owner"], "No owner")
        self.assertEqual(r["cost_centers"][0], "Head office")
        self.assertTrue(r["audit_available"])
        self.card_save({"last4": "4894", "default_cost_center": "EU", "primary_last4": "1610"})
        self.card_save({"last4": "1610", "default_cost_center": "US"})
        self.call("PATCH", "/api/admin/cost-centers?name=EU", {"active": False})
        cards = {c["last4"]: c for c in self.get("/api/admin/cards")["cards"]}
        self.assertFalse(cards["4894"]["default_cost_center_active"])
        self.assertIn("default_retired", cards["4894"]["flags"])
        self.assertEqual(cards["4894"]["primary_default"], "US")
        with mock.patch.object(self.store, "list_audit", side_effect=MISSING_TABLE), mock.patch("builtins.print"):
            r = self.get("/api/admin/cards")
        self.assertFalse(r["audit_available"])
        self.assertIsNone(r["cards"][0]["last_change"])


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------
class PeopleAdminTests(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.store.upsert_cards([{"last4": "4431", "label": "Card ending 4431", "holder_name": ""},
                                 {"last4": "7436", "label": "Card ending 7436", "holder_name": ""},
                                 {"last4": "3914", "label": "Card ending 3914", "holder_name": "NEW FRONTIER GLOBAL LIMITED"}])
        self.card_save({"last4": "4431", "holder_name": "George", "owner_email": "george@superhairpieces.com"})
        self.card_save({"last4": "7436", "holder_name": "George", "label": "George's Visa"})
        self.george = next(p for p in self.store.list_people() if p["name"] == "George")
        self.store.update_person(self.george["id"], {"email": "george@superhairpieces.com"})
        self.store.insert_transactions([dict(txn("t1", "2026-09-04", "Esso", 10.0, card="4431"), person_id=self.george["id"]),
                                        dict(txn("t2", "2026-09-05", "Esso", 11.0, card="4431"), person_id=self.george["id"])])

    def person(self, body, expect=201):
        return self.call("POST", "/api/people", body, expect)["person"]

    def people(self):
        return {p["name"]: p for p in self.get("/api/admin/people")["people"]}

    def test_who_holds_owns_and_codes_what(self):
        r = self.get("/api/admin/people")
        george = {p["name"]: p for p in r["people"]}["George"]
        self.assertEqual((george["cards_held"], george["cards_owned"], george["cannot_code"], george["purchases"]),
                         (["4431", "7436"], ["4431"], ["7436"], 2))
        self.assertEqual(r["holders_unlinked"], [{"last4": "3914", "holder_name": "NEW FRONTIER GLOBAL LIMITED", "likely": None}])
        nfg = self.person({"name": "New Frontier Global"})
        r = self.get("/api/admin/people")
        self.assertEqual(r["holders_unlinked"][0]["likely"], {"id": nfg["id"], "name": "New Frontier Global"})

    def test_rename(self):
        linda = self.person({"name": "Linda"})
        r = self.call("PATCH", f"/api/admin/people/{self.george['id']}", {"name": "linda"}, expect=409)
        self.assertEqual(r["merge_into"], {"id": linda["id"], "name": "Linda"})
        self.call("PATCH", f"/api/admin/people/{self.george['id']}", {"name": "GEORGE"})        # capitals only
        r = self.call("PATCH", f"/api/admin/people/{self.george['id']}", {"name": "George Smith"})
        self.assertEqual(r["person"]["name"], "George Smith")
        self.assertEqual(sorted(r["cards_updated"]), ["4431", "7436"])
        self.assertEqual((self.card("4431")["holder_name"], self.card("4431")["label"]),
                         ("George Smith", "Card ending 4431 - George Smith"))
        self.assertEqual((self.card("7436")["holder_name"], self.card("7436")["label"]), ("George Smith", "George's Visa"))
        card_rows = [a for a in self.audits(entity="card") if a["note"] == "via person rename"]
        self.assertEqual(len(card_rows), 4)                              # two renames, two cards each
        self.assertEqual(card_rows[0]["after"]["holder_name"], "George Smith")
        self.assertEqual([a["action"] for a in reversed(self.audits(entity="person", entity_id=self.george["id"]))],
                         ["rename", "rename"])
        self.call("PATCH", "/api/admin/people/junk", {"name": "x"}, expect=404)
        self.call("PATCH", f"/api/admin/people/{UUID_A}", {"name": "x"}, expect=404)
        self.call("PATCH", f"/api/admin/people/{self.george['id']}", {"name": " "}, expect=400)

    def test_email(self):
        linda = self.person({"name": "Linda"})
        sam = self.person({"name": "Sam"})
        self.call("PATCH", f"/api/admin/people/{linda['id']}", {"email": "Linda@Superhairpieces.com"})
        self.assertEqual(self.store.get_person(linda["id"])["email"], "linda@superhairpieces.com")
        for p in (linda, sam):
            self.call("PATCH", f"/api/admin/people/{p['id']}", {"email": ""})
            self.assertIsNone(self.store.get_person(p["id"])["email"])
        self.assertEqual(self.call("PATCH", f"/api/admin/people/{linda['id']}", {"email": "not an address"}, expect=400)["error"],
                         "email: 'not an address' is not an e-mail address")
        r = self.call("PATCH", f"/api/admin/people/{linda['id']}", {"email": "GEORGE@superhairpieces.com"}, expect=409)
        self.assertEqual(r["error"], "email: that e-mail already belongs to George")
        r = self.call("POST", "/api/people", {"name": "Someone", "email": "george@superhairpieces.com"}, expect=409)
        self.assertEqual(r["error"], "email: that e-mail already belongs to someone")

    def test_merge(self):
        geo = self.person({"name": "Geo Smith", "email": "geo@superhairpieces.com"})
        self.store.update_person(self.george["id"], {"email": None})
        self.store.insert_transactions([dict(txn("t3", "2026-09-06", "Esso", 12.0, card="7436"), person_id=geo["id"])])
        self.store.upsert_cards([{"last4": "3914", "person_id": geo["id"], "holder_name": "Geo Smith",
                                  "label": "Card ending 3914 - Geo Smith"}])
        r = self.call("POST", f"/api/admin/people/{geo['id']}/merge", {"into": self.george["id"]})
        self.assertEqual(r["moved"], {"transactions": 1, "cards": ["3914"]})
        self.assertEqual(r["person"]["email"], "geo@superhairpieces.com")
        self.assertIsNone(self.store.get_person(geo["id"]))
        self.assertEqual(self.store.get_transaction("t3")["person_id"], self.george["id"])
        self.assertEqual((self.card("3914")["person_id"], self.card("3914")["label"]),
                         (self.george["id"], "Card ending 3914 - George"))
        merge = self.audits(entity="person", entity_id=self.george["id"])[0]
        self.assertEqual((merge["action"], merge["after"]["transactions"]), ("merge", 1))
        self.assertEqual(self.audits(entity="card", entity_id="3914")[0]["note"], "via person merge")
        self.call("POST", f"/api/admin/people/{self.george['id']}/merge", {"into": self.george["id"]}, expect=400)
        self.call("POST", f"/api/admin/people/{self.george['id']}/merge", {"into": UUID_A}, expect=404)

    def test_delete(self):
        r = self.call("DELETE", f"/api/admin/people/{self.george['id']}", expect=409)
        self.assertEqual(r["holds_cards"], ["4431", "7436"])
        self.call("DELETE", f"/api/admin/people/{self.george['id']}?confirm=1", expect=409)
        self.assertIsNotNone(self.store.get_person(self.george["id"]))
        sam = self.person({"name": "Sam"})
        self.store.update_transaction("t1", {"person_id": sam["id"]})
        r = self.call("DELETE", f"/api/admin/people/{sam['id']}", expect=409)
        self.assertEqual(r["counts"], {"transactions": 1, "cards": []})
        self.assertEqual(r["error"], "Sam is assigned to 1 purchase; deleting unassigns them")
        r = self.call("DELETE", f"/api/admin/people/{sam['id']}?confirm=1")
        self.assertEqual(r["deleted"], sam["id"])
        self.assertIsNone(self.store.get_transaction("t1")["person_id"])
        self.call("DELETE", "/api/admin/people/junk", expect=404)
        self.assertEqual(self.audits(entity="person", entity_id=sam["id"])[0]["action"], "delete")

    def test_link_card_holders(self):
        self.person({"name": "New Frontier Global"})
        r = self.call("POST", "/api/admin/people/link-holders")
        self.assertEqual([(x["last4"], x["person"]["name"], x["created"]) for x in r["linked"]],
                         [("3914", "NEW FRONTIER GLOBAL LIMITED", True)])
        self.assertEqual([[p["name"] for p in pair] for pair in r["duplicates"]],
                         [["New Frontier Global", "NEW FRONTIER GLOBAL LIMITED"]])
        self.assertEqual(self.card("3914")["person_id"], r["linked"][0]["person"]["id"])
        self.assertEqual(self.call("POST", "/api/admin/people/link-holders")["linked"], [])
        self.assertEqual(self.audits(entity="card", entity_id="3914")[0]["action"], "link")


# ---------------------------------------------------------------------------
# History and Settings
# ---------------------------------------------------------------------------
class AuditApiTests(AdminApiTestCase):
    def test_newest_first_filters_and_paging(self):
        self.call("POST", "/api/admin/cost-centers", {"name": "Warehouse"}, expect=201)
        self.call("POST", "/api/admin/use-cases", {"name": "Training"}, expect=201)
        self.call("PATCH", "/api/admin/cost-centers?name=Warehouse", {"description": "stock"},
                  headers={"X-Dashboard-User": "Snow"})
        r = self.get("/api/admin/audit")
        self.assertEqual([(e["entity"], e["action"]) for e in r["entries"]],
                         [("cost_center", "update"), ("use_case", "create"), ("cost_center", "create")])
        self.assertTrue(r["available"])
        self.assertIn("vendor_coding", r["entities"])
        self.assertIsNone(r["next_before_id"])
        self.assertEqual(len(self.get("/api/admin/audit?entity=cost_center")["entries"]), 2)
        self.assertEqual(len(self.get("/api/admin/audit?entity=cost_center&entity_id=Warehouse")["entries"]), 2)
        self.assertEqual([e["actor"] for e in self.get("/api/admin/audit?actor=NOW")["entries"]], ["Snow"])
        page = self.get("/api/admin/audit?limit=2")
        self.assertEqual(len(page["entries"]), 2)
        older = self.get(f"/api/admin/audit?limit=2&before_id={page['next_before_id']}")
        self.assertEqual([(e["entity"], e["action"]) for e in older["entries"]], [("cost_center", "create")])
        self.assertIsNone(older["next_before_id"])
        self.get("/api/admin/audit?before_id=abc", expect=400)

    def test_before_the_schema(self):
        with mock.patch.object(self.store, "list_audit", side_effect=MISSING_TABLE):
            r = self.get("/api/admin/audit")
        self.assertEqual((r["entries"], r["available"], r["next_before_id"]), ([], False, None))


class SettingsApiTests(AdminApiTestCase):
    def test_settings(self):
        r = self.get("/api/admin/settings")
        self.assertEqual((r["auth_mode"], r["store"], r["ai_enabled"]), ("open", "memory", False))
        self.assertEqual(r["allowed_domains"], ["superhairpieces.com"])
        self.assertIn("ap@superhairpieces.com", r["ap_emails"])
        self.assertTrue(all(r["schema"].values()))
        self.assertTrue(r["schema"]["coding_split"])
        self.assertEqual(len(r["coding_split"]["retired"]), 11)
        self.assertEqual(r["coding"], {"suggestions": False, "by": None, "at": None})
        self.assertEqual(r["webhooks"]["cards"], ["1610"])
        self.assertTrue(r["webhooks"]["default"])
        self.assertEqual(r["receipt_upload"]["per_card"], {})
        self.assertEqual(r["alias_cache_seconds"], 600)
        self.assertEqual(r["registry_version"], 0)

    def test_the_suggestions_switch(self):
        r = self.call("PUT", "/api/admin/settings/coding", {"suggestions": True})
        self.assertEqual((r["coding"]["suggestions"], r["coding"]["by"]), (True, "manne"))
        self.assertTrue(self.get("/api/admin/settings")["coding"]["suggestions"])
        row = self.audits(entity="setting")[0]
        self.assertEqual((row["entity_id"], row["action"], row["before"], row["after"]["suggestions"]),
                         ("coding", "update", None, True))
        self.assertEqual(self.call("PUT", "/api/admin/settings/coding", {"suggestions": "yes"}, expect=400)["error"],
                         "suggestions: true or false")
        self.call("PUT", "/api/admin/settings/coding", ["not", "an", "object"], expect=400)


class StaticContractTests(unittest.TestCase):
    """The strings the pages and this API agree on (admin.html is F1's, index.html F2's)."""

    def read(self, name):
        with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
            return fh.read()

    def test_admin_page(self):
        page = self.read("admin.html")
        self.assertIn("<title>AP Admin</title>", page)
        for tab in ("cards", "coding", "vendors", "people", "history", "settings"):
            self.assertIn(f'data-tab="{tab}"', page)
        # The two Add forms on #coding are open at once: each keeps its own unsaved-changes guard, so an
        # Add (or erasing) in one never drops the other's draft unguarded.
        self.assertNotIn("'coding-add'", page)
        self.assertIn("function addOwner(kind) { return 'coding-add-' + kind; }", page)

    def test_cards_gallery(self):
        page = self.read("admin.html")
        # Gallery | Table, the gallery by default, remembered by the browser behind a try/catch.
        self.assertIn('data-cards-view="gallery" aria-pressed="true"', page)
        self.assertIn('data-cards-view="table" aria-pressed="false"', page)
        # By owner is the third view of the same switch, not a separate toggle that leaves Gallery pressed.
        self.assertIn('data-cards-view="owners" aria-pressed="false"', page)
        self.assertNotIn('id="cardsByOwner"', page)
        self.assertIn("var CARDS_VIEWS = ['gallery', 'table', 'owners'];", page)
        self.assertIn("try { var v = localStorage.getItem(CARDS_VIEW_KEY); return CARDS_VIEWS.indexOf(v) >= 0 ? v : 'gallery'; } catch (e) { return 'gallery'; }", page)
        self.assertIn("try { localStorage.setItem(CARDS_VIEW_KEY, cardsUi.view); } catch (e)", page)
        # Logos only from the server's banks list, and only its own files under /static/banks/.
        self.assertIn(r"function bankLogo(url) { url = String(url || ''); return /^\/static\/banks\/[A-Za-z0-9_-]+\.svg$/.test(url) ? url : ''; }", page)
        self.assertNotIn("rbc.svg", page)
        self.assertNotIn("chase.svg", page)
        # Every face class the page styles is a bank the server lists.
        for key in re.findall(r"\.cc-face\.bank-([a-z0-9-]+)", page):
            self.assertIn(key, main.BANKS)
        # Both views take the same filtered, sorted list; the editor sends the three new fields.
        self.assertIn("redraw(box, cardsUi.view === 'table' ? cardsTableHtml(rows) : cardsGalleryHtml(rows));", page)
        self.assertIn("if (cardsUi.view === 'owners') { redraw(box, ownersHtml(rows)); return; }", page)
        # Under a card the flags AP acts on, not the Chat-webhook flag nearly every card has, nor No owner (the
        # owners line says it); never_used hides inactive_90d, and status flags come after action flags.
        self.assertIn("var GALLERY_QUIET_FLAGS = ['no_webhook', 'no_owner'];", page)
        self.assertIn("var STATUS_FLAGS = ['never_used', 'inactive_90d'];", page)
        self.assertIn("if (flags.indexOf('never_used') >= 0) flags = flags.filter(function (f) { return f !== 'inactive_90d'; });", page)
        # The missing count links to the dashboard's own route (#/YYYY/MM?card=1234), built only from a
        # well-formed month and a four-digit card; no count badge on the card's corner.
        self.assertIn("return m && /^\\d{4}$/.test(last4 || '') ? '/#/' + m[1] + '/' + m[2] + '?card=' + encodeURIComponent(last4) : '';", page)
        self.assertIn("var m = /^(\\d{4})-(\\d{2})$/.exec(ym || '');", page)
        self.assertNotIn("cc-miss-badge", page)
        self.assertIn(r"var m = location.hash.match(/^#\/(\d{4})\/(\d{1,2})(?:\?(.*))?$/);", self.read("index.html"))
        # One tab stop per card: Edit is for the mouse. Search knows the network's short names.
        self.assertIn('class="btn btn-secondary btn-xs" tabindex="-1" data-open-card=', page)
        self.assertIn("var NETWORK_ALIASES = { visa: 'visa', mastercard: 'mastercard mc', amex: 'amex' };", page)
        self.assertIn("'bank', 'network',\n                   'product'", page)
        self.assertIn("<th>Bank</th>", page)
        self.assertIn("@media (prefers-reduced-motion: reduce) { .cc-card:hover { transform: none; } }", page)
        # The dashboard's Cards… dialog is unchanged: it never sends them.
        dashboard = self.read("index.html")
        for text in ('data-cf="bank"', 'data-cf="network"', 'data-cf="product"', "cc-card"):
            self.assertNotIn(text, dashboard)

    @staticmethod
    def function_src(page, name):
        """The source of the page's top-level `function name(...)`, up to the next one."""
        start = page.index(f"\n  function {name}(")
        return page[start:page.index("\n  function ", start + 1)]

    def test_card_editor_holder_and_group_are_pickers(self):
        # Holder and Group were inputs with a <datalist>: a browser lists only the suggestions matching
        # what the box already says, so with "Yin Wei" in it nobody else could be picked. They are
        # drop-downs of every person / group now; a datalist must not come back on either.
        page = self.read("admin.html")
        self.assertFalse("dlPeople" in page or "dlGroups" in page, "the Holder / Group datalist is back")
        form = self.function_src(page, "cardFormHtml")
        self.assertFalse("datalist" in form or re.search(r"\blist=", form), "the card editor has a datalist again")
        picker = self.function_src(page, "namePickerHtml")
        self.assertFalse("datalist" in picker or re.search(r"\blist=", picker), "the Holder / Group picker has a datalist")
        self.assertIn("field('holder_name', 'Holder', namePickerHtml('holder_name', people, cur.holder_name,", form)
        self.assertIn("field('group_name', 'Group', namePickerHtml('group_name', groups, cur.group_name,", form)
        self.assertIn("var people = nameList((d.people || []).map(function (p) { return p.name; }));", form)
        self.assertIn("var groups = nameList((d.groups || []).concat(['Hair', 'Beauty']))", form)
        self.assertIn("newLabel: 'New person…'", form)
        self.assertIn("notListed: ' (not in People)'", form)
        self.assertIn("newLabel: 'New group…'", form)
        # A <select> keeping the form's ids and names (the payload stays holder_name / group_name), with
        # "— none —" first, the card's value even when it is on no choice, and "New …" last.
        self.assertIn("""'<select class="inp" id="f_' + name + '" name="' + name + '">' + opts + opt(PICK_NEW, o.newLabel, false) + '</select>'""",
                      picker)
        self.assertIn("var opts = opt('', '— none —', !cur)", picker)
        self.assertIn("""if (cur && !found) opts += '<option value="' + esc(cur) + '" selected data-unlisted>' + esc(cur + o.notListed) + '</option>';""",
                      picker)
        # The box for a new name has no name of its own: values() reads it only on "New …".
        self.assertIn("""'<input class="inp hidden" id="f_' + name + '_new" maxlength=""", picker)
        self.assertIn("label: cleanText(fv('label')), holder_name: picked(holderPicker), group_name: picked(pickers[1]),", page)
        self.assertIn("if (p.sel.value !== PICK_NEW) return cleanText(p.sel.value);", page)
        # A typed name snaps only to the list's spelling (a listed choice's text), never to the card's own
        # (the unlisted choice, or a choice's value): "Chi Tung CHOW" typed for "Chi Tung Chow" is sent as typed.
        self.assertIn("return o.value && o.value !== PICK_NEW && !o.hasAttribute('data-unlisted') && o.text.toLowerCase() === key;", page)
        self.assertIn("return same ? same.text : typed;", page)
        # A new choice clears the field's message, and "Not in People" shows only for that holder or New person….
        self.assertIn("$('[data-err=\"' + name + '\"]', form).textContent = '';", page)
        self.assertIn("if (out) out.classList.toggle('hidden', !isNew && !sel.options[sel.selectedIndex].hasAttribute('data-unlisted'));", page)
        self.assertIn('<span id="f_holder_name_out"><b>Not in People</b>', form)

    def test_card_editor_owners_offer_people_and_new_address(self):
        # Owners was chips over a bare e-mail box. It is a drop-down like Holder's now: every person by name
        # (in Holder's order), then "Other e-mail address…"; a chip shows the person whose address it is.
        page = self.read("admin.html")
        form = self.function_src(page, "cardFormHtml")
        self.assertIn("field('owner_email', 'Owners', '<div class=\"owner-chips\" id=\"ownerChips\"></div>' + "
                      "ownerPickerHtml(d.people || []),", form)
        self.assertNotIn('<input id="f_owner_email" type="email"', page)
        picker = self.function_src(page, "ownerPickerHtml")
        self.assertFalse("datalist" in picker or re.search(r"\blist=", picker), "the Owners picker has a datalist")
        # The select keeps the id the field's <label for> names; "Add an owner…" first, then plain names (no
        # groups, no addresses, no card counts), "Other e-mail address…" last.
        self.assertIn("""return '<select class="inp" id="f_owner_email">' + opt('', 'Add an owner…', true) +""", picker)
        self.assertIn("return cleanText(a.name).toLowerCase().localeCompare(cleanText(b.name).toLowerCase());", picker)
        self.assertIn("}).map(function (p) { return opt(PICK_PERSON + p.id, cleanText(p.name), false); }).join('') +", picker)
        self.assertIn("opt(PICK_NEW, 'Other e-mail address…', false) + '</select>' +", picker)
        self.assertNotIn("optgroup", picker)
        self.assertNotIn("Other addresses in use", page)
        self.assertNotRegex(page, r"name\)?\.toLowerCase\(\)\s*\+\s*'@", "an owner address is derived from a name")
        editor = self.function_src(page, "openCardEditor")
        # A chip shows the person's name, the address in its title; a person already an owner is greyed out.
        self.assertIn("var out = outsideDomain(em), n = ownerName(em), title = [n !== em ? em : '', out ? OUTSIDE_TITLE : ''].filter(Boolean).join(' · ');", editor)
        self.assertIn("if (p) o.disabled = owners.indexOf(emailOf(p)) >= 0;", editor)
        # A person with no e-mail opens their box, with one line under it; Other e-mail address… the same box.
        self.assertIn("if (v === PICK_NEW || (p && !em)) {", editor)
        self.assertIn("ownerBox.setAttribute('aria-label', person ? n + '’s e-mail' : 'Other e-mail address');", editor)
        self.assertIn("ownerHint.textContent = person ? n + ' has no e-mail in People yet: Save adds it there too.' : '';", editor)
        # Arrowing through the closed list adds nothing; nor does leaving it on a person only arrowed to.
        self.assertIn("ownerSel.addEventListener('change', function () { takeOwnerChoice(!ownerKeyed); });", editor)
        self.assertIn("if (!em || !chosen) return;", editor)
        self.assertNotRegex(editor, r"addEventListener\('blur', function \(\) \{[^}]*takeOwnerChoice", "leaving the Owners list adds an owner")
        self.assertIn("ownerSel.addEventListener('blur', function () { if (ownerArrowed) { ownerArrowed = false; ownerSel.value = ''; } });", editor)
        # What was added or removed is read out; the × that removed a chip hands the focus on.
        self.assertIn('<span class="sr-only" id="f_owner_email_said" role="status" aria-live="polite"></span>', picker)
        self.assertIn("ownerSaid(added.join(', ') + (added.length === 1 ? ' added as an owner' : ' added as owners'));", editor)
        self.assertIn("ownerSaid(n + ' removed from the owners');", editor)
        self.assertIn("($all('#ownerChips [data-rm-owner]', body)[Math.min(i, owners.length - 1)] || ownerSel).focus();", editor)
        # The box: what it says is added before the list's choice moves on (refused, the choice goes back), not
        # kept back for later; a person's box takes one address; Backspace there removes no chip.
        self.assertNotIn("drafts", editor)
        self.assertIn("if (person !== ownerPerson) ownerBox.value = '';", editor)
        self.assertIn("if (!commitOwnerBox(false)) { ownerSel.value = was; return; }", editor)
        self.assertIn("var why = list.length > 1 ? cleanText(p.name) + '’s e-mail is one address' : personEmailProblem(p, list[0]);", editor)
        self.assertNotIn("'Backspace'", editor)
        # Browsing the list changes nothing: its choice is not a value, so it does not mark the card unsaved.
        self.assertIn("function formChanged(ev) { if (ev.target.id !== 'f_owner_email') markDirty(); }", editor)
        self.assertNotIn("form.addEventListener('change', markDirty);", editor)
        # Save: the person's address goes to People first (PATCH), and the card only after it succeeded.
        save = editor[editor.index("async function save(confirmed)"):]
        self.assertIn("{ method: 'PATCH', body: { email: pe.email } }", editor)
        self.assertLess(save.index("var named = await savePersonEmails();"), save.index("api('/api/cards', { method: 'POST', body: payload })"))
        self.assertIn("if (!named) { busy(btn, false); ownerSel.focus(); return; }", save)
        # People is read again before the PATCH, so an e-mail set since the page loaded is not overwritten.
        spe = editor[editor.index("async function savePersonEmails()"):editor.index("function savedInPeople(names)")]
        self.assertLess(spe.index("now = await api('/api/admin/people');"), spe.index("method: 'PATCH'"))
        self.assertIn("return em && em !== pe.email ? { pe: pe, email: em } : null;", spe)
        # A refusal after an earlier person's e-mail was saved, or a card refused after it, says it was saved.
        self.assertIn("? savedInPeople(names) + '; ' + cleanText(pe.person.name) + '’s was not (' + why + '), and the card was not saved.'", spe)
        self.assertIn("if (named.length) ownerErr(savedInPeople(named) + '; the card was not saved.');", save)
        # The payload is unchanged: owner_email, a comma-separated string, sent only when it changed.
        self.assertIn("owner_email: owners.join(', '),", editor)
        self.assertIn("CARD_KEYS.forEach(function (k) { if (k in v && v[k] !== orig[k]) payload[k] = v[k]; });", save)

    def test_people_tab_changes_reload_the_cards(self):
        # The card editor's Owners list shows each person's e-mail (from the cards' data), and Save writes an
        # address typed for "no e-mail yet" to the person: after any change on the People tab, Cards are read
        # again, or the list offers an old address and overwrites a new one.
        page = self.read("admin.html")
        edit = self.function_src(page, "openPersonEditor")
        self.assertNotIn("if (upd.length) state.loaded.cards = false;", edit)
        self.assertIn("      state.loaded.cards = false;\n      loadPeople(true);", edit)
        merge = page[page.index("async function mergePeople("):page.index("async function deletePerson(")]
        self.assertNotIn("if (cards.length) state.loaded.cards = false;", merge)
        self.assertIn("state.loaded.cards = false;", merge)
        delete = page[page.index("async function deletePerson("):page.index("async function makeOwner(")]
        self.assertIn("state.loaded.cards = false;", delete)

    def test_dashboard_cards_dialog_group_is_a_picker(self):
        # The dashboard's Cards… dialog had the same <datalist> for Group: a card in Hair offered only Hair.
        page = self.read("index.html")
        self.assertNotIn("groupList", page)
        no_datalist = re.compile(r'<datalist[\s>]|\blist="')
        self.assertNotRegex(self.function_src(page, "cardsModalHtml"), no_datalist)
        self.assertIn("'<td data-group-cell>' + groupPickHtml(c.group_name) + '</td>'", page)
        pick = self.function_src(page, "groupPickHtml")
        self.assertNotRegex(pick, no_datalist)
        self.assertIn('<select class="inp" data-cf="group_name"', pick)
        self.assertIn("'<option value=\"__new__\">New group…</option></select>'", pick)
        # The card's own spelling stays the matched choice's value, so an untouched row sends what it had.
        self.assertIn("return '<option value=\"' + esc(same ? cur : g) + '\"' + (same ? ' selected' : '') + '>' + esc(g) + '</option>';", pick)
        self.assertIn("if (payload.group_name === '__new__') {", page)

    def test_card_face_sizes_stay_on_the_face(self):
        page = self.read("admin.html")
        # .cc-bank-text is sized in cqw, which only the face (.cc-card, the query container) defines: in the
        # table cqw is the viewport's, so the rule is scoped to the face, and a table logo that fails to load
        # becomes a plain span (the face's lettering only inside .cc-bank).
        self.assertIsNone(re.search(r"^\s*\.cc-bank-text\s*[{,]", page, re.M))
        self.assertIn(".cc-bank .cc-bank-text { display: inline-flex;", page)
        self.assertIn("if (img.closest('.cc-bank')) span.className = 'cc-bank-text';", page)
        # The product sits beside the logo on its row (a lockup behind a hairline), clear of the chip, and
        # its ellipsis lands before the tags column (group pill, "Reminders off"), never under it.
        self.assertIn(".cc-bank { flex: 1 1 auto; display: flex; align-items: center;", page)
        self.assertIn(".cc-product { flex: 0 1 auto; min-width: 0; max-width: none;", page)
        self.assertIn("border-left: 1px solid rgba(255,255,255,0.32);", page)
        # No bank: engraved lettering at the 11px floor, not a dashed form box.
        self.assertIn(">Bank not set</span>", page)
        self.assertNotIn("+ Set bank", page)
        rule = re.search(r"\.cc-face\.no-bank \.cc-bank-text \{[^}]*\}", page).group(0)
        self.assertNotIn("dashed", rule)
        self.assertIn("font-size: max(11px, 3.2cqw)", rule)
        # The narrowest phones keep the activity line whole.
        self.assertIn("@media (max-width: 360px) { .cc-meta { font-size: 12px; } }", page)

    def test_cards_tab_finds_and_lists_every_card(self):
        page = self.read("admin.html")
        # A number the receipts show (7436's 7444) finds its card, and is said beside it, never on the face.
        self.assertIn("net, (c.receipts_show || []).join(' ')]", page)
        self.assertIn("title.push('<div class=\"cc-title\">Receipts show ' + esc(shows.join(', ')) + '</div>');", page)
        self.assertIn("if (shows.length) sub.push('receipts show ' + shows.join(', '));", page)
        # A short placeholder that is never cut off; the fields it searches in the title.
        self.assertIn('placeholder="Search cards" aria-label="Search the cards" title="Card number (or a number its receipts show)', page)
        # By owner ends with the cards nobody owns, so with "All" or the No owner chip it lists them.
        self.assertIn("var orphans = rows.filter(function (c) { return !(c.owners || []).length; });", page)
        self.assertIn("if (!emails.length && !orphans.length) return", page)
        self.assertNotIn("No owner has a card that matches.", page)
        self.assertIn(".chip.btn-wrap { white-space: normal; }", page)
        # The sort is remembered like the view, behind a try/catch; the summary offers the worst card first.
        self.assertIn("try { var v = localStorage.getItem(CARDS_SORT_KEY); return CARDS_SORT_NAMES.indexOf(v) >= 0 ? v : 'card'; } catch (e) { return 'card'; }", page)
        self.assertIn("try { localStorage.setItem(CARDS_SORT_KEY, cardsUi.sort); } catch (e)", page)
        self.assertEqual(re.findall(r'<option value="([a-z]+)">Sort: ', page), ["card", "holder", "group", "active", "missing"])
        self.assertIn("var CARDS_SORT_NAMES = ['card', 'holder', 'group', 'active', 'missing'];", page)
        self.assertIn('<div id="cardsSummary" class="cards-sum hidden"></div>', page)
        self.assertIn("if (t.closest('[data-sort-missing]')) { setCardsSort('missing'); return; }", page)
        # The missing total counts every month; its link names the one month the dashboard opens on.
        self.assertIn("function missingGo(a, missing, last4, monthOnly) {", page)
        self.assertNotIn("' · last used '", page)
        # Table: the holder's flag in the flag's own words, the flags the gallery shows, and Missing first.
        self.assertIn("<div class=\"sub\">not linked to People</div>", page)
        self.assertNotIn("not a person", page)
        self.assertIn("var flagHtml = galleryFlags(c).filter(function (f) { return f !== 'holder_not_person'; }).map(", page)
        self.assertIn('<th class="td-right">Missing</th><th>Transactions</th>', page)
        self.assertIn('colspan="12"', page)

    def test_readme_patch_row_matches_the_fill_race(self):
        # test_a_cost_centre_set_meanwhile_is_kept: a cost centre set while a use-case save would fill the
        # card's default is kept and the use case saved (200, no filled_cost_center); the API table says so.
        with open(os.path.join(HERE, "..", "README.md"), encoding="utf-8") as fh:
            row = next(line for line in fh if line.startswith("| `PATCH /api/transactions/<id>` |"))
        self.assertNotIn("cost centre meanwhile it answers 409", row)
        self.assertIn("if someone set a cost centre meanwhile, the use case is still saved, that cost centre is kept "
                      "and no `filled_cost_center` is returned", row)

    def test_dashboard(self):
        page = self.read("index.html")
        for text in ('href="/admin"', "No use case", "label: 'Use case'", "'Use case saved'"):
            self.assertIn(text, page)
        for text in ("'No cost centre'", "'Usage saved'", 'aria-label="Usage"'):
            self.assertNotIn(text, page)
        # On a phone the two drop-downs sit side by side and both read "— none —": each is named.
        for text in ('td[data-col="cost"]::before { content: "Cost centre"; }', 'td[data-col="usage"]::before { content: "Use case"; }'):
            self.assertIn(text, page)


if __name__ == "__main__":
    unittest.main()
