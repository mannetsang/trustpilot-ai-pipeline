"""Tests for the admin pages' storage and helpers: schema.sql's admin-portal section, the new store
methods (MemoryStore and the PostgREST requests SupabaseStore sends) and admin.py's pure functions.

Run from tests/:  python -m unittest discover -s . -q
"""

import datetime as dt
import json
import os
import re
import sys
import time
import unittest
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import inv, txn  # noqa: E402  (sets the env and imports main)
from test_review_fixes import FakeResponse  # noqa: E402

import admin  # noqa: E402
import store as store_module  # noqa: E402
from store import (AUDIT_FIELDS, DEFAULT_USE_CASES, USE_CASE_FIELDS, MemoryStore, StoreError,  # noqa: E402
                   SupabaseStore)

SCHEMA = os.path.join(HERE, "..", "schema.sql")
PLACES = ["Head office", "Salon - Dufferin", "Salon - Ridgeway", "Salon - Consumer", "Salon - Eglinton",
          "Salon - STC", "Salon - Rapistan", "Salon - Brampton", "US", "EU"]
EXPENSES = [n for n, _ in DEFAULT_USE_CASES]


def conflict_code(ctx):
    return ctx.exception.code


# ---------------------------------------------------------------------------
# Parity and schema.sql
# ---------------------------------------------------------------------------
class StoreParityTests(unittest.TestCase):
    def test_both_stores_have_the_same_methods(self):
        def public(cls):
            return {n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n))}
        self.assertEqual(public(MemoryStore), public(SupabaseStore) - {"select", "insert", "upsert", "patch", "delete"})


class SchemaSqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SCHEMA, encoding="utf-8") as f:
            cls.sql = f.read()
        # The do-blocks hold statements of their own; most checks look at the file without them.
        cls.outside = re.sub(r"do \$\$.*?end \$\$;", "", cls.sql, flags=re.S)

    def test_every_table_is_revoked_and_has_rls(self):
        tables = re.findall(r"create table if not exists (\w+)", self.sql)
        self.assertIn("admin_audit", tables)
        revokes = re.findall(r"revoke all on (?!sequence)(.*?) from anon, authenticated;", self.sql, re.S)
        self.assertEqual(len(revokes), 1)
        revoked = {t.strip() for t in revokes[0].split(",")}
        for table in tables:
            self.assertIn(table, revoked, table)
            self.assertRegex(self.sql, rf"alter table {table}\s+enable row level security;")
        self.assertIn("revoke all on sequence admin_audit_id_seq from anon, authenticated;", self.sql)

    def test_cost_centre_seed_is_the_places_on_an_empty_table_only(self):
        seed = re.search(r"insert into cost_centers \(name, sort\)\nselect.*?;", self.outside, re.S).group(0)
        self.assertIn("where not exists (select 1 from cost_centers)", seed)
        for name in PLACES:
            self.assertIn(f"('{name}', ", seed)
        self.assertNotIn("('Marketing', 10)", self.outside)

    def test_the_split_runs_once_and_seeds_the_expense_types_in_order(self):
        block = next(b for b in re.findall(r"do \$\$.*?end \$\$;", self.sql, re.S) if "coding_split" in b)
        self.assertIn("where key = 'coding_split'", block)
        listed = re.search(r"expense text\[\] := array\[(.*?)\];", block, re.S).group(1)
        self.assertEqual(re.findall(r"'([^']+)'", listed), EXPENSES)

    def test_uncoded_counts_purchases_without_a_use_case(self):
        line = next(ln for ln in self.sql.splitlines() if ln.rstrip().endswith("as uncoded"))
        self.assertIn("usage", line)
        self.assertNotIn("cost_center", line)

    def test_card_default_follows_a_cost_centre_rename(self):
        self.assertRegex(self.sql, r"add column if not exists default_cost_center text\s+references "
                                   r"cost_centers\(name\) on update cascade")

    def test_the_card_face_columns_and_checks(self):
        # The columns store.CARD_FIELDS names are added (idempotently), and the network check matches
        # store.CARD_NETWORKS, which MemoryStore and POST /api/cards enforce too.
        for column in ("bank", "network", "product"):
            self.assertIn(column, store_module.CARD_FIELDS)
            self.assertRegex(self.outside, rf"alter table cards add column if not exists {column}\s+text;")
        check = re.search(r"alter table cards add constraint cards_network_check\s+check \((.*?)\);", self.outside, re.S)
        self.assertEqual(re.findall(r"'([a-z]+)'", check.group(1)), list(store_module.CARD_NETWORKS))
        self.assertIn("network is null or", check.group(1))
        self.assertLess(self.outside.index("drop constraint if exists cards_network_check"),
                        self.outside.index("add constraint cards_network_check"))
        self.assertIn(f"check (product is null or length(product) <= {store_module.CARD_PRODUCT_MAX})", self.outside)
        self.assertLess(self.outside.index("drop constraint if exists cards_product_check"),
                        self.outside.index("add constraint cards_product_check"))
        self.assertIn(("cards.bank", ("cards", "bank")), store_module.SCHEMA_PROBES)

    def test_the_duplicate_guard_comes_before_the_unique_indexes(self):
        guard = self.sql.index("raise exception 'schema.sql not applied: two people share the e-mail")
        self.assertLess(guard, self.sql.index("people_email_unique"))
        self.assertLess(guard, self.sql.index("cost_centers_name_lower_unique"))

    def test_the_last_statement_reloads_postgrest(self):
        statements = [ln for ln in self.sql.splitlines() if ln.strip() and not ln.startswith("--")]
        self.assertEqual(statements[-1], "notify pgrst, 'reload schema';")


# ---------------------------------------------------------------------------
# StoreError
# ---------------------------------------------------------------------------
class StoreErrorTests(unittest.TestCase):
    def error_for(self, status, body):
        st = SupabaseStore("https://x.supabase.co", "service-key")
        with mock.patch.object(store_module.requests, "request", lambda *a, **k: FakeResponse(status=status, body=body)):
            with self.assertRaises(StoreError) as ctx:
                st._req("GET", "use_cases")
        return ctx.exception

    def test_a_unique_violation(self):
        exc = self.error_for(409, '{"code":"23505","message":"dup"}')
        self.assertEqual((exc.code, exc.status, exc.conflict, exc.missing), ("23505", 409, True, False))
        self.assertIn("23505", str(exc))

    def test_missing_tables_and_columns(self):
        self.assertTrue(self.error_for(404, '{"code":"PGRST205","message":"Could not find the table"}').missing)
        self.assertTrue(self.error_for(400, '{"code":"42703","message":"column x does not exist"}').missing)
        self.assertTrue(self.error_for(400, '{"code":"PGRST204","message":"no column"}').missing)

    def test_a_check_violation_is_not_missing(self):
        exc = self.error_for(400, '{"code":"23514","message":"check"}')
        self.assertEqual((exc.missing, exc.conflict), (False, False))

    def test_a_body_that_is_not_json(self):
        exc = self.error_for(502, "<html>bad gateway</html>")
        self.assertEqual((exc.status, exc.code, exc.missing), (502, None, False))

    def test_a_plain_error_has_no_flags(self):
        exc = StoreError("x")
        self.assertEqual((exc.missing, exc.conflict, exc.status, exc.code), (False, False, None, None))


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------
class MemoryCodingTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()

    def test_cost_centres_are_the_places_after_the_split(self):
        self.assertEqual(self.store.list_cost_centers(), PLACES)
        rows = self.store.list_cost_center_rows()
        self.assertEqual(len(rows), 21)
        self.assertEqual([r["name"] for r in rows if r["active"]], PLACES)
        retired = [r for r in rows if not r["active"]]
        self.assertEqual(sorted(r["name"] for r in retired), sorted(EXPENSES))
        self.assertTrue(all(r["updated_by"] == "schema:coding_split" for r in retired))
        self.assertEqual(set(rows[0]), set(store_module.COST_CENTER_FIELDS))
        self.assertEqual(self.store.get_state("coding_split")["retired"], EXPENSES)

    def test_rename_clash_and_cascade(self):
        self.assertEqual(self.store.update_cost_center("Head office", {"name": "Head Office"})["name"], "Head Office")
        with self.assertRaises(StoreError) as ctx:
            self.store.add_cost_center({"name": "salon - dufferin", "sort": 5})
        self.assertEqual(conflict_code(ctx), "23505")
        self.assertTrue(ctx.exception.conflict)
        with self.assertRaises(StoreError):
            self.store.add_cost_center({"name": "marketing", "sort": 5})        # the retired row still holds it
        with self.assertRaises(StoreError):
            self.store.update_cost_center("US", {"name": "eu"})
        self.store.upsert_cards([{"last4": "4431", "default_cost_center": "Salon - Dufferin"},
                                 {"last4": "1610", "default_cost_center": "US"}])
        self.store.update_cost_center("Salon - Dufferin", {"name": "Dufferin"})
        cards = {c["last4"]: c for c in self.store.list_cards()}
        self.assertEqual((cards["4431"]["default_cost_center"], cards["1610"]["default_cost_center"]), ("Dufferin", "US"))
        self.assertIsNone(self.store.update_cost_center("Nowhere", {"sort": 1}))

    def test_add_and_retire(self):
        row = self.store.add_cost_center({"name": "Warehouse", "sort": 130, "description": "Brampton warehouse",
                                          "updated_by": "ap@x", "updated_at": "2026-11-24T00:00:00Z"})
        self.assertEqual((row["name"], row["active"], row["sort"]), ("Warehouse", True, 130))
        self.assertEqual(self.store.list_cost_centers()[-1], "Warehouse")
        self.store.update_cost_center("Warehouse", {"active": False})
        self.assertNotIn("Warehouse", self.store.list_cost_centers())
        self.assertIn("Warehouse", [r["name"] for r in self.store.list_cost_center_rows()])

    def test_use_cases(self):
        rows = self.store.list_use_cases()
        self.assertEqual([r["name"] for r in rows], EXPENSES)
        self.assertEqual(rows[-1]["name"], "Other")
        self.assertEqual(set(rows[0]), set(USE_CASE_FIELDS))
        made = self.store.create_use_case({"name": "Training", "sort": 110, "group_name": "Hair", "created_by": "ap@x"})
        self.assertTrue(store_module.UUID_RE.match(made["id"]))
        self.assertEqual((made["active"], made["created_by"]), (True, "ap@x"))
        self.assertEqual(set(made), set(USE_CASE_FIELDS))
        with self.assertRaises(StoreError) as ctx:
            self.store.create_use_case({"name": "TRAINING"})
        self.assertEqual(conflict_code(ctx), "23505")
        travel = next(r for r in rows if r["name"] == "Travel")
        with self.assertRaises(StoreError):
            self.store.update_use_case(travel["id"], {"name": "training"})
        self.assertEqual(self.store.update_use_case(travel["id"], {"name": "TRAVEL"})["name"], "TRAVEL")   # itself
        self.store.update_use_case(made["id"], {"active": False})
        self.assertNotIn("Training", [r["name"] for r in self.store.list_use_cases(active_only=True)])
        self.assertIsNone(self.store.update_use_case("junk", {"name": "x"}))
        self.assertIsNone(self.store.update_use_case(str(uuid.uuid4()), {"name": "x"}))

    def test_values_in_use_and_uncoded(self):
        rows = [txn("a", "2026-10-01", "A", 1.0), txn("b", "2026-10-02", "B", 2.0), txn("c", "2026-10-03", "C", 3.0),
                txn("d", "2026-10-04", "D", 4.0, type_="credit")]
        rows[0].update(usage="Marketing ", cost_center="Salon - STC")
        rows[1].update(cost_center="Marketing")          # a cost centre alone does not code a purchase
        rows[2].update(usage="  ")
        rows[3].update(usage="Other")
        self.store.insert_transactions(rows)
        self.assertEqual(self.store.coding_values_in_use(),
                         {"cost_center": {"Salon - STC": 1, "Marketing": 1}, "usage": {"Marketing": 1, "Other": 1}})
        summary = self.store.month_summary()
        self.assertEqual(sum(g["uncoded"] for g in summary), 2)

    def test_an_unknown_default_cost_centre_is_refused(self):
        with self.assertRaises(StoreError) as ctx:
            self.store.upsert_cards([{"last4": "1234", "label": "x"}, {"last4": "5678", "default_cost_center": "Nowhere"}])
        self.assertEqual(conflict_code(ctx), "23503")
        self.assertEqual(self.store.list_cards(), [])                   # nothing written
        self.store.upsert_cards([{"last4": "5678", "default_cost_center": "Marketing"}])   # retired, still a row
        self.store.upsert_cards([{"last4": "5678", "default_cost_center": None}])
        self.assertIsNone(self.store.list_cards()[0]["default_cost_center"])


class MemoryPeopleTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.amy = self.store.create_person("Amy", email="amy@x.com", created_by="ap@x")[0]
        self.bo = self.store.create_person("Bo Li")[0]

    def test_people_are_id_name_and_email(self):
        self.assertEqual(self.amy, {"id": self.amy["id"], "name": "Amy", "email": "amy@x.com"})
        self.store.update_person(self.amy["id"], {"name": "Amy Chen", "updated_by": "ap@x", "updated_at": "2026-11-24T00:00:00Z"})
        self.assertEqual(self.store.get_person(self.amy["id"]), {"id": self.amy["id"], "name": "Amy Chen", "email": "amy@x.com"})
        self.assertEqual([set(p) for p in self.store.list_people()], [{"id", "name", "email"}] * 2)
        full = next(p for p in self.store.list_people_rows() if p["id"] == self.amy["id"])
        self.assertEqual((full["created_by"], full["updated_by"]), ("ap@x", "ap@x"))
        self.assertEqual(set(full), set(store_module.PERSON_FIELDS))

    def test_clashes(self):
        with self.assertRaises(StoreError) as ctx:
            self.store.update_person(self.bo["id"], {"name": "amy"})
        self.assertEqual(conflict_code(ctx), "23505")
        with self.assertRaises(StoreError):
            self.store.update_person(self.bo["id"], {"email": "AMY@x.com"})
        self.assertEqual(self.store.update_person(self.amy["id"], {"name": "AMY", "email": "Amy@X.com"})["name"], "AMY")
        with self.assertRaises(StoreError) as ctx:
            self.store.create_person("Cy", email="amy@x.com")
        self.assertEqual(conflict_code(ctx), "23505")
        self.assertEqual(self.store.create_person("amy ")[1], False)          # the name: the same person

    def test_empty_emails_never_clash(self):
        self.store.update_person(self.amy["id"], {"email": ""})
        self.store.update_person(self.bo["id"], {"email": "  "})
        self.assertEqual([p["email"] for p in self.store.list_people()], [None, None])
        self.assertIsNone(self.store.create_person("Cy", email="")[0]["email"])

    def test_ids_that_are_not_uuids(self):
        self.assertIsNone(self.store.update_person("junk", {"name": "x"}))
        self.assertFalse(self.store.delete_person("junk"))
        self.assertFalse(self.store.delete_person(str(uuid.uuid4())))
        self.assertEqual(self.store.reassign_person_transactions("junk", self.bo["id"]), 0)

    def test_delete_reassign_and_usage(self):
        rows = [txn(f"t{i}", "2026-10-01", "X", 1.0) for i in range(3)]
        self.store.insert_transactions(rows)
        for i in range(3):
            self.store.update_transaction(f"t{i}", {"person_id": self.amy["id"]})
        self.store.upsert_cards([{"last4": "1610", "person_id": self.amy["id"]}])
        self.assertEqual(self.store.people_usage(), {self.amy["id"]: 3})
        self.assertEqual(self.store.reassign_person_transactions(self.amy["id"], self.bo["id"]), 3)
        self.assertEqual(self.store.people_usage(), {self.bo["id"]: 3})
        self.assertTrue(self.store.delete_person(self.amy["id"]))
        self.assertIsNone(self.store.list_cards()[0]["person_id"])
        self.assertTrue(self.store.delete_person(self.bo["id"]))
        self.assertEqual(self.store.people_usage(), {})
        self.assertIsNone(self.store.get_transaction("t0")["person_id"])


class MemoryCardTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store.upsert_cards([{"last4": "4431"}, {"last4": "7436", "primary_last4": "4431"}, {"last4": "9999"}])
        self.store.insert_transactions([txn("a", "2026-10-01", "X", 1.0, card="4431"),
                                        txn("b", "2026-10-02", "Y", 2.0, card="4431")])
        self.store.insert_statement({"file_name": "Transaction Export.xlsx", "cards": ["4431", "1610"],
                                     "uploaded_at": "2026-10-03T00:00:00Z"})

    def test_card_usage(self):
        self.assertEqual(self.store.card_usage("4431"), {"transactions": 2, "statements": 1, "supplementary": 1})
        self.assertEqual(self.store.card_usage("9999"), {"transactions": 0, "statements": 0, "supplementary": 0})

    def test_delete_card(self):
        with self.assertRaises(StoreError) as ctx:
            self.store.delete_card("4431")
        self.assertEqual(conflict_code(ctx), "23503")
        self.assertTrue(self.store.delete_card("9999"))
        self.assertFalse(self.store.delete_card("9999"))
        self.assertEqual([c["last4"] for c in self.store.list_cards()], ["4431", "7436"])

    def test_latest_notifications(self):
        for sent_at, detail in (("2026-10-01T00:00:00Z", "first"), ("2026-11-01T00:00:00Z", "second"),
                                ("2026-11-01T00:00:00Z", "third")):
            self.store.log_notification({"card_last4": "4431", "year": 2026, "month": 10, "sent_at": sent_at,
                                         "sent_by": "ap@x", "ok": True, "detail": detail, "missing_count": 1})
        self.store.log_notification({"card_last4": "7436", "year": 2026, "month": 9, "sent_at": "2026-09-01T00:00:00Z",
                                     "sent_by": "scheduler", "ok": False, "detail": "no webhook"})
        latest = self.store.latest_notifications()
        self.assertEqual(latest["4431"]["detail"], "third")
        self.assertEqual(set(latest["4431"]), {"card_last4", "year", "month", "sent_at", "sent_by", "ok", "detail"})
        self.assertEqual(latest["7436"]["ok"], False)


class MemoryAuditTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        for i, (entity, eid, actor) in enumerate([("card", "4431", "AP@superhairpieces.com"),
                                                  ("card", "1610", "manne@superhairpieces.com"),
                                                  ("use_case", "u1", "ap@superhairpieces.com"),
                                                  ("card", "4431", "snow@superhairpieces.com")]):
            self.store.insert_audit({"actor": actor, "auth_mode": "iap", "entity": entity, "entity_id": eid,
                                     "action": "update", "before": {"n": i}, "after": {"n": i + 1}, "ignored": 1})

    def test_newest_first_and_filters(self):
        rows = self.store.list_audit()
        self.assertEqual([r["id"] for r in rows], [4, 3, 2, 1])
        self.assertEqual(set(rows[0]), {"id", "at", "actor", "auth_mode", "entity", "entity_id", "action", "before",
                                        "after", "note"})
        self.assertEqual(rows[0]["before"], {"n": 3})
        self.assertEqual([r["id"] for r in self.store.list_audit(entity="card")], [4, 2, 1])
        self.assertEqual([r["id"] for r in self.store.list_audit(entity="card", entity_id="4431")], [4, 1])
        self.assertEqual([r["id"] for r in self.store.list_audit(actor="ap@")], [3, 1])
        self.assertEqual([r["id"] for r in self.store.list_audit(actor="*Manne(")], [2])
        self.assertEqual([r["id"] for r in self.store.list_audit(before_id=3)], [2, 1])
        self.assertEqual([r["id"] for r in self.store.list_audit(limit=2)], [4, 3])

    def test_rows_are_copies(self):
        self.store.list_audit()[0]["before"]["n"] = 99
        self.assertEqual(self.store.list_audit()[0]["before"], {"n": 3})


class MemoryRegistryTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.vendor = self.store.create_vendor({"name": "Canada Post", "created_by": "ap@x"})

    def test_vendor_crud_and_clashes(self):
        self.assertEqual((self.vendor["kind"], self.vendor["code_each"], self.vendor["active"]), ("supplier", False, True))
        self.assertEqual(set(self.vendor), set(store_module.VENDOR_FIELDS))
        with self.assertRaises(StoreError) as ctx:
            self.store.create_vendor({"name": "CANADA POST"})
        self.assertEqual(conflict_code(ctx), "23505")
        amazon = self.store.create_vendor({"name": "Amazon", "code_each": True})
        with self.assertRaises(StoreError):
            self.store.update_vendor(amazon["id"], {"name": "canada post"})
        self.assertEqual(self.store.update_vendor(amazon["id"], {"name": "AMAZON", "kind": "agent"})["kind"], "agent")
        self.assertIsNone(self.store.update_vendor("junk", {"name": "x"}))
        self.assertIsNone(self.store.update_vendor(str(uuid.uuid4()), {"name": "x"}))

    def test_folders_and_patterns(self):
        folder = self.store.add_vendor_folder({"vendor_id": self.vendor["id"], "folder": "Canada  Post"})
        self.assertEqual(folder["folder_key"], "canada post")
        other = self.store.create_vendor({"name": "Purolator"})
        with self.assertRaises(StoreError) as ctx:
            self.store.add_vendor_folder({"vendor_id": other["id"], "folder": "canada post", "folder_key": "canada post"})
        self.assertEqual(conflict_code(ctx), "23505")
        with self.assertRaises(StoreError) as ctx:
            self.store.add_vendor_folder({"vendor_id": str(uuid.uuid4()), "folder": "x", "folder_key": "x"})
        self.assertEqual(conflict_code(ctx), "23503")
        pattern = self.store.add_vendor_pattern({"vendor_id": self.vendor["id"], "pattern": "cpc scp"})
        self.assertEqual((pattern["kind"], pattern["match_type"], pattern["status"], pattern["source"]),
                         ("descriptor", "prefix", "probation", "admin"))
        with self.assertRaises(StoreError):
            self.store.add_vendor_pattern({"vendor_id": other["id"], "pattern": "cpc scp"})
        self.store.add_vendor_pattern({"vendor_id": other["id"], "pattern": "cpc scp", "kind": "invoice_vendor",
                                       "match_type": "exact"})                 # another kind: no clash
        self.assertEqual(self.store.update_vendor_pattern(pattern["id"], {"status": "off"})["status"], "off")
        self.assertIsNone(self.store.update_vendor_pattern("junk", {"status": "off"}))
        reg = self.store.list_vendor_registry()
        self.assertEqual({k: len(v) for k, v in reg.items()}, {"vendors": 2, "folders": 1, "patterns": 2, "coding": 0})
        self.assertTrue(self.store.delete_vendor_pattern(pattern["id"]))
        self.assertFalse(self.store.delete_vendor_pattern(pattern["id"]))
        self.assertFalse(self.store.delete_vendor_pattern("junk"))
        self.assertTrue(self.store.delete_vendor_folder(folder["id"]))
        self.assertFalse(self.store.delete_vendor_folder("junk"))

    def test_coding_per_group(self):
        uc = {r["name"]: r["id"] for r in self.store.list_use_cases()}
        first = self.store.set_vendor_coding(self.vendor["id"], "Hair", uc["Shipping & Logistics"], "ap@x")
        self.assertEqual((first["mode"], first["group_name"], first["created_by"]), ("suggest", "Hair", "ap@x"))
        again = self.store.set_vendor_coding(self.vendor["id"], "hair", uc["Marketing"], "manne@x")
        self.assertEqual((again["id"], again["use_case_id"], again["updated_by"]), (first["id"], uc["Marketing"], "manne@x"))
        self.store.set_vendor_coding(self.vendor["id"], "", uc["Other"], "ap@x")
        self.assertEqual(len(self.store.list_vendor_registry()["coding"]), 2)
        self.assertIsNone(self.store.set_vendor_coding("junk", "", uc["Other"], "ap@x"))
        with self.assertRaises(StoreError):
            self.store.set_vendor_coding(self.vendor["id"], "Beauty", str(uuid.uuid4()), "ap@x")
        self.assertTrue(self.store.delete_vendor_coding(self.vendor["id"], "HAIR"))
        self.assertFalse(self.store.delete_vendor_coding(self.vendor["id"], "Hair"))
        self.assertFalse(self.store.delete_vendor_coding("junk", ""))
        self.assertEqual([r["group_name"] for r in self.store.list_vendor_registry()["coding"]], [""])

    def test_schema_status_and_catalogue_rows(self):
        self.assertEqual(set(self.store.schema_status().values()), {True})
        self.assertEqual(len(self.store.schema_status()), 11)
        self.store.insert_transactions([txn("a", "2026-10-01", "X", 1.0)])
        self.store.upsert_invoices([inv("i1", "Canada Post", 1.0, "2026-10-01")])
        self.assertEqual(list(self.store.admin_transaction_rows()[0]), list(store_module.ADMIN_TXN_COLUMNS))
        self.assertEqual(self.store.admin_transaction_rows()[0]["also_invoice_ids"], [])
        self.assertEqual(list(self.store.admin_invoice_rows()[0]), list(store_module.ADMIN_INVOICE_COLUMNS))


# ---------------------------------------------------------------------------
# SupabaseStore: what goes over the wire
# ---------------------------------------------------------------------------
class SupabaseShapingTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.responses = [], []
        self.store = SupabaseStore("https://x.supabase.co", "service-key")
        patcher = mock.patch.object(store_module.requests, "request", self.fake_request)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "table": url.rsplit("/", 1)[1], "params": dict(params or {}),
                           "json": json, "prefer": (headers or {}).get("Prefer", "")})
        return self.responses.pop(0) if self.responses else FakeResponse()

    def respond(self, *bodies):
        self.responses = [b if isinstance(b, FakeResponse) else FakeResponse(body=json.dumps(b)) for b in bodies]

    def test_insert_audit_posts_the_audit_fields(self):
        self.store.insert_audit({"actor": "ap@x", "auth_mode": "iap", "entity": "card", "entity_id": "4431",
                                 "action": "update", "before": None, "after": {"a": 1}, "extra": "dropped"})
        call = self.calls[0]
        self.assertEqual((call["method"], call["table"], call["prefer"]), ("POST", "admin_audit", "return=minimal"))
        self.assertEqual(list(call["json"][0]), list(AUDIT_FIELDS))

    def test_create_use_case_posts_every_field(self):
        self.respond(FakeResponse(status=201, body=json.dumps([{"id": "x", "name": "Training"}])))
        self.store.create_use_case({"name": "Training", "sort": 110})
        row = self.calls[0]["json"][0]
        self.assertEqual(set(row), set(USE_CASE_FIELDS))
        self.assertTrue(store_module.UUID_RE.match(row["id"]))
        self.assertEqual((row["active"], row["sort"], row["group_name"]), (True, 110, None))

    def test_update_cost_center_patches_by_exact_name(self):
        self.respond([{"name": "Inventory", "sort": 70}])
        row = self.store.update_cost_center("Inventory / COGS", {"name": "Inventory", "bogus": 1})
        call = self.calls[0]
        self.assertEqual((call["method"], call["params"], call["json"]),
                         ("PATCH", {"name": "eq.Inventory / COGS"}, {"name": "Inventory"}))
        self.assertEqual(call["prefer"], "return=representation")
        self.assertEqual(row["name"], "Inventory")

    def test_list_cost_centers_keeps_active_and_unknown_rows(self):
        self.respond(FakeResponse(body=json.dumps([{"name": "Head office", "sort": 100, "active": True},
                                                   {"name": "Marketing", "sort": 10, "active": False},
                                                   {"name": "US", "sort": 120}]),
                                  headers={"Content-Range": "0-2/3"}))
        self.assertEqual(self.store.list_cost_centers(), ["Head office", "US"])

    def test_list_cost_center_rows_fills_missing_columns(self):
        self.respond(FakeResponse(body=json.dumps([{"name": "US", "sort": 120}]), headers={"Content-Range": "0-0/1"}))
        self.assertEqual(self.store.list_cost_center_rows(),
                         [{"name": "US", "sort": 120, "active": True, "description": None, "updated_by": None,
                           "updated_at": None}])

    def test_list_audit_is_one_get(self):
        self.store.list_audit(entity="card", entity_id="4431", actor="*ap(x),", before_id=40, limit=25)
        self.assertEqual(len(self.calls), 1)
        params = self.calls[0]["params"]
        self.assertEqual((params["order"], params["limit"], params["id"], params["actor"]),
                         ("id.desc", 25, "lt.40", "ilike.*apx*"))
        self.assertEqual((params["entity"], params["entity_id"]), ("eq.card", "eq.4431"))
        self.assertNotIn("offset", params)

    def test_card_usage_counts_without_reading_rows(self):
        self.respond(FakeResponse(body="[]", headers={"Content-Range": "0-0/12"}),
                     FakeResponse(body="[]", headers={"Content-Range": "*/0"}),
                     FakeResponse(body="[]", headers={"Content-Range": "0-0/1"}))
        self.assertEqual(self.store.card_usage("1234"), {"transactions": 12, "statements": 0, "supplementary": 1})
        self.assertEqual(self.calls[1]["params"]["cards"], 'cs.{"1234"}')
        self.assertTrue(all(c["prefer"] == "count=exact" and c["params"]["limit"] == 1 for c in self.calls))
        self.assertEqual(self.calls[0]["params"]["card_last4"], "eq.1234")
        self.assertEqual(self.calls[2]["params"]["primary_last4"], "eq.1234")

    def test_schema_status_maps_missing_to_false(self):
        missing = FakeResponse(status=404, body='{"code":"PGRST205","message":"Could not find the table"}')
        self.respond(missing, FakeResponse(), *[FakeResponse()] * 9)
        status = self.store.schema_status()
        self.assertEqual((status["admin_audit"], status["use_cases"]), (False, True))
        self.assertEqual(self.calls[0]["params"], {"select": "id", "limit": 1})
        self.assertEqual(self.calls[2]["params"], {"select": "active", "limit": 1})

    def test_schema_status_raises_other_failures(self):
        self.respond(FakeResponse(status=500, body='{"message":"down"}'))
        with self.assertRaises(StoreError):
            self.store.schema_status()

    def test_list_people_still_selects_the_view(self):
        self.store.list_people()
        self.assertEqual(self.calls[0]["params"]["select"], "id,name,email")

    def test_create_person_sends_no_empty_email(self):
        self.respond(FakeResponse(body="[]", headers={"Content-Range": "*/0"}),
                     FakeResponse(status=201, body=json.dumps([{"id": "p", "name": "Amy", "email": None, "created_by": None}])))
        person, created = self.store.create_person("Amy", email="  ")
        self.assertEqual(self.calls[1]["json"][0]["email"], None)
        self.assertEqual((person, created), ({"id": "p", "name": "Amy", "email": None}, True))

    def test_create_person_reraises_an_email_clash(self):
        empty = FakeResponse(body="[]", headers={"Content-Range": "*/0"})
        self.respond(empty, FakeResponse(status=409, body='{"code":"23505","message":"people_email_unique"}'), empty)
        with self.assertRaises(StoreError) as ctx:
            self.store.create_person("Amy", email="taken@x.com")
        self.assertTrue(ctx.exception.conflict)

    def test_delete_vendor_coding_selects_then_deletes_by_id(self):
        vid = str(uuid.uuid4())
        self.respond(FakeResponse(body=json.dumps([{"id": "c1", "vendor_id": vid, "group_name": "Hair_%"},
                                                   {"id": "c2", "vendor_id": vid, "group_name": "hair  _%"}]),
                                  headers={"Content-Range": "0-1/2"}),
                     [{"id": "c1"}])
        self.assertTrue(self.store.delete_vendor_coding(vid, "HAIR_%"))
        self.assertEqual(self.calls[0]["params"]["vendor_id"], f"eq.{vid}")
        self.assertEqual((self.calls[1]["method"], self.calls[1]["params"]), ("DELETE", {"id": "eq.c1"}))
        self.assertFalse(any("ilike" in str(c["params"]) for c in self.calls))

    def test_set_vendor_coding_patches_in_place_or_inserts(self):
        vid, ucid = str(uuid.uuid4()), str(uuid.uuid4())
        self.respond(FakeResponse(body=json.dumps([{"id": "c1", "vendor_id": vid, "group_name": "Hair"}]),
                                  headers={"Content-Range": "0-0/1"}), [{"id": "c1", "use_case_id": ucid}])
        self.store.set_vendor_coding(vid, "hair", ucid, "ap@x")
        self.assertEqual((self.calls[1]["method"], self.calls[1]["params"]), ("PATCH", {"id": "eq.c1"}))
        self.assertEqual(set(self.calls[1]["json"]), {"use_case_id", "updated_by", "updated_at"})
        self.calls = []
        self.respond(FakeResponse(body="[]", headers={"Content-Range": "*/0"}), [{"id": "c2"}])
        self.store.set_vendor_coding(vid, "Beauty", ucid, "ap@x")
        self.assertEqual(self.calls[1]["method"], "POST")
        self.assertEqual(set(self.calls[1]["json"][0]), set(store_module.VENDOR_CODING_FIELDS))
        self.assertEqual(self.calls[1]["json"][0]["mode"], "suggest")

    def test_ids_that_are_not_uuids_send_nothing(self):
        self.assertIsNone(self.store.update_person("junk", {"name": "x"}))
        self.assertFalse(self.store.delete_vendor_pattern("junk"))
        self.assertIsNone(self.store.update_use_case("junk", {"name": "x"}))
        self.assertEqual(self.store.reassign_person_transactions("junk", str(uuid.uuid4())), 0)
        self.assertEqual(self.calls, [])

    def test_update_person_blank_email_is_null(self):
        pid = str(uuid.uuid4())
        self.respond([{"id": pid, "name": "Amy", "email": None, "created_by": "x"}])
        self.assertEqual(self.store.update_person(pid, {"email": " "}), {"id": pid, "name": "Amy", "email": None})
        self.assertEqual(self.calls[0]["json"], {"email": None})

    def test_reassign_counts_the_rows_moved(self):
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        self.respond([{"id": "t1"}, {"id": "t2"}])
        self.assertEqual(self.store.reassign_person_transactions(a, b), 2)
        call = self.calls[0]
        self.assertEqual((call["method"], call["params"], call["json"], call["prefer"]),
                         ("PATCH", {"person_id": f"eq.{a}", "select": "id"}, {"person_id": b}, "return=representation"))

    def test_latest_notifications_order(self):
        rows = [{"card_last4": "4431", "detail": "new"}, {"card_last4": "4431", "detail": "old"}]
        self.respond(FakeResponse(body=json.dumps(rows),
                                  headers={"Content-Range": "0-1/2"}))
        self.assertEqual(self.store.latest_notifications()["4431"]["detail"], "new")
        self.assertEqual(self.calls[0]["params"]["order"], "sent_at.desc,id.desc")

    def test_catalogue_reads_only_their_columns(self):
        self.store.admin_transaction_rows()
        self.store.admin_invoice_rows()
        self.assertEqual(self.calls[0]["params"]["select"], ",".join(store_module.ADMIN_TXN_COLUMNS))
        self.assertEqual(self.calls[1]["params"]["select"], ",".join(store_module.ADMIN_INVOICE_COLUMNS))
        self.assertEqual((self.calls[0]["params"]["order"], self.calls[1]["params"]["order"]), ("id", "id"))

    def test_registry_reads_four_tables_by_id(self):
        self.store.list_vendor_registry()
        self.assertEqual([c["table"] for c in self.calls], ["vendors", "vendor_folders", "vendor_patterns", "vendor_coding"])
        self.assertTrue(all(c["params"]["order"] == "id.asc" for c in self.calls))


