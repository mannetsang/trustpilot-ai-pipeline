"""Cards with a group (Hair, Beauty), the number printed on them, the primary card a supplementary card is
linked under, the name on the card, and the holder as the Person of their purchases; the export of the
purchases still missing a receipt, per group (Nov 2026)."""
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from openpyxl import load_workbook  # noqa: E402
from test_pipeline import ApiTestCase, inv, txn  # noqa: E402
import main  # noqa: E402
import matching  # noqa: E402


class CardsTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.store.upsert_cards([{"last4": "4431", "label": "Card ending 4431", "holder_name": ""},
                                 {"last4": "7436", "label": "Card ending 7436", "holder_name": ""},
                                 {"last4": "9456", "label": "Card ending 9456 - Linda", "holder_name": "Linda"},
                                 {"last4": "3914", "label": "Card ending 3914", "holder_name": "NEW FRONTIER GLOBAL LIMITED"}])

    def save(self, **body):
        return self.client.post("/api/cards", json=body)

    def card(self, last4, cards=None):
        return next(c for c in (cards or self.client.get("/api/cards").get_json()["cards"]) if c["last4"] == last4)


class CardFieldTests(CardsTestCase):
    def test_holder_group_number_and_label(self):
        r = self.save(last4="4431", holder_name="George", group_name="  Hair ", card_number="4449")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c = self.card("4431", r.get_json()["cards"])
        self.assertEqual((c["holder_name"], c["group_name"], c["card_number"], c["label"]),
                         ("George", "Hair", "4449", "Card ending 4431 - George"))
        self.assertEqual([p["name"] for p in r.get_json()["people"]], ["George"])
        self.assertEqual(c["person_id"], r.get_json()["people"][0]["id"])
        self.assertIn("4449", c["receipts_show"])            # the number on the card, always listed

    def test_a_label_someone_wrote_is_kept_when_the_holder_changes(self):
        self.save(last4="9456", label="Linda's Visa (Henry Li)")
        r = self.save(last4="9456", holder_name="Linda Li", label="Linda's Visa (Henry Li)")
        self.assertEqual(self.card("9456", r.get_json()["cards"])["label"], "Linda's Visa (Henry Li)")

    def test_the_same_holder_on_two_cards_is_one_person_and_saving_again_adds_nobody(self):
        self.save(last4="4431", holder_name="George")
        r = self.save(last4="7436", holder_name="george")
        people = r.get_json()["people"]
        self.assertEqual(len(people), 1)
        self.assertEqual(self.card("7436", r.get_json()["cards"])["person_id"], people[0]["id"])
        # A card saved with its holder not yet a person links them; saving it again adds nobody.
        r = self.save(last4="9456", holder_name="Linda", group_name="Beauty")
        linda = next(p for p in r.get_json()["people"] if p["name"] == "Linda")
        self.assertEqual(self.card("9456", r.get_json()["cards"])["person_id"], linda["id"])
        r = self.save(last4="9456", holder_name="Linda", group_name="Beauty")
        self.assertEqual(len(r.get_json()["people"]), 2)
        self.assertEqual(self.card("9456", r.get_json()["cards"])["label"], "Card ending 9456 - Linda")

    def test_a_supplementary_card_shows_its_primary_and_the_primary_its_cards(self):
        self.save(last4="4431", holder_name="George", card_number="4449")
        r = self.save(last4="7436", holder_name="Linda", primary_last4="4431", name_on_card="")
        cards = r.get_json()["cards"]
        sup, primary = self.card("7436", cards), self.card("4431", cards)
        self.assertEqual((sup["primary_last4"], sup["primary_holder"], sup["primary_card_number"]), ("4431", "George", "4449"))
        self.assertEqual(primary["supplementary"], ["7436"])

    def test_what_is_refused(self):
        self.assertEqual(self.save(last4="4431", card_number="449").status_code, 400)
        self.assertEqual(self.save(last4="4431", card_number="7436").status_code, 400)       # a card of its own
        self.assertEqual(self.save(last4="4431", primary_last4="4431").status_code, 400)
        self.assertEqual(self.save(last4="4431", primary_last4="1234").status_code, 400)     # not on the dashboard
        self.assertEqual(self.save(last4="7436", primary_last4="4431").status_code, 200)
        self.assertEqual(self.save(last4="9456", primary_last4="7436").status_code, 400)     # one level only
        self.assertEqual(self.save(last4="4431", primary_last4="9456").status_code, 400)     # a primary has cards
        self.assertEqual(self.save(last4="4431", card_number="").status_code, 200)
        self.assertEqual(self.card("4431")["card_number"], "")


