"""Assigning a transaction to a person, and adding people from the dashboard."""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import MEMBER, ApiTestCase  # noqa: E402
import main  # noqa: E402
from store import MemoryStore, person_named  # noqa: E402


class PeopleApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.assertIn(self.upload().status_code, (200, 201))
        self.ids_ = self.ids()

    def add(self, name, expect=201, **extra):
        r = self.client.post("/api/people", json=dict({"name": name}, **extra))
        self.assertEqual(r.status_code, expect, r.get_json())
        return r.get_json()

    def test_a_person_is_added_listed_and_assigned(self):
        bo = self.add("Bo Li")["person"]
        self.assertEqual(self.client.get("/api/people").get_json()["people"], [bo])
        row = self.patch(self.ids_["anth"], {"person_id": bo["id"]})["transaction"]
        self.assertEqual(row["person_id"], bo["id"])
        data = self.transactions()
        self.assertEqual(data["people"], [bo])
        anth = next(t for t in data["transactions"] if t["id"] == self.ids_["anth"])
        self.assertEqual((anth["person_id"], anth["updated_by"]), (bo["id"], "anonymous"))
        self.assertEqual(self.client.get("/api/months").get_json()["people"], [bo])

    def test_the_same_name_gives_back_the_person_already_there(self):
        first = self.add("Linda Chan")["person"]
        again = self.add("  linda   CHAN ", expect=200)
        self.assertEqual((again["person"], again["created"]), (first, False))
        self.assertEqual(len(self.client.get("/api/people").get_json()["people"]), 1)

    def test_names_and_emails_are_checked(self):
        self.assertIn("required", self.add("   ", expect=400)["error"])
        self.assertIn("longer than", self.add("x" * 101, expect=400)["error"])
        self.assertIn("not an e-mail", self.add("Amy", expect=400, email="amy at home")["error"])
        amy = self.add("Amy", email="Amy@SuperHairpieces.com")["person"]
        self.assertEqual(amy["email"], "amy@superhairpieces.com")
        r = self.client.post("/api/people", data="nope", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_an_unknown_person_is_refused_and_an_empty_one_clears(self):
        bo = self.add("Bo Li")["person"]
        self.assertIn("does not exist", self.patch(self.ids_["anth"], {"person_id": "no-such-id"}, expect=400)["error"])
        self.patch(self.ids_["anth"], {"person_id": bo["id"]})
        self.assertIsNone(self.patch(self.ids_["anth"], {"person_id": ""})["transaction"]["person_id"])
        self.assertIsNone(self.store.get_transaction(self.ids_["anth"])["person_id"])

    def test_assigning_leaves_the_other_fields_alone(self):
        bo = self.add("Bo Li")["person"]
        self.patch(self.ids_["anth"], {"cost_center": "IT & Software", "usage": "Team plan"})
        self.patch(self.ids_["anth"], {"person_id": bo["id"]})
        t = self.store.get_transaction(self.ids_["anth"])
        self.assertEqual((t["cost_center"], t["usage"], t["person_id"]), ("IT & Software", "Team plan", bo["id"]))

    def test_a_member_assigns_only_on_cards_they_own_but_may_add_people(self):
        with mock.patch.object(main, "current_user", return_value=dict(MEMBER)):
            bo = self.add("Bo Li")["person"]
            r = self.client.patch(f"/api/transactions/{self.ids_['anth']}", json={"person_id": bo["id"]},
                                  headers={"X-Requested-With": "XMLHttpRequest"})
            self.assertEqual(r.status_code, 403)
        self.assertIsNone(self.store.get_transaction(self.ids_["anth"]).get("person_id"))

    def test_only_accounts_payable_sets_an_email(self):
        # /admin's People tab offers "Make owner" from a person's e-mail: a member must not plant one.
        with mock.patch.object(main, "current_user", return_value=dict(MEMBER)):
            r = self.add("George Newhire", expect=403, email=MEMBER["email"])
            self.assertEqual(r["error"], "email: only accounts payable can set a person's e-mail")
            self.add("George Newhire")                                   # a name alone is fine
        self.assertEqual([p["email"] for p in self.store.list_people()], [None])
        self.assertEqual(self.add("Amy", email="amy@superhairpieces.com")["person"]["email"], "amy@superhairpieces.com")

    def test_signing_in_is_required(self):
        with mock.patch.object(main, "current_user", return_value=None):
            self.assertEqual(self.client.get("/api/people").status_code, 401)
            self.assertEqual(self.client.post("/api/people", json={"name": "X"}).status_code, 401)

    def test_removing_an_upload_keeps_lines_assigned_to_someone(self):
        bo = self.add("Bo Li")["person"]
        self.patch(self.ids_["anth"], {"person_id": bo["id"]})
        statement_id = self.store.get_transaction(self.ids_["anth"])["statement_id"]
        out = self.store.delete_statement(statement_id)
        self.assertGreaterEqual(out["transactions_kept"], 1)
        self.assertIsNotNone(self.store.get_transaction(self.ids_["anth"]))


class PersonNamedTests(unittest.TestCase):
    def test_case_and_spaces_aside(self):
        people = [{"id": "1", "name": "Bo  Li"}, {"id": "2", "name": "Linda"}]
        self.assertEqual(person_named(people, " bo li ")["id"], "1")
        self.assertIsNone(person_named(people, "Bo"))

    def test_memory_store_people(self):
        store = MemoryStore()
        a, created = store.create_person("Zed")
        b, _ = store.create_person("amy")
        self.assertTrue(created)
        self.assertEqual([p["name"] for p in store.list_people()], ["amy", "Zed"])
        self.assertEqual(store.get_person(a["id"]), a)
        self.assertIsNone(store.get_person("nope"))
        self.assertEqual(store.create_person("ZED"), (a, False))


if __name__ == "__main__":
    unittest.main()