# ---------------------------------------------------------------------------
# admin.py
# ---------------------------------------------------------------------------
class AdminCacheTests(unittest.TestCase):
    def setUp(self):
        admin.invalidate()
        self.addCleanup(admin.invalidate)

    def test_keyed_by_store(self):
        a, b = MemoryStore(), MemoryStore()
        a.insert_transactions([txn("a1", "2026-10-01", "X", 1.0)])
        b.insert_transactions([txn("b1", "2026-10-01", "Y", 2.0)])
        self.assertIsNone(admin.cached_at("transactions"))
        self.assertEqual([t["id"] for t in admin.transaction_rows(a)], ["a1"])
        self.assertEqual([t["id"] for t in admin.transaction_rows(b)], ["b1"])
        self.assertIsNotNone(admin.cached_at("transactions"))
        b.insert_transactions([txn("b2", "2026-10-01", "Z", 2.0)])
        self.assertEqual(len(admin.transaction_rows(b)), 1)                      # cached
        self.assertEqual(len(admin.transaction_rows(b, fresh=True)), 2)
        admin.invalidate()
        self.assertIsNone(admin.cached_at("transactions"))

    def test_ttl_and_invalidate_one_key(self):
        st = MemoryStore()
        calls = []
        admin.cached("k", st, lambda: calls.append(1) or len(calls))
        with mock.patch.object(admin.time, "monotonic", return_value=time.monotonic() + admin.CACHE_TTL + 1):
            self.assertEqual(admin.cached("k", st, lambda: calls.append(1) or len(calls)), 2)
        admin.invoice_rows(st)
        admin.invalidate("k")
        self.assertIsNone(admin.cached_at("k"))
        self.assertIsNotNone(admin.cached_at("invoices"))

    def test_registry_missing_is_none_for_a_minute(self):
        st = MemoryStore()
        with mock.patch.object(st, "list_vendor_registry", side_effect=StoreError("x", status=404, code="PGRST205")) as m:
            self.assertIsNone(admin.registry(st))
            self.assertIsNone(admin.registry(st))
            self.assertEqual(m.call_count, 1)
            with mock.patch.object(admin.time, "monotonic", return_value=time.monotonic() + admin.MISSING_TTL + 1):
                admin.registry(st)
            self.assertEqual(m.call_count, 2)
        self.assertIsInstance(admin.registry(st, fresh=True), admin.RegistryIndex)
        with mock.patch.object(st, "list_vendor_registry", side_effect=StoreError("down", status=500)):
            with self.assertRaises(StoreError):
                admin.registry(st, fresh=True)


class AdminTextTests(unittest.TestCase):
    def test_descriptor_prefix(self):
        for text, want in (("UPS CANADA BILLING C", "ups canada"), ("FEDEX95845920", "fedex"),
                           ("PAYPAL *FIVERR", "paypal fiverr"), ("AMZN Mktp CA*2K3L", "amzn mktp"),
                           ("CPC / SCP 123456", "cpc scp"), ("", "")):
            self.assertEqual(admin.descriptor_prefix(text), want, text)
        self.assertEqual(admin.first_word("UPS CANADA"), "ups")
        self.assertEqual(admin.normalise_pattern("  Canada-Post  Corp. 1234 "), "canada post corp")

    def test_pattern_problem(self):
        self.assertEqual(admin.pattern_problem("cpc scp"), None)
        self.assertEqual(admin.pattern_problem("sq"), "pattern: at least 3 letters or digits")
        self.assertIn("4 letters or more", admin.pattern_problem("air"))
        self.assertIn("'paypal' is a payment processor", admin.pattern_problem("paypal"))
        self.assertIsNone(admin.pattern_problem("paypal fiverr"))
        self.assertEqual(admin.pattern_problem("x" * 121), "pattern: at most 120 characters")
        self.assertEqual(admin.pattern_problem("canada post", "bank"), "kind: descriptor or invoice_vendor")
        self.assertTrue(admin.pattern_problem("canada post", "invoice_vendor", "prefix").startswith("match_type:"))
        self.assertTrue(admin.pattern_problem("canada post", "descriptor", "contains").startswith("match_type:"))
        self.assertIsNone(admin.pattern_problem("air", "descriptor", "exact"))
        self.assertIsNone(admin.pattern_problem("dhl", "invoice_vendor", "exact"))

    def test_pattern_hits_whole_words(self):
        ups = {"pattern": "ups", "match_type": "prefix"}
        self.assertTrue(admin.pattern_hits(ups, "UPS*1234567 CANADA"))
        self.assertTrue(admin.pattern_hits(ups, "ups"))
        self.assertFalse(admin.pattern_hits(ups, "UPSTART CO"))
        exact = {"pattern": "canada post", "match_type": "exact"}
        self.assertTrue(admin.pattern_hits(exact, "CANADA POST"))
        self.assertFalse(admin.pattern_hits(exact, "Canada Post Corporation"))

    def test_names(self):
        self.assertEqual(admin.name_key("  Salon -   Dufferin "), "salon - dufferin")
        self.assertEqual(admin.folder_key("Canada  POST"), "canada post")
        self.assertEqual(admin.snap_name("salon - DUFFERIN", ["US", "Salon - Dufferin"]), "Salon - Dufferin")
        self.assertIsNone(admin.snap_name("Dufferin", ["Salon - Dufferin"]))
        self.assertIsNone(admin.snap_name("", [""]))
        self.assertEqual(admin.card_groups([{"group_name": "Hair"}, {"group_name": "hair "}, {"group_name": "Beauty"},
                                            {"group_name": None}]), ["Beauty", "Hair"])
        self.assertEqual(admin.owner_emails("A@x.com, b@y.com;c@x.com"), ["a@x.com", "b@y.com", "c@x.com"])