class CardNumberMatchingTests(CardsTestCase):
    def test_a_receipt_showing_the_number_on_the_card_is_that_cards(self):
        self.save(last4="4431", card_number="4449")
        matching.refresh_card_aliases(self.store, force=True)
        self.assertFalse(matching.cards_differ("4449", "4431"))
        self.assertTrue(matching.cards_differ("4449", "9456"))
        self.store.upsert_invoices([dict(inv("cp", "Canada Post", 719.97, "2026-10-03", month_folder="2026 10",
                                             card="4449"))])
        self.store.insert_transactions([txn("t", "2026-10-03", "CANADA POST", 719.97, card="4431")])
        matching.match_month(self.store, None, 2026, 10, use_ai=False, log=lambda *a: None)
        self.assertEqual(self.store.get_transaction("t")["invoice_id"], "cp")


class ExportTests(CardsTestCase):
    def setUp(self):
        super().setUp()
        self.save(last4="4431", holder_name="George", group_name="Hair", card_number="4449")
        self.save(last4="7436", holder_name="Linda", group_name="Beauty", primary_last4="4431")
        self.save(last4="9456", group_name="Beauty")
        self.store.insert_transactions([
            txn("h1", "2026-10-03", "UPS CANADA BILLING", 120.00, card="4431"),
            txn("h2", "2026-09-03", "=HYPERLINK(\"http://x\")", 10.00, card="4431"),
            txn("b1", "2026-10-05", "SEPHORA", 45.50, card="7436"),
            txn("b2", "2026-10-06", "AMAZON", 30.00, card="9456", currency="USD"),
            txn("b3", "2026-10-07", "AMAZON", 12.00, card="9456", currency="USD", status="matched"),
            txn("c1", "2026-10-08", "REFUND", -5.00, card="9456", currency="USD", type_="credit"),
            txn("n1", "2026-10-09", "FOO", 9.99, card="3914"),
        ])
        person = self.store.create_person("Amy")[0]
        self.store.update_transaction("b1", {"person_id": person["id"]})

    def export(self, query=""):
        r = self.client.get("/api/export/missing" + query)
        self.assertEqual(r.status_code, 200, r.data[:300])
        wb = load_workbook(io.BytesIO(r.data))
        sheet = wb["Missing receipts"]
        rows = [dict(zip([c.value for c in sheet[1]], [c.value for c in row])) for row in sheet.iter_rows(min_row=2)]
        return r, rows, [[c.value for c in row] for row in wb["Totals"].iter_rows(min_row=2)]

    def test_a_groups_missing_purchases_in_every_month(self):
        r, rows, totals = self.export("?group=beauty")
        self.assertEqual([x["Card (account)"] for x in rows], ["7436", "9456"])
        sup = rows[0]
        self.assertEqual((sup["Group"], sup["Linked under"], sup["Card holder"], sup["Person"]),
                         ("Beauty", "4431 (George)", "Linda", "Amy"))
        self.assertEqual(rows[1]["Person"], "Linda")                       # nobody assigned: the holder
        self.assertEqual(totals, [["Beauty", "CAD", 1, 45.5], ["Beauty", "USD", 1, 30.0]])   # never added up
        self.assertIn("missing-receipts-beauty-all-months.xlsx", r.headers["Content-Disposition"])
        self.assertEqual(r.headers["X-Rows"], "2")

    def test_one_month_one_card_and_the_cards_in_no_group(self):
        _, rows, _ = self.export("?group=Hair&year=2026&month=10")
        self.assertEqual([x["Description"] for x in rows], ["UPS CANADA BILLING"])
        self.assertEqual(rows[0]["Card number"], "4449")
        _, rows, _ = self.export("?card=4431")
        self.assertEqual(len(rows), 2)
        _, rows, _ = self.export("?group=-")
        self.assertEqual([x["Card (account)"] for x in rows], ["3914"])

    def test_text_is_never_a_formula(self):
        r = self.client.get("/api/export/missing?card=4431")
        sheet = load_workbook(io.BytesIO(r.data))["Missing receipts"]
        cell = next(row[7] for row in sheet.iter_rows(min_row=2) if str(row[7].value).startswith("="))
        self.assertEqual(cell.data_type, "s")

    def test_a_bad_month_is_refused(self):
        self.assertEqual(self.client.get("/api/export/missing?year=2026").status_code, 400)
        self.assertEqual(self.client.get("/api/export/missing?year=2026&month=13").status_code, 400)


if __name__ == "__main__":
    unittest.main()