def view(**kw):
    base = {"last4": "4431", "owner_email": "ap@superhairpieces.com", "group_name": "Hair", "holder_name": "George",
            "person_id": "p1", "webhook_configured": True, "default_cost_center": "Salon - STC", "primary_last4": ""}
    base.update(kw)
    return base


class AdminCardsTests(unittest.TestCase):
    def test_recent_months_and_activity_across_a_year(self):
        today = dt.date(2027, 1, 15)
        self.assertEqual(admin.recent_months(today), {(2027, 1), (2026, 12), (2026, 11)})
        rows = [{"year": 2026, "month": 10, "card_last4": "4431", "transactions": 4, "missing": 1},
                {"year": 2026, "month": 12, "card_last4": "4431", "transactions": 3, "missing": 2},
                {"year": 2027, "month": 1, "card_last4": "4431", "transactions": 1, "missing": 0},
                {"year": 2026, "month": 9, "card_last4": "1610", "transactions": 2, "missing": 0}]
        act = admin.card_activity(rows, today)
        self.assertEqual(act["4431"], {"transactions": 8, "recent": 4, "missing": 3, "first_month": "2026-10",
                                       "last_month": "2027-01", "missing_last_month": "2026-12"})
        self.assertEqual(act["1610"]["recent"], 0)
        self.assertIsNone(act["1610"]["missing_last_month"])
        self.assertNotIn("9999", act)

    def test_missing_last_month_is_the_newest_month_with_a_missing_invoice(self):
        # Rows in any order (month_summary returns them newest first), across a year end; a month whose
        # invoices are all in does not count, however new.
        today = dt.date(2027, 2, 1)
        rows = [{"year": 2027, "month": 2, "card_last4": "4421", "transactions": 3, "missing": 0},
                {"year": 2026, "month": 11, "card_last4": "4421", "transactions": 2, "missing": 1},
                {"year": 2027, "month": 1, "card_last4": "4421", "transactions": 5, "missing": 2},
                {"year": 2026, "month": 12, "card_last4": "4421", "transactions": 1, "missing": None}]
        act = admin.card_activity(rows, today)["4421"]
        self.assertEqual(act["missing_last_month"], "2027-01")
        self.assertEqual(act["last_month"], "2027-02")
        self.assertEqual(act["missing"], 3)

    def test_card_flags(self):
        busy = {"transactions": 5, "recent": 2}
        self.assertEqual(admin.card_flags(view(), busy, "2026-11-01T00:00:00Z", ["Salon - STC"]), [])
        self.assertEqual(admin.card_flags(view(owner_email="", group_name="", person_id="", webhook_configured=False),
                                          {}, None, ["Salon - STC"]),
                         ["no_owner", "no_group", "holder_not_person", "no_webhook", "inactive_90d", "never_used",
                          "never_reviewed"])
        self.assertEqual(admin.card_flags(view(), busy, "x", ["US"]), ["default_retired"])
        sup = view(primary_last4="4431", default_cost_center="")
        self.assertEqual(admin.card_flags(sup, busy, "x", ["US"], primary_default="Salon - STC"), ["no_default_primary_has"])
        self.assertEqual(admin.card_flags(sup, busy, "x", ["US"], primary_default=None), [])
        self.assertEqual(admin.card_flags(view(default_cost_center=""), busy, "x", [], primary_default="US"), [])
        self.assertEqual(set(admin.FLAG_LABELS), set(admin.FLAGS))

    def test_owners(self):
        self.assertEqual(admin.owner_list("ap@superhairpieces.com, someone@gmail.com", {"superhairpieces.com"}),
                         [{"email": "ap@superhairpieces.com", "outside_domain": False},
                          {"email": "someone@gmail.com", "outside_domain": True}])
        self.assertEqual(admin.owners_index([{"last4": "4431", "owner_email": "b@x, a@x"},
                                             {"last4": "1610", "owner_email": "a@x"},
                                             {"last4": "9999", "owner_email": None}]),
                         {"a@x": ["1610", "4431"], "b@x": ["4431"]})

    def test_unclaimed_numbers(self):
        cards = [{"last4": "4431", "card_number": "4449"}, {"last4": "3914"}]
        docs = [inv("r1", "Esso", 10.0, "2026-10-01", vendor_folder="Esso", card="5758"),
                inv("r2", "Esso", 20.0, "2026-10-05", vendor_folder="ESSO", card="5758"),
                inv("r3", "Esso", 30.0, "2026-09-05", vendor_folder="Esso", card="5758"),
                inv("r4", "UPS", 30.0, "2026-09-07", vendor_folder="UPS", card="5758"),
                inv("r5", "X", 1.0, "2026-09-07", card="4431"),          # a card's last four
                inv("r6", "X", 1.0, "2026-09-07", card="4449"),          # entered as printed on 4431
                inv("r7", "X", 1.0, "2026-09-07", card="3906"),          # a reissue number of 3914
                inv("r8", "X", 1.0, "2026-09-07", card="1234"),
                inv("r9", "X", 1.0, "2026-09-07", card="12345"),
                dict(inv("r10", "X", 1.0, "2026-09-07", card="7777"), removed_at="2026-10-01T00:00:00Z")]
        rows = admin.unclaimed_numbers(docs, cards, {"1234": {"reason": "a gift card", "by": "ap@x", "at": "t"}})
        self.assertEqual([r["number"] for r in rows], ["5758", "1234"])
        top = rows[0]
        self.assertEqual((top["invoices"], top["first"], top["last"], top["ignored"]), (4, "2026-09-05", "2026-10-05", None))
        self.assertEqual(top["folders"], [{"folder": "Esso", "count": 3}, {"folder": "UPS", "count": 1}])
        self.assertEqual(rows[1]["ignored"]["reason"], "a gift card")

    def test_likely_cards(self):
        cards = [{"last4": "4431", "label": "George", "card_number": "4449"}, {"last4": "1610", "label": "Linda"}]
        docs = [inv("r1", "Esso", 50.0, "2026-10-01", card="5758"),
                inv("r2", "Esso", 60.0, "2026-10-10", card="5758"),
                inv("r3", "Esso", None, "2026-10-12", card="5758"),            # no total: not considered
                inv("r4", "Esso", 70.0, "2026-11-01", card="5758"),
                inv("o1", "X", 1.0, "2026-10-01", card="4449"), inv("o2", "X", 1.0, "2026-10-02", card="4449")]
        txns = [txn("a", "2026-10-01", "ESSO", 50.0, card="4431"),             # day 0
                txn("a2", "2026-10-03", "ESSO", 50.0, card="4431"),            # same invoice, same card: once
                txn("b", "2026-10-31", "ESSO", 60.0, card="4431"),             # day 21
                txn("c", "2026-10-09", "ESSO", 60.0, card="1610"),             # day -1
                txn("d", "2026-11-23", "ESSO", 70.0, card="1610"),             # day 22
                txn("e", "2026-11-05", "ESSO", 70.01, card="1610", currency="USD"),
                txn("f", "2026-11-05", "ESSO", 99.0, card="1610", source_amount=70.0, source_currency="CAD"),
                txn("g", "2026-11-02", "REFUND", 70.0, card="4431", type_="credit")]
        out = admin.likely_cards("5758", docs, txns, cards)
        self.assertEqual(out["considered"], 3)
        self.assertEqual([i["id"] for i in out["invoices"]], ["r4", "r3", "r2", "r1"])
        candidates = {i["id"]: i["candidates"] for i in out["invoices"]}
        self.assertEqual(candidates, {"r1": ["4431"], "r2": ["4431"], "r3": [], "r4": ["1610"]})
        george = out["likely"][0]
        self.assertEqual((george["last4"], george["hits"], george["share"], george["card_number"],
                          george["card_number_receipts"], george["months"]),
                         ("4431", 2, 0.67, "4449", 2, ["2026-10"]))
        linda = out["likely"][1]
        self.assertEqual((linda["last4"], linda["hits"], linda["card_number"], linda["card_number_receipts"], linda["months"]),
                         ("1610", 1, None, 0, ["2026-11"]))

    def test_alias_changes(self):
        both = [{"last4": "3914"}, {"last4": "3922"}]
        moved = admin.alias_changes(both, [{"last4": "3922"}], "3914")
        self.assertEqual(moved, [{"number": n, "before": None, "after": "3922"} for n in ("3906", "3914", "3930")])
        self.assertEqual(admin.alias_changes(both + [{"last4": "1610"}], both, "1610"), [])
        taken = admin.alias_changes([{"last4": "3922"}], both, "3914")
        self.assertEqual(taken, [{"number": n, "before": "3922", "after": None} for n in ("3906", "3914", "3930")])


class AdminCodingTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()

    def test_coding_overview(self):
        rows = [txn(f"t{i}", "2026-10-01", "X", 1.0) for i in range(6)]
        rows[0].update(usage="Marketing", cost_center="marketing")
        rows[1].update(usage="marketing ", cost_center="Salon - STC")
        rows[2].update(usage="Ticktick", cost_center="Marketing")
        rows[3].update(usage="TICKTICK", cost_center="Warehouse")
        rows[4].update(usage="Ticktick")
        self.store.insert_transactions(rows)
        self.store.upsert_cards([{"last4": "4431", "default_cost_center": "Salon - STC"},
                                 {"last4": "1610", "default_cost_center": "Salon - STC"}, {"last4": "9999"}])
        out = admin.coding_overview(self.store.coding_values_in_use(), self.store.list_cost_center_rows(),
                                    self.store.list_use_cases(), self.store.list_cards())
        marketing = next(u for u in self.store.list_use_cases() if u["name"] == "Marketing")
        self.assertEqual(out["use_case_uses"][marketing["id"]], 2)
        self.assertEqual(out["cost_center_uses"]["Marketing"], 2)
        self.assertEqual(out["cost_center_uses"]["Salon - STC"], 1)
        self.assertEqual(out["usage_not_listed"], [{"value": "Ticktick", "uses": 3}])
        self.assertEqual(out["cost_centers_not_listed"], [{"value": "Warehouse", "uses": 1}])
        self.assertEqual(out["cost_centers_like_use_cases"], [{"value": "Marketing", "uses": 2, "use_case": "Marketing"}])
        self.assertEqual(out["card_defaults"], {"Salon - STC": ["1610", "4431"]})

    def test_new_use_case_sort(self):
        rows = self.store.list_use_cases()
        self.assertEqual(admin.new_use_case_sort(rows), 110)
        squeezed = [dict(r, sort=110) if r["name"] == "Other" else r for r in rows]
        self.assertEqual(admin.new_use_case_sort(squeezed), 109)
        self.assertEqual(admin.new_use_case_sort([]), 10)

    def test_use_cases_for_a_group(self):
        made = self.store.create_use_case({"name": "Hair training", "group_name": "Hair", "sort": 110})
        self.store.create_use_case({"name": "Old", "sort": 120, "active": False})
        rows = self.store.list_use_cases()
        self.assertIn("Hair training", [u["name"] for u in admin.use_cases_for_group(rows, "hair")])
        self.assertNotIn("Hair training", [u["name"] for u in admin.use_cases_for_group(rows, "Beauty")])
        self.assertNotIn("Hair training", [u["name"] for u in admin.use_cases_for_group(rows, "")])
        self.assertNotIn("Old", [u["name"] for u in admin.use_cases_for_group(rows, "")])
        self.assertEqual(admin.snap_use_case("hair  TRAINING", rows, "Hair"), "Hair training")
        self.assertIsNone(admin.snap_use_case("hair training", rows, "Beauty"))
        self.assertIsNone(admin.snap_use_case("old", rows, ""))
        self.assertEqual(admin.snap_use_case("marketing", rows, ""), "Marketing")
        self.assertEqual(made["group_name"], "Hair")
        self.assertEqual(admin.uses("MARKETING", {"Marketing": 2, "marketing ": 1, "Other": 5}), 3)


def purchase(tid, description, amount=10.0, status="matched", invoice_id=None, method="rule", card="4431",
             usage=None, updated_by=None, note=None, date="2026-10-05"):
    row = txn(tid, date, description, amount, card=card, status=status, invoice_id=invoice_id, method=method)
    row.update(usage=usage, updated_by=updated_by, match_note=note, also_invoice_ids=[])
    return row


class AdminVendorTests(unittest.TestCase):
    def setUp(self):
        admin.invalidate()
        self.store = MemoryStore()
        self.uc = {u["name"]: u for u in self.store.list_use_cases()}

    def fixture(self):
        docs = [inv(f"pw{i}", "ParkWhiz Inc", 10.0 + i, "2026-10-01", vendor_folder="Parkwhiz,") for i in range(4)]
        docs += [inv("cp1", "Canada Post", 5.0, "2026-09-02", month_folder="2026 09", vendor_folder="Canada Post"),
                 inv("cp2", "CANADA POST CORP", 6.0, "2026-10-02", month_folder="2026 10", vendor_folder="Canada Post"),
                 inv("cp3", "Canada Post", 7.0, "2026-10-03", month_folder="2026 10", vendor_folder="canada post"),
                 inv("cp4", "Canada Post", 8.0, "2026-11-03", month_folder="2026 11", vendor_folder="Canada Post")]
        docs += [inv("up1", "UPS", 9.0, "2026-10-01", vendor_folder="UPS"),
                 dict(inv("gone", "UPS", 9.0, "2026-10-01", vendor_folder="UPS"), removed_at="2026-10-02T00:00:00Z")]
        txns = [purchase("p1", "PARKWHIZ 1234", invoice_id="pw0"),
                purchase("p2", "PARKWHIZ 5678", invoice_id="pw1", status="possible", note="vendor similarity 0.00; amount"),
                purchase("p3", "PARKWHIZ 999", invoice_id="pw2", status="possible", note="vendor similarity 0.85"),
                purchase("c1", "CPC / SCP 1", invoice_id="cp1"),
                purchase("c2", "CPC / SCP 2", invoice_id="cp2", method="manual"),
                purchase("c3", "CPC / SCP 3", invoice_id="up1"),                 # once to UPS: minority
                purchase("m1", "CPC / SCP 4", status="missing", invoice_id=None, method=None),
                purchase("m2", "CPC / SCP 5", status="missing", invoice_id=None, method=None),
                purchase("m3", "PARKWHIZ 3", status="missing", invoice_id=None, method=None),
                dict(purchase("x1", "CPC / SCP 6", invoice_id="cp3", status="matched"), type="credit")]
        txns[3]["also_invoice_ids"] = ["cp4"]
        return docs, txns

    def test_catalogue(self):
        docs, txns = self.fixture()
        rows = {r["folder_key"]: r for r in admin.catalogue(docs, txns)}
        self.assertEqual(set(rows), {"parkwhiz,", "canada post", "ups"})
        pw = rows["parkwhiz,"]
        self.assertEqual((pw["folder"], pw["docs"], pw["matched"], pw["possible"], pw["possible_zero_sim"],
                          pw["unmatched_docs"], pw["missing_by_prefix"], pw["attention"], pw["filing_gap"]),
                         ("Parkwhiz,", 4, 1, 2, 1, 1, 1, 2, False))
        self.assertEqual(pw["top_prefixes"], [{"prefix": "parkwhiz", "count": 1}])
        self.assertEqual(pw["spellings"], [{"text": "ParkWhiz Inc", "count": 4}])
        cp = rows["canada post"]
        self.assertEqual((cp["folder"], cp["docs"], cp["first_month"], cp["last_month"], cp["spelling_count"]),
                         ("Canada Post", 4, "2026 09", "2026 11", 2))
        self.assertEqual(cp["spellings"], [{"text": "Canada Post", "count": 3}, {"text": "CANADA POST CORP", "count": 1}])
        # cp3 is linked by a credit only, cp4 as an also document: neither is unmatched
        self.assertEqual((cp["matched"], cp["unmatched_docs"], cp["missing_by_prefix"]), (2, 0, 2))
        self.assertEqual(cp["top_prefixes"], [{"prefix": "cpc scp", "count": 2}])
        self.assertEqual((cp["attention"], cp["filing_gap"]), (0, False))
        self.assertEqual(rows["ups"]["missing_by_prefix"], 0)              # the minority folder of "cpc scp"
        self.assertEqual(rows["ups"]["docs"], 1)                           # the removed document is left out
        self.assertIsNone(cp["vendor"])

    def test_filing_gap(self):
        docs = [inv("d1", "Walmart", 1.0, "2026-10-01", vendor_folder="Walmart")]
        txns = [purchase("w0", "WALMART.COM 1", invoice_id="d1")]
        txns += [purchase(f"w{i}", "WALMART.COM 1", status="missing", invoice_id=None, method=None) for i in range(1, 6)]
        row = admin.catalogue(docs, txns)[0]
        self.assertEqual((row["missing_by_prefix"], row["unmatched_docs"], row["filing_gap"]), (5, 0, True))

    def test_majority_tie_goes_to_the_lowest_folder(self):
        docs = [inv("a", "B", 1.0, "2026-10-01", vendor_folder="Bravo"), inv("b", "A", 1.0, "2026-10-01", vendor_folder="Alpha")]
        txns = [purchase("1", "SHOP ONE", invoice_id="a"), purchase("2", "SHOP ONE", invoice_id="b"),
                purchase("3", "SHOP ONE", status="missing", invoice_id=None, method=None)]
        rows = {r["folder_key"]: r for r in admin.catalogue(docs, txns)}
        self.assertEqual((rows["alpha"]["missing_by_prefix"], rows["bravo"]["missing_by_prefix"]), (1, 0))

    def test_catalogue_page(self):
        docs, txns = self.fixture()
        vendor = self.store.create_vendor({"name": "Canada Post"})
        self.store.add_vendor_folder({"vendor_id": vendor["id"], "folder": "Canada Post"})
        reg = admin.RegistryIndex(self.store.list_vendor_registry())
        rows = admin.catalogue(docs, txns, reg)
        self.assertEqual(next(r for r in rows if r["folder_key"] == "canada post")["vendor"],
                         {"id": vendor["id"], "name": "Canada Post", "kind": "supplier"})
        page = admin.catalogue_page(rows)
        self.assertEqual([r["folder_key"] for r in page["rows"]], ["parkwhiz,", "canada post", "ups"])
        self.assertEqual((page["total"], page["page"], page["pages"], page["per"]), (3, 1, 1, 50))
        self.assertEqual([r["folder_key"] for r in admin.catalogue_page(rows, sort="volume")["rows"]],
                         ["canada post", "parkwhiz,", "ups"])
        self.assertEqual([r["folder_key"] for r in admin.catalogue_page(rows, sort="name")["rows"]],
                         ["canada post", "parkwhiz,", "ups"])
        self.assertEqual([r["folder_key"] for r in admin.catalogue_page(rows, q="corp")["rows"]], ["canada post"])
        self.assertEqual([r["folder_key"] for r in admin.catalogue_page(rows, q="CPC SCP")["rows"]], ["canada post", "ups"])
        self.assertEqual([r["folder_key"] for r in admin.catalogue_page(rows, registered="yes")["rows"]], ["canada post"])
        self.assertEqual(admin.catalogue_page(rows, registered="no")["total"], 2)
        small = admin.catalogue_page(rows, per=0, page=99)
        self.assertEqual((small["per"], small["pages"], small["page"], len(small["rows"])), (1, 3, 3, 1))
        self.assertEqual(admin.catalogue_page(rows, per=1000)["per"], 200)
        self.assertEqual(admin.catalogue_page([], page=-4)["page"], 1)

    def test_folder_detail_and_names(self):
        docs, txns = self.fixture()
        docs.append(inv("sq1", "SQ *COFFEE", 3.0, "2026-10-01", vendor_folder="Canada Post"))
        txns.append(purchase("s1", "SQ *COFFEE BAR", invoice_id="sq1"))
        vendor = self.store.create_vendor({"name": "Canada Post"})
        self.store.add_vendor_pattern({"vendor_id": vendor["id"], "pattern": "cpc scp"})
        reg = admin.RegistryIndex(self.store.list_vendor_registry())
        self.assertIsNone(admin.folder_detail("Nowhere", docs, txns, reg))
        d = admin.folder_detail("CANADA  post", docs, txns, reg)
        self.assertEqual((d["folder"], d["docs"], d["vendor"]), ("Canada Post", 5, None))
        prefixes = {p["prefix"]: p for p in d["prefixes"]}
        cpc = prefixes["cpc scp"]
        self.assertEqual((cpc["first_word"], cpc["matched"], cpc["missing"], cpc["other_folders"], cpc["problem"]),
                         ("cpc", 2, 2, 1, None))
        self.assertEqual(cpc["owner"], {"id": vendor["id"], "name": "Canada Post"})
        self.assertIsNone(prefixes["sq coffee"]["problem"])
        self.assertEqual(d["prefixes"][0]["prefix"], "cpc scp")
        spellings = {s["text"]: s for s in d["spellings"]}
        self.assertEqual(spellings["CANADA POST CORP"]["pattern"], "canada post corp")
        self.assertIsNone(spellings["Canada Post"]["problem"])
        self.assertIsNone(spellings["Canada Post"]["owner"])
        self.assertEqual(d["documents_unmatched"], [])                     # every document is linked
        self.assertEqual(admin.folder_names(docs), ["Canada Post", "Parkwhiz,", "UPS"])
        nothing = admin.folder_detail("Canada Post", docs, txns, None)
        self.assertIsNone(nothing["prefixes"][0]["owner"])

    def test_folder_detail_problem_for_a_processor_prefix(self):
        docs = [inv("d1", "Square", 1.0, "2026-10-01", vendor_folder="Coffee")]
        txns = [purchase("1", "SQ", invoice_id="d1"), purchase("2", "PAYPAL", invoice_id=None, status="missing")]
        d = admin.folder_detail("Coffee", docs, txns)
        self.assertIn("at least 3", d["prefixes"][0]["problem"])
        docs.append(inv("d2", "Paypal", 1.0, "2026-10-01", vendor_folder="Coffee"))
        txns[1].update(invoice_id="d2", invoice_status="matched")
        d = admin.folder_detail("Coffee", docs, txns)
        self.assertIn("payment processor", next(p for p in d["prefixes"] if p["prefix"] == "paypal")["problem"])
        self.assertEqual(d["documents_unmatched"], [])

    def registry(self):
        st = self.store
        cp = st.create_vendor({"name": "Canada Post"})
        ups = st.create_vendor({"name": "UPS"})
        old = st.create_vendor({"name": "Old Co", "active": False})
        st.add_vendor_folder({"vendor_id": cp["id"], "folder": "Canada Post"})
        st.add_vendor_folder({"vendor_id": old["id"], "folder": "Old Folder"})
        st.add_vendor_pattern({"vendor_id": cp["id"], "pattern": "cpc"})
        st.add_vendor_pattern({"vendor_id": ups["id"], "pattern": "cpc scp"})
        st.add_vendor_pattern({"vendor_id": cp["id"], "pattern": "cpc scp special", "match_type": "exact"})
        st.add_vendor_pattern({"vendor_id": ups["id"], "pattern": "ups canada", "status": "off"})
        st.add_vendor_pattern({"vendor_id": old["id"], "pattern": "old shop"})
        st.add_vendor_pattern({"vendor_id": ups["id"], "pattern": "united parcel service", "kind": "invoice_vendor",
                               "match_type": "exact"})
        return cp, ups, old, admin.RegistryIndex(st.list_vendor_registry())

    def test_resolve_order(self):
        cp, ups, old, reg = self.registry()
        self.assertEqual(reg.resolve("CPC SCP SPECIAL")[0]["name"], "Canada Post")          # exact first
        self.assertEqual(reg.resolve("CPC SCP SPECIAL 2")[0]["name"], "Canada Post")       # the number is no word
        self.assertEqual(reg.resolve("CPC SCP SPECIAL OFFER")[:2], (ups, "pattern"))       # then the longest prefix
        self.assertEqual(reg.resolve("CPC / SCP 12345")[2], "cpc scp")
        self.assertEqual(reg.resolve("CPC MAIL")[0]["name"], "Canada Post")
        self.assertEqual(reg.resolve("UPS CANADA", folder="canada  POST")[:2], (cp, "folder"))   # off pattern skipped
        self.assertEqual(reg.resolve("OLD SHOP 1"), (None, None, None))                     # inactive vendor
        self.assertEqual(reg.resolve("X", folder="Old Folder"), (None, None, None))
        self.assertEqual(reg.resolve("X", invoice_vendor="United Parcel Service")[:2], (ups, "invoice_vendor"))
        self.assertEqual(reg.resolve("X", folder="Nowhere", invoice_vendor="Nobody"), (None, None, None))
        self.assertEqual(reg.pattern_owner("descriptor", "cpc scp")["id"], ups["id"])
        self.assertEqual(reg.folder_owner("canada post")["id"], cp["id"])
        nested = reg.nested()
        self.assertEqual([v["name"] for v in nested], ["Canada Post", "Old Co", "UPS"])
        self.assertEqual([p["pattern"] for p in nested[0]["patterns"]], ["cpc", "cpc scp special"])

    def test_coding_for_and_suggestions(self):
        cp, ups, old, _ = self.registry()
        self.store.set_vendor_coding(cp["id"], "", self.uc["Shipping & Logistics"]["id"], "ap@x")
        self.store.set_vendor_coding(cp["id"], "Hair", self.uc["Marketing"]["id"], "ap@x")
        amazon = self.store.create_vendor({"name": "Amazon", "code_each": True})
        self.store.add_vendor_pattern({"vendor_id": amazon["id"], "pattern": "amzn mktp"})
        self.store.set_vendor_coding(amazon["id"], "", self.uc["Other"]["id"], "ap@x")
        stripe = self.store.create_vendor({"name": "Stripe", "kind": "processor"})
        self.store.add_vendor_pattern({"vendor_id": stripe["id"], "pattern": "stripe"})
        self.store.set_vendor_coding(stripe["id"], "", self.uc["Other"]["id"], "ap@x")
        reg = admin.RegistryIndex(self.store.list_vendor_registry())
        self.assertEqual(reg.coding_for(cp["id"], "HAIR"), self.uc["Marketing"]["id"])
        self.assertEqual(reg.coding_for(cp["id"], "Beauty"), self.uc["Shipping & Logistics"]["id"])
        use_cases, active = self.store.list_use_cases(), self.store.list_cost_centers()
        hair = {"last4": "4431", "group_name": "Hair", "default_cost_center": "Salon - STC"}
        beauty = {"last4": "1610", "group_name": "Beauty", "default_cost_center": "Marketing"}     # retired default
        row = purchase("t", "CPC MAIL 1", status="missing", invoice_id=None)
        row["invoice"] = None
        s = admin.suggest_coding(row, hair, reg, use_cases, active)
        self.assertEqual(s, {"usage": "Marketing", "cost_center": "Salon - STC", "vendor": "Canada Post",
                             "source": "Canada Post · Hair default (by pattern 'cpc')"})
        s = admin.suggest_coding(row, beauty, reg, use_cases, active)
        self.assertEqual((s["usage"], s["cost_center"]), ("Shipping & Logistics", None))
        self.assertIn("any group default", s["source"])
        self.assertIsNone(admin.suggest_coding(dict(row, cost_center="US"), hair, reg, use_cases, active)["cost_center"])
        by_folder = dict(purchase("f", "SOMETHING", invoice_id="i"), invoice={"vendor_folder": "Canada Post", "vendor": "x"})
        self.assertIn("(by folder)", admin.suggest_coding(by_folder, None, reg, use_cases, active)["source"])
        self.assertIsNone(admin.suggest_coding(dict(row, usage="Travel"), hair, reg, use_cases, active))
        self.assertIsNone(admin.suggest_coding(dict(row, type="credit"), hair, reg, use_cases, active))
        self.assertIsNone(admin.suggest_coding(dict(row, description="AMZN Mktp CA*1"), hair, reg, use_cases, active))
        self.assertIsNone(admin.suggest_coding(dict(row, description="STRIPE 1"), hair, reg, use_cases, active))
        self.assertIsNone(admin.suggest_coding(dict(row, description="NOBODY"), hair, reg, use_cases, active))
        self.assertIsNone(admin.suggest_coding(row, hair, None, use_cases, active))
        self.store.update_use_case(self.uc["Marketing"]["id"], {"active": False})
        self.assertIsNone(admin.suggest_coding(row, hair, reg, self.store.list_use_cases(), active))
        # use_case_list() rows (no "active") never suggest anything
        brief = [{k: u[k] for k in ("id", "name", "group_name", "description")} for u in use_cases]
        self.assertIsNone(admin.suggest_coding(row, beauty, reg, brief, active))
        resolves = admin.vendor_resolves(reg, [], [row, dict(row, id="t2", usage="x", invoice_status="matched")])
        self.assertEqual(resolves[cp["id"]], {"purchases": 2, "uncoded": 1, "missing": 1})
        self.assertEqual(resolves[ups["id"]], {"purchases": 0, "uncoded": 0, "missing": 0})

    def history(self, description, folder_ids, n, method="rule"):
        return [purchase(f"{description}-{i}", f"{description} {i}", invoice_id=folder_ids[i % len(folder_ids)], method=method)
                for i in range(n)]

    def test_alias_proposals(self):
        docs = [inv(f"cp{i}", "Canada Post", 1.0, "2026-10-01", vendor_folder="Canada Post") for i in range(6)]
        docs += [inv(f"ac{i}", "Air Canada", 1.0, "2026-10-01", vendor_folder="Air Canada") for i in range(6)]
        docs += [inv(f"sq{i}", "Coffee", 1.0, "2026-10-01", vendor_folder="Coffee Bar") for i in range(6)]
        docs += [inv(f"tw{i}", "Two", 1.0, "2026-10-01", vendor_folder="Two Shop") for i in range(2)]
        txns = self.history("CPC SCP", ["cp0", "cp1", "cp2"], 3)
        txns += self.history("AIR CANADA", ["ac0", "ac1"], 2) + self.history("AIR CA", ["ac2", "ac3"], 2, method="manual")
        txns += self.history("SQ COFFEE", ["sq0", "sq1", "sq2"], 3) + self.history("SQ BAR", ["sq3"], 1)
        txns += self.history("TWOSHOP X", ["tw0", "tw1"], 2)
        txns += [purchase("miss", "CPC SCP 99", status="missing", invoice_id=None, method=None)]
        txns += [purchase("ai", "CPC SCP 98", invoice_id="cp3", method="ai")]                  # not link history
        reg = admin.RegistryIndex({})
        ids = [p["id"] for p in admin.proposals(docs, txns, reg, self.store.list_use_cases(), [])]
        self.assertIn("alias:canada post:cpc scp", ids)                     # 3 links
        self.assertIn("alias:coffee bar:sq coffee", ids)
        self.assertNotIn("alias:two shop:twoshop x", ids)                   # 2 links
        self.assertNotIn("alias:air canada:air", ids)                       # first word under 4 letters
        self.assertNotIn("alias:coffee bar:sq", ids)                        # processor first word
        cp = next(p for p in admin.proposals(docs, txns, reg, [], []) if p["id"] == "alias:canada post:cpc scp")
        self.assertEqual(cp["evidence"], {"folder": "Canada Post", "pattern": "cpc scp", "links": 3})
        self.assertEqual(cp["effect"], {"purchases": 5, "missing": 1, "uncoded": 5})
        self.assertEqual(cp["accept"]["path"], "/api/admin/vendors")
        self.assertEqual(cp["accept"]["body"], {"name": "Canada Post", "folders": ["Canada Post"],
                                                "patterns": [{"pattern": "cpc scp"}], "source": "proposal"})
        self.assertEqual(cp["text"], 'Purchases starting "cpc scp" were linked to Canada Post 3 times, never to another folder.')
        # a registered folder: the pattern is added to its vendor; a held pattern is not proposed again
        vendor = self.store.create_vendor({"name": "Canada Post"})
        self.store.add_vendor_folder({"vendor_id": vendor["id"], "folder": "Canada Post"})
        reg = admin.RegistryIndex(self.store.list_vendor_registry())
        cp = next(p for p in admin.proposals(docs, txns, reg, [], []) if p["id"] == "alias:canada post:cpc scp")
        self.assertEqual((cp["accept"]["method"], cp["accept"]["path"]), ("POST", f"/api/admin/vendors/{vendor['id']}/patterns"))
        self.assertEqual(cp["accept"]["body"]["source"], "proposal")
        self.store.add_vendor_pattern({"vendor_id": vendor["id"], "pattern": "cpc scp"})
        reg = admin.RegistryIndex(self.store.list_vendor_registry())
        self.assertNotIn("alias:canada post:cpc scp", [p["id"] for p in admin.proposals(docs, txns, reg, [], [])])
        dismissed = admin.proposals(docs, txns, reg, [], [], dismissed={"alias:coffee bar:sq coffee"})
        self.assertNotIn("alias:coffee bar:sq coffee", [p["id"] for p in dismissed])

    def test_first_word_alias(self):
        docs = [inv(f"st{i}", "Staples", 1.0, "2026-10-01", vendor_folder="Staples") for i in range(4)]
        txns = self.history("STAPLES STORE", ["st0"], 2) + self.history("STAPLES ONLINE", ["st1"], 1)
        props = {p["id"]: p for p in admin.proposals(docs, txns, admin.RegistryIndex({}), [], [])}
        self.assertIn("alias:staples:staples", props)
        self.assertEqual(props["alias:staples:staples"]["evidence"]["links"], 3)
        self.assertNotIn("alias:staples:staples store", props)              # 2 links only

    def test_folders_proposals(self):
        docs = [inv(f"u{i}", "UPS", 1.0, "2026-10-01", vendor_folder="UPS") for i in range(3)]
        docs += [inv(f"uc{i}", "UPS", 1.0, "2026-10-01", vendor_folder="UPS Canada") for i in range(3)]
        docs += [inv("one", "UPS", 1.0, "2026-10-01", vendor_folder="Misfiled")]
        txns = self.history("UPS BILLING", ["u0", "u1", "uc0", "uc1"], 4) + self.history("UPS BILLING Z", ["one"], 1)
        props = {p["id"]: p for p in admin.proposals(docs, txns, admin.RegistryIndex({}), [], [])}
        p = props["folders:ups|ups canada"]
        self.assertEqual(p["evidence"]["links"], {"UPS": 2, "UPS Canada": 2})
        self.assertEqual(p["evidence"]["prefixes"], ["ups billing"])
        self.assertEqual(p["accept"]["body"], {"name": "UPS", "folders": ["UPS", "UPS Canada"], "source": "proposal"})
        vendor = self.store.create_vendor({"name": "UPS"})
        self.store.add_vendor_folder({"vendor_id": vendor["id"], "folder": "UPS"})
        reg = admin.RegistryIndex(self.store.list_vendor_registry())
        p = next(p for p in admin.proposals(docs, txns, reg, [], []) if p["id"] == "folders:ups|ups canada")
        self.assertEqual((p["accept"]["path"], p["accept"]["body"]), (f"/api/admin/vendors/{vendor['id']}/folders",
                                                                      {"folders": ["UPS Canada"]}))
        self.store.add_vendor_folder({"vendor_id": vendor["id"], "folder": "UPS Canada"})
        reg = admin.RegistryIndex(self.store.list_vendor_registry())
        self.assertFalse([p for p in admin.proposals(docs, txns, reg, [], []) if p["type"] == "folders"])
        other = self.store.create_vendor({"name": "UPS CA"})
        self.store.delete_vendor_folder(next(f["id"] for f in self.store.list_vendor_registry()["folders"]
                                             if f["folder_key"] == "ups canada"))
        self.store.add_vendor_folder({"vendor_id": other["id"], "folder": "UPS Canada"})
        reg = admin.RegistryIndex(self.store.list_vendor_registry())
        self.assertFalse([p for p in admin.proposals(docs, txns, reg, [], []) if p["type"] == "folders"])

    def test_coding_proposals(self):
        vendor = self.store.create_vendor({"name": "Canada Post"})
        self.store.add_vendor_pattern({"vendor_id": vendor["id"], "pattern": "cpc scp"})
        cards = [{"last4": "4431", "group_name": "Hair"}, {"last4": "1610", "group_name": "Beauty"}]
        txns = [purchase(f"h{i}", f"CPC SCP {i}", status="missing", invoice_id=None, usage="shipping & logistics",
                         updated_by="george@x") for i in range(3)]
        txns += [purchase("h3", "CPC SCP 3", status="missing", invoice_id=None, usage="Marketing", updated_by="george@x")]
        txns += [purchase("b0", "CPC SCP 9", card="1610", status="missing", invoice_id=None, usage="Travel", updated_by="x@y")]
        txns += [purchase(f"j{i}", f"CPC SCP 1{i}", card="1610", status="missing", invoice_id=None, usage="Travel",
                          updated_by=who) for i, who in enumerate(("scheduler", "job:remind", "schema:coding_split", ""))]
        reg = admin.RegistryIndex(self.store.list_vendor_registry())
        props = {p["id"]: p for p in admin.proposals([], txns, reg, self.store.list_use_cases(), cards)}
        ship = self.uc["Shipping & Logistics"]["id"]
        p = props[f"coding:{vendor['id']}:hair:{ship}"]                     # 3 of 4 = 75%
        self.assertEqual(p["accept"], {"method": "PUT", "path": f"/api/admin/vendors/{vendor['id']}/coding",
                                       "body": {"group_name": "Hair", "use_case_id": ship}})
        self.assertEqual((p["evidence"]["rows"], p["evidence"]["top"], p["evidence"]["use_case"]), (4, 3, "Shipping & Logistics"))
        self.assertEqual(p["effect"], {"purchases": 4, "missing": 4, "uncoded": 0})
        self.assertNotIn("beauty", " ".join(props))                         # 1 coded by a person: too few
        self.assertNotIn(f"coding:{vendor['id']}::{ship}", props)          # 3 of 5 overall: under 75%
        self.store.set_vendor_coding(vendor["id"], "", ship, "ap@x")          # already the vendor's default
        reg = admin.RegistryIndex(self.store.list_vendor_registry())
        self.assertFalse([p for p in admin.proposals([], txns, reg, self.store.list_use_cases(), cards) if p["type"] == "coding"])

    def test_proposal_order(self):
        docs = [inv(f"cp{i}", "Canada Post", 1.0, "2026-10-01", vendor_folder="Canada Post") for i in range(9)]
        docs += [inv(f"u{i}", "UPS", 1.0, "2026-10-01", vendor_folder="UPS") for i in range(4)]
        docs += [inv(f"uc{i}", "UPS", 1.0, "2026-10-01", vendor_folder="UPS Canada") for i in range(4)]
        txns = (self.history("CPC SCP", ["cp0", "cp1", "cp2"], 3) + self.history("ESSO GAS", ["cp3", "cp4"], 5)
                + self.history("UPS BILLING", ["u0", "uc0"], 4))
        types = [p["type"] for p in admin.proposals(docs, txns, admin.RegistryIndex({}), [], [])]
        self.assertEqual(types, sorted(types, key=["alias", "folders", "coding"].index))
        ids = [p["id"] for p in admin.proposals(docs, txns, admin.RegistryIndex({}), [], [])]
        self.assertEqual(ids[:2], ["alias:canada post:esso gas", "alias:canada post:cpc scp"])
        self.assertTrue(admin.proposals(docs, txns, None, [], []))            # no registry yet: still proposed

    def test_likely_duplicate_people(self):
        people = [{"id": "1", "name": "NEW FRONTIER GLOBAL LIMITED"}, {"id": "2", "name": "New Frontier Global"},
                  {"id": "3", "name": "Amy"}, {"id": "4", "name": "Amy Chen"}, {"id": "5", "name": "The Frontier"},
                  {"id": "6", "name": "Bo Li Inc"}, {"id": "7", "name": "bo li"}]
        self.assertEqual(admin.likely_duplicate_people(people), [["7", "6"], ["2", "1"]])
        self.assertEqual(admin.person_tokens("The NEW Frontier, Global Ltd."), ("new", "frontier", "global"))


if __name__ == "__main__":
    unittest.main()
