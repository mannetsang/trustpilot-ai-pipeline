"""Tests for the AP credit-card reconciliation service.

Standard-library unittest only. Nothing here touches the network: storage is
store.MemoryStore, and the Gemini and Drive clients are replaced by small fakes
that answer from canned JSON and an in-memory folder tree.

Run from ap-reconciliation/:  python -m unittest discover -s tests -v
"""

import datetime as dt
import io
import json
import os
import re
import sys
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD_RUN = os.path.abspath(os.path.join(HERE, "..", "cloud_run"))
if CLOUD_RUN not in sys.path:
    sys.path.insert(0, CLOUD_RUN)

# main.py reads its configuration when it is imported, so the test environment
# has to be in place first: in-memory store, no sign-in, no AI, no Drive copy of
# uploads, a fixed dashboard URL for deep links and dummy Chat webhooks.
os.environ.update({
    "STORE": "memory",
    "AUTH_DISABLED": "1",
    "AI_DISABLED": "1",
    "STATEMENTS_FOLDER_ID": "",
    "DASHBOARD_URL": "https://ap.test",
    "API_TOKEN": "test-token",
    "CARD_WEBHOOKS_JSON": '{"1610": "https://chat.test/1610", "default": "https://chat.test/default"}',
})

import openpyxl  # noqa: E402

import chat  # noqa: E402
import invoices  # noqa: E402
import main  # noqa: E402
import matching  # noqa: E402
import statements  # noqa: E402
from drive import EXPORT_AS_PDF, FOLDER, SHORTCUT  # noqa: E402
from store import MemoryStore  # noqa: E402

main.app.testing = True

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GDOC = "application/vnd.google-apps.document"


def quiet(*args, **kwargs):
    """Swallows the pipeline's progress prints."""


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeGemini:
    """Stand-in for gemini.Gemini: same part builders, canned JSON instead of a model call.

    Answers come from `handler(parts, model)` when given, otherwise from the
    `responses` queue in order, otherwise {}. Every call is recorded."""

    def __init__(self, responses=None, handler=None):
        self.extract_model = "fake-extract"
        self.match_model = "fake-match"
        self.responses = list(responses or [])
        self.handler = handler
        self.calls = []
        self._lock = threading.Lock()

    @staticmethod
    def text(value):
        return {"text": value}

    @staticmethod
    def inline(data, mime_type):
        return {"inlineData": {"mimeType": mime_type, "data": data}}

    def generate_json(self, parts, model=None, temperature=0.0, timeout=300):
        with self._lock:
            self.calls.append({"parts": parts, "model": model})
        if self.handler is not None:
            return self.handler(parts, model)
        if self.responses:
            return self.responses.pop(0)
        return {}

    def prompt_text(self, call_index=0):
        return "\n".join(p.get("text", "") for p in self.calls[call_index]["parts"])


class FakeDrive:
    """Stand-in for drive.Drive over an in-memory folder tree.

    tree: {folder id: [child dicts]} with the fields Drive.list_children returns.
    Shortcuts are resolved the way the real client does it (the target's record
    under the shortcut's name) and a dangling shortcut is skipped. `files_by_id`
    holds shortcut targets that live outside the tree."""

    def __init__(self, tree, files_by_id=None, contents=None):
        self.tree = tree
        self.files_by_id = files_by_id or {}
        self.contents = contents or {}
        self.downloads = []
        self._lock = threading.Lock()

    def list_children(self, folder_id):
        out = []
        for f in self.tree.get(folder_id, []):
            f = dict(f)
            if f.get("mimeType") == SHORTCUT:
                target = self.files_by_id.get((f.get("shortcutDetails") or {}).get("targetId"))
                if not target:
                    continue
                resolved = dict(target)
                resolved["name"] = f.get("name") or resolved.get("name")
                f = resolved
            out.append(f)
        return out

    def download(self, file):
        with self._lock:
            self.downloads.append(file["id"])
        mime = file.get("mimeType", "")
        if mime in EXPORT_AS_PDF:
            return b"%PDF-1.4 exported " + file["id"].encode(), "application/pdf"
        return self.contents.get(file["id"], b"%PDF-1.4 " + file["id"].encode()), mime

    def find_child_folder(self, parent_id, name):
        for f in self.tree.get(parent_id, []):
            if f.get("mimeType") == FOLDER and f.get("name") == name:
                return f["id"]
        return None


# ---------------------------------------------------------------------------
# Builders: RBC export, Drive tree, invoice facts, store rows
# ---------------------------------------------------------------------------
RBC_HEADER = [
    "Account type", "Account issuer", "Account hierarchy", "Account", "Cardholder first name",
    "Cardholder last name", "Employee ID", "Employee first name", "Employee last name", "Company unit",
    "Period start date", "Period end date", "Posting date", "Transaction date", "Transaction type", "Supplier",
    "Supplier address", "Supplier city", "Supplier state", "Supplier country", "Supplier postal",
    "Merchant group", "Merchant category", "Source currency", "Source amount", "Billing currency", "Amount",
    "Tax amount", "Tax receipt checked", "Amount (Tax excl)", "Approval status", "Transaction status",
    "Disputed status", "Reimbursed by ACH", "Expense report name", "Expense report number", "Extract date",
    "Extract issuer", "Extract type", "Extract status",
]
PERIOD = ("2026-09-04", "2026-10-03")


def iso_dt(value):
    return dt.datetime.strptime(value, "%Y-%m-%d") if value else None


def rbc_row(account, first, last, txn_date, post_date, txn_type, supplier, amount, currency="CAD",
            source_amount=None, source_currency=None, city="", country="CA", group="", period=PERIOD):
    """One line of the export, with the cell types the real file has (datetimes, floats)."""
    row = {h: None for h in RBC_HEADER}
    row.update({
        "Account type": "Purchasing Card", "Account issuer": "RBC", "Account hierarchy": "Superhairpieces",
        "Account": f"****-****-****-{account}", "Cardholder first name": first, "Cardholder last name": last,
        "Employee first name": first, "Employee last name": last, "Company unit": "SHP",
        "Period start date": iso_dt(period[0]), "Period end date": iso_dt(period[1]),
        "Posting date": iso_dt(post_date), "Transaction date": iso_dt(txn_date), "Transaction type": txn_type,
        "Supplier": supplier, "Supplier city": city, "Supplier country": country, "Merchant group": group,
        "Merchant category": group, "Source currency": source_currency or currency,
        "Source amount": source_amount if source_amount is not None else amount,
        "Billing currency": currency, "Amount": amount, "Approval status": "Approved",
        "Transaction status": "Posted", "Extract type": "Transaction Export",
    })
    return row


def sample_rows():
    """Three cards; a purchase, a credit voucher, a fee, a payment without supplier,
    a USD purchase, two identical rows and one line in the next month."""
    return [
        rbc_row("1610", "Yin", "Wei", "2026-09-04", "2026-09-05", "Purchase", "Anthropic* Claude Team", 150.00,
                city="San Francisco", country="US", group="Computer Software"),
        rbc_row("1610", "Yin", "Wei", "2026-09-10", "2026-09-11", "Purchase", "Amzn Mktp Ca", 45.10,
                city="Toronto", group="Retail"),
        rbc_row("1610", "Yin", "Wei", "2026-09-12", "2026-09-13", "Credit Voucher", "Amzn Mktp Ca", -45.10,
                city="Toronto", group="Retail"),
        rbc_row("1610", "Yin", "Wei", "2026-09-19", "2026-09-19", "Fee", "Annual Card Fee", 120.00),
        rbc_row("1610", "Yin", "Wei", "2026-09-28", "2026-09-28", "Payment", "", -1000.00),
        rbc_row("4894", "Jane", "Doe", "2026-09-12", "2026-09-14", "Purchase", "Www.Retellai.Com", 96.12,
                source_amount=70.40, source_currency="USD", city="Redwood City", country="US",
                group="Computer Software"),
        rbc_row("4894", "Jane", "Doe", "2026-09-15", "2026-09-16", "Purchase", "Tim Hortons #123", 5.25,
                city="Toronto", group="Restaurants"),
        rbc_row("4894", "Jane", "Doe", "2026-09-15", "2026-09-16", "Purchase", "Tim Hortons #123", 5.25,
                city="Toronto", group="Restaurants"),
        rbc_row("2301", "Sam", "Lee", "2026-09-20", "2026-09-21", "Purchase", "Costco Wholesale", 88.00,
                city="Mississauga", group="Wholesale Clubs"),
        rbc_row("2301", "Sam", "Lee", "2026-10-01", "2026-10-02", "Purchase", "Shopify", 39.00,
                city="Ottawa", group="Computer Software"),
    ]


def build_xlsx(rows, header=RBC_HEADER):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Transaction Export"
    ws.append(list(header))
    for r in rows:
        ws.append([r.get(h) for h in header])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def by_description(transactions, description):
    return [t for t in transactions if t["description"] == description]


def one(transactions, description):
    found = by_description(transactions, description)
    assert len(found) == 1, f"{description}: {len(found)} rows"
    return found[0]


def folder(fid, name):
    return {"id": fid, "name": name, "mimeType": FOLDER, "modifiedTime": "2026-09-01T00:00:00Z"}


def file_(fid, name, mime="application/pdf", modified="2026-09-05T10:00:00Z", size=1000):
    return {"id": fid, "name": name, "mimeType": mime, "modifiedTime": modified, "size": str(size),
            "webViewLink": f"https://drive.google.com/file/d/{fid}/view"}


def invoice_tree():
    """Invoice/2026 09/{Anthropic/a.pdf, Anthropic/<shortcut>, Google/<gdoc>, loose.jpg, notes.docx, order.txt}
       Invoice/2026 10/Costco/{sub/b.pdf, huge.pdf}   Invoice/Misc/x.pdf"""
    tree = {
        "root": [folder("m09", "2026 09"), folder("m10", "2026 10"), folder("misc", "Misc")],
        "m09": [folder("v_anth", "Anthropic"), folder("v_goog", "Google"), file_("f_loose", "loose.jpg", "image/jpeg"),
                file_("f_docx", "notes.docx", DOCX), file_("f_txt", "order.txt", "text/plain")],
        "v_anth": [file_("f_a", "a.pdf"),
                   {"id": "sc_1", "name": "shared receipt.pdf", "mimeType": SHORTCUT,
                    "shortcutDetails": {"targetId": "f_target"}},
                   {"id": "sc_2", "name": "dangling shortcut", "mimeType": SHORTCUT, "shortcutDetails": {}}],
        "v_goog": [file_("f_gdoc", "Google Workspace invoice", GDOC, size=0)],
        "m10": [folder("v_costco", "Costco")],
        "v_costco": [folder("v_sub", "sub"), file_("f_huge", "huge.pdf", size=20 * 1024 * 1024)],
        "v_sub": [file_("f_b", "b.pdf")],
        "misc": [file_("f_x", "x.pdf")],
    }
    files_by_id = {"f_target": file_("f_target", "receipt from shared drive.pdf")}
    return tree, files_by_id


INVOICE_FACTS = {
    "a.pdf": {"is_invoice": True, "vendor": "Anthropic", "invoice_number": "INV-0001", "invoice_date": "2026-09-03",
              "total": 150.0, "currency": "CAD", "card_last4": "1610", "payment_method": "Visa ending 1610",
              "summary": "Claude Team seats"},
    "shared receipt.pdf": {"is_invoice": True, "vendor": "Anthropic", "invoice_number": "INV-0002",
                           "invoice_date": "2026-09-20", "total": 20.0, "currency": "USD", "card_last4": None,
                           "payment_method": None, "summary": "API credits"},
    "Google Workspace invoice": {"is_invoice": True, "vendor": "Google", "invoice_number": "G-1",
                                 "invoice_date": "2026-09-01", "total": 84.0, "currency": "CAD",
                                 "card_last4": None, "payment_method": None, "summary": "Workspace seats"},
    "loose.jpg": {"is_invoice": True, "vendor": "Tim Hortons", "invoice_number": None, "invoice_date": "2026-09-15",
                  "total": 5.25, "currency": "CAD", "card_last4": None, "payment_method": "Visa",
                  "summary": "coffee"},
    "order.txt": {"is_invoice": True, "vendor": "Shopify", "invoice_number": "123", "invoice_date": "2026-10-01",
                  "total": 39.0, "currency": "CAD", "card_last4": None, "payment_method": None,
                  "summary": "plan"},
    "b.pdf": {"is_invoice": True, "vendor": "Costco Wholesale", "invoice_number": "C-9", "invoice_date": "2026-10-02",
              "total": 88.0, "currency": "CAD", "card_last4": "2301", "payment_method": None,
              "summary": "supplies"},
    "x.pdf": {"is_invoice": False, "vendor": "", "invoice_number": None, "invoice_date": None, "total": None,
              "currency": None, "card_last4": None, "payment_method": None, "summary": "a contract"},
}


def file_name_in(parts):
    text = "\n".join(p.get("text", "") for p in parts)
    m = re.search(r'file name "([^"]+)"', text)
    return m.group(1) if m else ""


def facts_handler(parts, model):
    """Gemini stand-in for invoice extraction: facts keyed by the file name in the prompt."""
    name = file_name_in(parts)
    return dict(INVOICE_FACTS.get(name, {"is_invoice": False, "summary": f"unknown file {name}"}))


def txn(tid, date, description, amount, card="1610", type_="purchase", status="missing", currency="CAD",
        source_amount=None, source_currency="", invoice_id=None, method=None):
    """A stored transaction row (every column the schema has)."""
    return {
        "id": tid, "statement_id": "st_1", "card_last4": card, "holder_name": "", "txn_date": date,
        "post_date": date, "year": int(date[:4]), "month": int(date[5:7]), "description": description,
        "supplier": description, "city": "", "country": "", "merchant_category": "", "type": type_,
        "amount": amount, "currency": currency, "source_amount": source_amount, "source_currency": source_currency,
        "invoice_status": status, "invoice_id": invoice_id, "match_confidence": None, "match_method": method,
        "match_note": None, "cost_center": None, "usage": None, "note": None, "updated_by": None,
        "updated_at": None, "created_at": "2026-10-01T00:00:00Z", "raw": {},
    }


def inv(iid, vendor, total, date, month_folder="2026 09", vendor_folder=None, currency="CAD", card=None,
        file_name=None, is_invoice=True):
    """A stored invoice row; the vendor folder defaults to the vendor name."""
    vendor_folder = vendor if vendor_folder is None else vendor_folder
    return {
        "id": iid, "file_name": file_name or f"{iid}.pdf", "mime_type": "application/pdf",
        "web_view_link": f"https://drive.google.com/file/d/{iid}/view",
        "folder_path": f"/{month_folder}/{vendor_folder}".rstrip("/"), "month_folder": month_folder,
        "vendor_folder": vendor_folder, "modified_time": "2026-09-05T10:00:00Z", "size": 1000, "vendor": vendor,
        "invoice_number": None, "invoice_date": date, "total": total, "currency": currency, "card_last4": card,
        "payment_method": None, "summary": "", "is_invoice": is_invoice, "extracted_at": "2026-10-01T00:00:00Z",
        "extraction_error": None,
    }


# ---------------------------------------------------------------------------
# statements: RBC export
# ---------------------------------------------------------------------------
class RbcExportTests(unittest.TestCase):
    def setUp(self):
        self.data = build_xlsx(sample_rows())
        self.result = statements.parse_statement("Transaction Export.xlsx", self.data)
        self.txns = self.result["transactions"]

    def test_header_detection_maps_the_rbc_columns(self):
        rows = statements.read_xlsx(self.data)
        index, mapping = statements.detect_header(rows)
        self.assertEqual(index, 0)
        self.assertEqual(set(mapping.values()), {
            "card", "holder_first", "holder_last", "period_start", "period_end", "post_date", "txn_date", "type",
            "description", "city", "country", "merchant_category", "source_currency", "source_amount", "currency",
            "amount"})

    def test_cards_period_and_counts(self):
        self.assertEqual(self.result["format"], "table")
        self.assertEqual(self.result["warnings"], [])
        self.assertEqual(len(self.txns), 10)
        self.assertEqual(self.result["cards"], {
            "1610": {"holder_name": "Yin Wei", "count": 5},
            "4894": {"holder_name": "Jane Doe", "count": 3},
            "2301": {"holder_name": "Sam Lee", "count": 2},
        })
        self.assertEqual((self.result["period_start"], self.result["period_end"]), PERIOD)

    def test_types_follow_the_transaction_type_column(self):
        self.assertEqual(one(self.txns, "Anthropic* Claude Team")["type"], "purchase")
        self.assertEqual(one(self.txns, "Annual Card Fee")["type"], "fee")
        credit = [t for t in by_description(self.txns, "Amzn Mktp Ca") if t["amount"] < 0]
        self.assertEqual(len(credit), 1)
        self.assertEqual(credit[0]["type"], "credit")
        self.assertEqual(credit[0]["amount"], -45.10)

    def test_payment_without_supplier_is_kept(self):
        payment = one(self.txns, "Payment")
        self.assertEqual(payment["type"], "payment")
        self.assertEqual(payment["amount"], -1000.0)
        self.assertEqual(payment["supplier"], "")
        self.assertEqual(payment["card_last4"], "1610")
        self.assertEqual(payment["holder_name"], "Yin Wei")

    def test_foreign_currency_fields(self):
        usd = one(self.txns, "Www.Retellai.Com")
        self.assertEqual((usd["amount"], usd["currency"]), (96.12, "CAD"))
        self.assertEqual((usd["source_amount"], usd["source_currency"]), (70.40, "USD"))
        self.assertEqual((usd["city"], usd["country"], usd["merchant_category"]),
                         ("Redwood City", "US", "Computer Software"))
        cad = one(self.txns, "Anthropic* Claude Team")
        self.assertIsNone(cad["source_amount"])
        self.assertEqual(cad["source_currency"], "")
        self.assertEqual(cad["post_date"], "2026-09-05")
        self.assertEqual(cad["raw"]["Supplier"], "Anthropic* Claude Team")
        self.assertEqual(cad["raw"]["Account"], "****-****-****-1610")

    def test_year_and_month_come_from_the_transaction_date(self):
        self.assertEqual((one(self.txns, "Shopify")["year"], one(self.txns, "Shopify")["month"]), (2026, 10))
        self.assertEqual((one(self.txns, "Costco Wholesale")["year"], one(self.txns, "Costco Wholesale")["month"]),
                         (2026, 9))
        self.assertTrue(all(t["txn_date"] and t["id"].startswith("t_") for t in self.txns))

    def test_identical_rows_get_distinct_deterministic_ids(self):
        twins = by_description(self.txns, "Tim Hortons #123")
        self.assertEqual(len(twins), 2)
        self.assertNotEqual(twins[0]["id"], twins[1]["id"])
        self.assertEqual(len({t["id"] for t in self.txns}), 10)
        self.assertEqual(twins[0]["id"], statements.transaction_id(twins[0], 0))
        self.assertEqual(twins[1]["id"], statements.transaction_id(twins[1], 1))

    def test_reparsing_gives_identical_ids(self):
        again = statements.parse_statement("Transaction Export.xlsx", self.data)["transactions"]
        self.assertEqual([t["id"] for t in again], [t["id"] for t in self.txns])
        # Same lines in another file name or order: same ids too, that is what makes uploads idempotent.
        reordered = build_xlsx(list(reversed(sample_rows())))
        again = statements.parse_statement("other name.xlsx", reordered)["transactions"]
        self.assertEqual(sorted(t["id"] for t in again), sorted(t["id"] for t in self.txns))

    def test_blank_type_cell_falls_back_to_the_amount_sign(self):
        rows = [rbc_row("1610", "Yin", "Wei", "2026-09-04", "2026-09-05", "", "Anthropic* Claude Team", 150.00),
                rbc_row("1610", "Yin", "Wei", "2026-09-06", "2026-09-07", "", "Amzn Mktp Ca", -45.10),
                rbc_row("1610", "Yin", "Wei", "2026-09-28", "2026-09-28", "Payment", "", -1000.00)]
        txns = statements.parse_statement("export.xlsx", build_xlsx(rows))["transactions"]
        self.assertEqual([t["type"] for t in txns], ["purchase", "credit", "payment"])


# ---------------------------------------------------------------------------
# statements: generic CSV, cell parsing, Gemini paths
# ---------------------------------------------------------------------------
GENERIC_CSV = (
    "Date,Description,Amount\n"
    "15/09/2026,AMZN MKTP CA,-45.10\n"
    "03/09/2026,ANTHROPIC* CLAUDE TEAM,-150.00\n"
    "22/09/2026,TIM HORTONS #123,-5.25\n"
    "30/09/2026,WWW.RETELLAI.COM,-96.12\n"
    "28/09/2026,PAYMENT - THANK YOU,500.00\n"
)


class GenericCsvTests(unittest.TestCase):
    def test_negative_charges_flipped_and_day_first_dates(self):
        result = statements.parse_statement("visa_4894_sep_2026.csv", GENERIC_CSV.encode())
        txns = result["transactions"]
        self.assertEqual(len(txns), 5)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("signs flipped", result["warnings"][0])
        self.assertEqual(one(txns, "AMZN MKTP CA")["amount"], 45.10)
        self.assertEqual(one(txns, "AMZN MKTP CA")["type"], "purchase")
        self.assertEqual(one(txns, "PAYMENT - THANK YOU")["amount"], -500.0)
        self.assertEqual(one(txns, "PAYMENT - THANK YOU")["type"], "credit")
        # 03/09 is the 3rd of September because other rows (15/09, 22/09, 30/09) prove the file is day-first.
        self.assertEqual(one(txns, "ANTHROPIC* CLAUDE TEAM")["txn_date"], "2026-09-03")
        self.assertEqual(one(txns, "AMZN MKTP CA")["txn_date"], "2026-09-15")
        self.assertEqual((result["period_start"], result["period_end"]), ("2026-09-03", "2026-09-30"))
        self.assertEqual(one(txns, "AMZN MKTP CA")["currency"], "CAD")
        self.assertEqual(one(txns, "AMZN MKTP CA")["post_date"], "")

    def test_card_comes_from_the_file_name_when_there_is_no_card_column(self):
        result = statements.parse_statement("visa_4894_sep_2026.csv", GENERIC_CSV.encode())
        self.assertEqual({t["card_last4"] for t in result["transactions"]}, {"4894"})
        self.assertEqual(list(result["cards"]), ["4894"])
        result = statements.parse_statement("statement.csv", GENERIC_CSV.encode())
        self.assertEqual({t["card_last4"] for t in result["transactions"]}, {"0000"})

    def test_debit_credit_columns(self):
        csv_text = ("Transaction Date,Details,Debit,Credit\n"
                    "2026-09-04,ANTHROPIC,150.00,\n"
                    "2026-09-28,PAYMENT RECEIVED,,1000.00\n")
        txns = statements.parse_statement("c.csv", csv_text.encode())["transactions"]
        self.assertEqual([(t["description"], t["amount"], t["type"]) for t in txns],
                         [("ANTHROPIC", 150.0, "purchase"), ("PAYMENT RECEIVED", -1000.0, "credit")])

    def test_unrecognised_columns_need_gemini(self):
        with self.assertRaises(statements.StatementError) as ctx:
            statements.parse_statement("odd.csv", b"Foo,Bar\nx,1\n")
        self.assertIn("Could not recognise", str(ctx.exception))
        fake = FakeGemini(responses=[{"cards": [], "currency": "CAD", "transactions": [
            {"card_last4": "1610", "txn_date": "2026-09-04", "description": "FOO", "amount": 1.0, "type": "purchase"}]}])
        result = statements.parse_statement("odd.csv", b"Foo,Bar\nx,1\n", gemini=fake)
        self.assertEqual(result["format"], "gemini")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["model"], "fake-extract")
        self.assertIn("The statement, as CSV text:", fake.prompt_text())
        self.assertIn("Foo,Bar", fake.prompt_text())

    def test_header_row_but_no_readable_lines(self):
        with self.assertRaises(statements.StatementError) as ctx:
            statements.parse_statement("empty.csv", b"Date,Description,Amount\nnot a date,x,oops\n")
        self.assertIn("no transactions", str(ctx.exception))


class CellParsingTests(unittest.TestCase):
    def test_parse_amount(self):
        cases = {"(12.34)": -12.34, "12.34 CR": -12.34, "$1,234.56": 1234.56, "12,34": 12.34, "1,234": 1234.0,
                 "-45.10": -45.10, "CAD 96.12": 96.12, "  7 ": 7.0, "abc": None, "": None, None: None, "-": None}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(statements.parse_amount(raw), expected)
        self.assertEqual(statements.parse_amount(12), 12.0)
        self.assertEqual(statements.parse_amount(12.345), 12.35)

    def test_parse_date(self):
        cases = {"Sep 4, 2026": "2026-09-04", "September 4, 2026": "2026-09-04", "4 Sep 2026": "2026-09-04",
                 "04-Sep-26": "2026-09-04", "2026-09-04T00:00:00": "2026-09-04", "2026-09-04 13:45:00": "2026-09-04",
                 "2026/09/04": "2026-09-04", "20260904": "2026-09-04", "09/04/2026": "2026-09-04",
                 "15/09/2026": "2026-09-15", "9/4/26": "2026-09-04", "13/13/2026": "", "31/04/2026": "",
                 "garbage": "", "": "", None: ""}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(statements.parse_date(raw), expected)
        self.assertEqual(statements.parse_date("04/09/2026", dayfirst=True), "2026-09-04")
        self.assertEqual(statements.parse_date("04/09/2026"), "2026-04-09")
        self.assertEqual(statements.parse_date(dt.datetime(2026, 9, 4, 10, 30)), "2026-09-04")
        self.assertEqual(statements.parse_date(dt.date(2026, 9, 4)), "2026-09-04")

    def test_last4_and_types(self):
        self.assertEqual(statements.last4("****-****-****-1610"), "1610")
        self.assertEqual(statements.last4("Card ending in 4894"), "4894")
        self.assertEqual(statements.last4("16"), "")
        self.assertEqual(statements.last4(None), "")
        self.assertEqual(statements.normalise_type("Purchase", 10), "purchase")
        self.assertEqual(statements.normalise_type("Credit Voucher", -10), "credit")
        self.assertEqual(statements.normalise_type("Payment", -10), "payment")
        self.assertEqual(statements.normalise_type("Pre-authorized payment", -10), "payment")
        self.assertEqual(statements.normalise_type("Fee", 10), "fee")
        self.assertEqual(statements.normalise_type("Cash Advance", 10), "purchase")
        self.assertEqual(statements.normalise_type("", -10), "credit")
        self.assertEqual(statements.normalise_type("", 10), "purchase")


class GeminiStatementTests(unittest.TestCase):
    GEMINI_STATEMENT = {
        "cards": [{"last4": "4894", "holder_name": "Jane Doe"}],
        "period_start": "2026-09-01", "period_end": "2026-09-30", "currency": "CAD",
        "transactions": [
            {"card_last4": None, "txn_date": "2026-09-04", "post_date": None, "description": "ANTHROPIC* CLAUDE TEAM",
             "amount": 150, "currency": "CAD", "source_amount": None, "source_currency": None, "type": "purchase"},
            {"card_last4": "", "txn_date": "2026-09-12", "post_date": "2026-09-14", "description": "WWW.RETELLAI.COM",
             "amount": 96.12, "currency": "CAD", "source_amount": 70.40, "source_currency": "USD",
             "type": "purchase", "city": "Redwood City", "country": "US"},
            {"txn_date": "2026-09-28", "description": "PAYMENT - THANK YOU", "amount": -500, "type": "payment"},
            {"txn_date": "", "description": "TOTAL", "amount": 999},
        ],
    }

    def test_unsupported_extension(self):
        with self.assertRaises(statements.StatementError) as ctx:
            statements.parse_statement("statement.docx", b"PK\x03\x04", gemini=FakeGemini())
        self.assertIn("Unsupported file type '.docx'", str(ctx.exception))
        with self.assertRaises(statements.StatementError):
            statements.parse_statement("statement.xls", b"\xd0\xcf\x11\xe0")

    def test_pdf_without_gemini(self):
        with self.assertRaises(statements.StatementError) as ctx:
            statements.parse_statement("statement.pdf", b"%PDF-1.4")
        self.assertIn("Gemini", str(ctx.exception))

    def test_pdf_with_gemini_infers_the_single_card(self):
        fake = FakeGemini(responses=[dict(self.GEMINI_STATEMENT)])
        result = statements.parse_statement("statement.pdf", b"%PDF-1.4 fake", gemini=fake)
        self.assertEqual(result["format"], "gemini")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["model"], "fake-extract")
        parts = fake.calls[0]["parts"]
        self.assertEqual(parts[0]["inlineData"]["mimeType"], "application/pdf")
        self.assertEqual(parts[0]["inlineData"]["data"], b"%PDF-1.4 fake")
        self.assertIn("Extract EVERY transaction", parts[1]["text"])
        txns = result["transactions"]
        self.assertEqual(len(txns), 3)   # the undated TOTAL line is dropped
        self.assertEqual({t["card_last4"] for t in txns}, {"4894"})
        self.assertEqual({t["holder_name"] for t in txns}, {"Jane Doe"})
        self.assertEqual(result["cards"], {"4894": {"holder_name": "Jane Doe", "count": 3}})
        self.assertEqual((result["period_start"], result["period_end"]), ("2026-09-01", "2026-09-30"))
        self.assertEqual(len(result["warnings"]), 1)
        usd = one(txns, "WWW.RETELLAI.COM")
        self.assertEqual((usd["amount"], usd["currency"], usd["source_amount"], usd["source_currency"]),
                         (96.12, "CAD", 70.40, "USD"))
        self.assertEqual((usd["post_date"], usd["city"], usd["country"]), ("2026-09-14", "Redwood City", "US"))
        payment = one(txns, "PAYMENT - THANK YOU")
        self.assertEqual((payment["type"], payment["amount"], payment["currency"]), ("payment", -500.0, "CAD"))
        self.assertEqual((payment["year"], payment["month"]), (2026, 9))
        self.assertEqual(len({t["id"] for t in txns}), 3)

    def test_image_by_mime_type_and_several_cards(self):
        data = dict(self.GEMINI_STATEMENT, cards=[{"last4": "4894", "holder_name": "Jane Doe"},
                                                   {"last4": "1610", "holder_name": "Yin Wei"}])
        fake = FakeGemini(responses=[data])
        result = statements.parse_statement("photo", b"\x89PNG", mime_type="image/png", gemini=fake)
        self.assertEqual(fake.calls[0]["parts"][0]["inlineData"]["mimeType"], "image/png")
        # With two cards on the statement a line without a card number cannot be attributed.
        self.assertEqual({t["card_last4"] for t in result["transactions"]}, {"0000"})

    def test_gemini_finding_nothing_is_an_error(self):
        with self.assertRaises(statements.StatementError):
            statements.parse_statement("statement.pdf", b"%PDF", gemini=FakeGemini(responses=[{"transactions": []}]))


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------
class VendorAndAmountTests(unittest.TestCase):
    def test_vendor_similarity(self):
        sim = matching.vendor_similarity
        self.assertGreaterEqual(sim({"description": "Anthropic* Claude Team"}, {"vendor": "Anthropic"}), 0.5)
        self.assertEqual(sim({"description": "Amzn Mktp Ca"}, {"vendor": "Amazon"}), 1.0)
        self.assertGreaterEqual(sim({"description": "Www.Retellai.Com"},
                                    {"vendor": "", "vendor_folder": "Retell AI", "file_name": "receipt.pdf"}), 0.5)
        self.assertGreaterEqual(sim({"description": "Google *Cloud 1234"}, {"vendor": "Google Cloud"}), 0.9)
        self.assertEqual(sim({"description": "Costco Wholesale"}, {"vendor": "Anthropic"}), 0.0)
        self.assertEqual(sim({"description": "Www.Com.Ca"}, {"vendor": "Anthropic"}), 0.0)   # only stop words
        self.assertEqual(sim({"description": ""}, {"vendor": "Anthropic"}), 0.0)
        self.assertEqual(matching.tokens("Amzn Mktp Ca Order 123 receipt.pdf"), {"amazon"})

    def test_amount_score(self):
        score = matching.amount_score
        billed = {"amount": 96.12, "currency": "CAD", "source_amount": 70.40, "source_currency": "USD"}
        self.assertEqual(score(billed, {"total": 96.12, "currency": "CAD"})[0], 1.0)
        self.assertEqual(score(billed, {"total": 96.11, "currency": "CAD"})[0], 1.0)   # rounding tolerance
        self.assertEqual(score(billed, {"total": 70.40, "currency": "USD"})[0], 1.0)   # foreign currency
        self.assertEqual(score(billed, {"total": 70.40, "currency": "EUR"})[0], 0.0)   # currency mismatch
        self.assertEqual(score(billed, {"total": 70.40, "currency": None})[0], 1.0)    # unknown currency tries both
        self.assertEqual(score({"amount": 96.12, "currency": "CAD"}, {"total": 96.12, "currency": "USD"}),
                         (0.0, "currency differs"))
        self.assertEqual(score(billed, {"total": None}), (0.0, "invoice has no total"))
        self.assertEqual(score({"amount": 100.0, "currency": "CAD"}, {"total": 100.50, "currency": "CAD"})[0], 0.6)
        self.assertEqual(score({"amount": 100.0, "currency": "CAD"}, {"total": 104.0, "currency": "CAD"})[0], 0.3)
        self.assertEqual(score({"amount": 100.0, "currency": "CAD"}, {"total": 120.0, "currency": "CAD"})[0], 0.0)
        self.assertEqual(score({"amount": -45.10, "currency": "CAD"}, {"total": 45.10, "currency": "CAD"})[0], 1.0)

    def test_dates(self):
        self.assertEqual(matching.days_apart({"txn_date": "2026-09-04"}, {"invoice_date": "2026-09-01"}), 3)
        self.assertIsNone(matching.days_apart({"txn_date": "2026-09-04"}, {"invoice_date": None}))
        self.assertIsNone(matching.days_apart({"txn_date": "2026-09-04"}, {"invoice_date": "soon"}))
        self.assertEqual([matching.date_score(d) for d in (None, 0, 3, 7, 14, 35, 36)],
                         [0.3, 1.0, 1.0, 0.8, 0.5, 0.2, 0.0])


class RuleMatchTests(unittest.TestCase):
    def test_exact_amount_and_close_date_is_a_match(self):
        t = txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00)
        i = inv("i1", "Anthropic", 150.00, "2026-09-03")
        out = matching.rule_match([t], [i])
        iid, status, conf, note = out["t1"]
        self.assertEqual((iid, status), ("i1", "matched"))
        self.assertAlmostEqual(conf, 0.95)
        self.assertIn("equals invoice total", note)
        self.assertIn("1 days apart", note)

    def test_close_date_without_vendor_overlap_is_still_a_match(self):
        t = txn("t1", "2026-09-04", "Sq *Corner Cafe", 33.00)
        i = inv("i1", "Dunder Mifflin", 33.00, "2026-09-05")
        self.assertEqual(matching.rule_match([t], [i])["t1"][1], "matched")

    def test_exact_amount_far_date_no_vendor_overlap_is_possible(self):
        t = txn("t1", "2026-09-20", "Costco Wholesale", 88.00)
        i = inv("i1", "Staples", 88.00, "2026-07-01")
        self.assertEqual(matching.rule_match([t], [i])["t1"][1:3], ("possible", 0.6))

    def test_near_amount_needs_vendor_and_date(self):
        t = txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00)
        self.assertEqual(matching.rule_match([t], [inv("i1", "Anthropic", 150.90, "2026-09-03")])["t1"][1:3],
                         ("possible", 0.65))
        self.assertEqual(matching.rule_match([t], [inv("i1", "Dunder Mifflin", 150.90, "2026-09-03")]), {})
        self.assertEqual(matching.rule_match([t], [inv("i1", "Anthropic", 150.90, "2026-08-01")]), {})
        self.assertEqual(matching.rule_match([t], [inv("i1", "Anthropic", 160.00, "2026-09-03")]), {})

    def test_invoice_printed_with_another_card_is_not_matched(self):
        t = txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00, card="1610")
        self.assertEqual(matching.rule_match([t], [inv("i1", "Anthropic", 150.00, "2026-09-03", card="9999")]), {})
        self.assertEqual(matching.rule_match([t], [inv("i1", "Anthropic", 150.00, "2026-09-03", card="1610")])["t1"][1],
                         "matched")

    def test_greedy_best_score_wins(self):
        uber = txn("t_uber", "2026-09-10", "Uber Trip", 50.00)
        lyft = txn("t_lyft", "2026-09-10", "Lyft", 50.00)
        receipt = inv("i1", "Uber", 50.00, "2026-09-10")
        out = matching.rule_match([lyft, uber], [receipt])
        self.assertEqual(list(out), ["t_uber"])
        # Two invoices, two transactions: each invoice used once.
        other = inv("i2", "Lyft", 50.00, "2026-09-10")
        out = matching.rule_match([lyft, uber], [receipt, other])
        self.assertEqual({tid: r[0] for tid, r in out.items()}, {"t_uber": "i1", "t_lyft": "i2"})

    def test_excluded_inputs(self):
        self.assertEqual(matching.rule_match([], [inv("i1", "A", 1.0, "2026-09-01")]), {})
        self.assertEqual(matching.rule_match([txn("t1", "2026-09-04", "A", 1.0)], []), {})


class AiMatchTests(unittest.TestCase):
    def setUp(self):
        self.txns = [txn(f"t{c}", "2026-09-0%d" % (i + 1), f"Vendor {c}", 10.0 * (i + 1)) for i, c in enumerate("ABCDE")]
        self.invs = [inv(f"i{n}", f"Vendor {n}", 10.0 * n, "2026-09-0%d" % n) for n in (1, 2, 3)]

    def test_confidence_thresholds_duplicates_and_unknown_ids(self):
        fake = FakeGemini(responses=[{"matches": [
            {"transaction_id": "tC", "invoice_id": "i1", "confidence": 0.8, "reason": "weaker duplicate of i1"},
            {"transaction_id": "tA", "invoice_id": "i1", "confidence": 0.95, "reason": "vendor amount date agree"},
            {"transaction_id": "tB", "invoice_id": "i2", "confidence": "0.7", "reason": "amount differs by tip"},
            {"transaction_id": "ghost", "invoice_id": "i3", "confidence": 0.99, "reason": "unknown transaction"},
            {"transaction_id": "tD", "invoice_id": "ghost", "confidence": 0.99, "reason": "unknown invoice"},
            {"transaction_id": "tE", "invoice_id": "i3", "confidence": 0.4, "reason": "below threshold"},
            {"transaction_id": "tD", "invoice_id": "i3", "confidence": "high", "reason": "not a number"},
        ]}])
        out = matching.ai_match(fake, self.txns, self.invs)
        self.assertEqual(out, {"tA": ("i1", "matched", 0.95, "AI: vendor amount date agree"),
                               "tB": ("i2", "possible", 0.7, "AI: amount differs by tip")})
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["model"], "fake-match")
        prompt = fake.prompt_text()
        for ident in ("tA", "tE", "i1", "i3", '"Vendor A"'):
            self.assertIn(ident, prompt)
        self.assertEqual(matching.ai_match(fake, self.txns, []), {})
        self.assertEqual(matching.ai_match(fake, [], self.invs), {})
        self.assertEqual(len(fake.calls), 1)

    def test_bare_list_and_exact_thresholds(self):
        fake = FakeGemini(responses=[[{"transaction_id": "tA", "invoice_id": "i1", "confidence": 0.85},
                                      {"transaction_id": "tB", "invoice_id": "i2", "confidence": 0.6},
                                      {"transaction_id": "tC", "invoice_id": "i3", "confidence": 0.59}]])
        out = matching.ai_match(fake, self.txns, self.invs)
        self.assertEqual({k: v[1] for k, v in out.items()}, {"tA": "matched", "tB": "possible"})

    def test_batches_do_not_reoffer_used_invoices(self):
        fake = FakeGemini(responses=[
            {"matches": [{"transaction_id": "tA", "invoice_id": "i1", "confidence": 0.9, "reason": "first batch"}]},
            {"matches": []}, {"matches": []}])
        out = matching.ai_match(fake, self.txns, self.invs, txn_batch=2)
        self.assertEqual(list(out), ["tA"])
        self.assertEqual(len(fake.calls), 3)
        self.assertIn('"i1"', fake.prompt_text(0))
        self.assertNotIn('"i1"', fake.prompt_text(1))
        self.assertIn('"i2"', fake.prompt_text(1))

    def test_judge_failure_is_not_fatal(self):
        def boom(parts, model):
            raise RuntimeError("quota")
        with mock.patch("builtins.print"):
            self.assertEqual(matching.ai_match(FakeGemini(handler=boom), self.txns, self.invs), {})


class MatchMonthTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store.insert_transactions([
            txn("t_anth", "2026-09-04", "Anthropic* Claude Team", 150.00),
            txn("t_amzn", "2026-09-10", "Amzn Mktp Ca", 45.10),
            txn("t_retell", "2026-09-12", "Www.Retellai.Com", 96.12, card="4894", source_amount=70.40,
                source_currency="USD"),
            txn("t_mystery", "2026-09-15", "Sq *Corner Cafe", 33.00),
            txn("t_dup", "2026-09-21", "Costco Wholesale", 88.00, card="2301"),
            txn("t_possible", "2026-09-20", "Google *Cloud", 12.00, status="possible", invoice_id="inv_google",
                method="rule"),
            txn("t_manual", "2026-09-16", "Staples", 20.00, status="matched", invoice_id="inv_staples", method="manual"),
            txn("t_waived", "2026-09-17", "Tim Hortons", 5.00, status="waived", method="manual"),
            txn("t_credit", "2026-09-18", "Amzn Mktp Ca", -45.10, type_="credit", status="not_required"),
            txn("t_payment", "2026-09-28", "Payment", -1000.00, type_="payment", status="not_required"),
            txn("t_aug", "2026-08-30", "Costco Wholesale", 88.00, card="2301", status="matched",
                invoice_id="inv_costco", method="rule"),
        ])
        self.store.upsert_invoices([
            inv("inv_anth", "Anthropic", 150.00, "2026-09-03"),
            inv("inv_amzn", "Amazon.ca", 45.10, "2026-09-10", vendor_folder="Amazon"),
            inv("inv_retell", "Retell AI Inc", 70.40, "2026-09-12", vendor_folder="Retell AI", currency="USD"),
            inv("inv_google", "Google Cloud", 12.00, "2026-09-01"),
            inv("inv_staples", "Staples", 20.00, "2026-09-16"),
            inv("inv_costco", "Costco", 88.00, "2026-08-30", month_folder="2026 08"),
            inv("inv_mystery", "DM Paper Co", 33.20, "2026-09-15", month_folder="2026 10", vendor_folder="Receipts"),
            inv("inv_notinv", "Bank", 88.00, "2026-09-21", is_invoice=False),
            inv("inv_nototal", "Costco", None, "2026-09-21"),
            inv("inv_far", "Elsewhere", 33.00, "2026-01-05", month_folder="2026 01"),
        ])
        self.judge = FakeGemini(responses=[{"matches": [
            {"transaction_id": "t_mystery", "invoice_id": "inv_mystery", "confidence": 0.9, "reason": "same receipt"}]}])

    def status(self, tid):
        t = self.store.get_transaction(tid)
        return (t["invoice_status"], t["invoice_id"], t["match_method"])

    def test_end_to_end(self):
        counts = matching.match_month(self.store, self.judge, 2026, 9, use_ai=True, log=quiet)
        self.assertEqual(counts, {"checked": 6, "matched": 5, "possible": 0, "missing": 1, "invoices_available": 5})
        self.assertEqual(self.status("t_anth"), ("matched", "inv_anth", "rule"))
        self.assertEqual(self.status("t_amzn"), ("matched", "inv_amzn", "rule"))
        self.assertEqual(self.status("t_retell"), ("matched", "inv_retell", "rule"))
        self.assertEqual(self.status("t_possible"), ("matched", "inv_google", "rule"))
        self.assertEqual(self.status("t_mystery"), ("matched", "inv_mystery", "ai"))
        self.assertEqual(self.status("t_dup"), ("missing", None, None))
        anth = self.store.get_transaction("t_anth")
        self.assertAlmostEqual(anth["match_confidence"], 0.95)
        self.assertIn("equals invoice total", anth["match_note"])
        self.assertIsNotNone(anth["updated_at"])
        mystery = self.store.get_transaction("t_mystery")
        self.assertEqual(mystery["match_note"], "AI: same receipt")
        self.assertEqual(mystery["match_confidence"], 0.9)
        # Untouched: manual, waived, credits, payments and other months.
        self.assertEqual(self.status("t_manual"), ("matched", "inv_staples", "manual"))
        self.assertEqual(self.status("t_waived"), ("waived", None, "manual"))
        self.assertEqual(self.status("t_credit"), ("not_required", None, None))
        self.assertEqual(self.status("t_payment"), ("not_required", None, None))
        self.assertEqual(self.status("t_aug"), ("matched", "inv_costco", "rule"))
        for tid in ("t_manual", "t_waived", "t_credit", "t_payment", "t_aug"):
            self.assertIsNone(self.store.get_transaction(tid)["updated_at"], tid)
        # The judge only saw the leftovers: two transactions and the one unused invoice.
        self.assertEqual(len(self.judge.calls), 1)
        prompt = self.judge.prompt_text()
        for ident in ("t_mystery", "t_dup", "inv_mystery"):
            self.assertIn(f'"{ident}"', prompt)
        for ident in ("t_anth", "t_amzn", "t_possible", "inv_anth", "inv_google", "inv_costco", "inv_staples",
                      "inv_notinv", "inv_nototal", "inv_far"):
            self.assertNotIn(f'"{ident}"', prompt)
        # Running again only re-checks the leftover; nothing changes and the judge is not asked again.
        before = {t["id"]: dict(t) for t in self.store.transactions.values()}
        counts = matching.match_month(self.store, self.judge, 2026, 9, use_ai=True, log=quiet)
        self.assertEqual(counts, {"checked": 1, "matched": 0, "possible": 0, "missing": 1, "invoices_available": 0})
        self.assertEqual({t["id"]: dict(t) for t in self.store.transactions.values()}, before)
        self.assertEqual(len(self.judge.calls), 1)

    def test_rules_only(self):
        counts = matching.match_month(self.store, self.judge, 2026, 9, use_ai=False, log=quiet)
        self.assertEqual((counts["matched"], counts["missing"]), (4, 2))
        self.assertEqual(self.judge.calls, [])
        self.assertEqual(self.status("t_mystery"), ("missing", None, None))
        self.assertEqual(self.status("t_dup"), ("missing", None, None))

    def test_without_a_gemini_client_rules_still_run(self):
        counts = matching.match_month(self.store, None, 2026, 9, use_ai=True, log=quiet)
        self.assertEqual((counts["checked"], counts["matched"], counts["missing"]), (6, 4, 2))
        self.assertEqual(self.status("t_anth"), ("matched", "inv_anth", "rule"))

    def test_card_filter(self):
        counts = matching.match_month(self.store, self.judge, 2026, 9, card="4894", log=quiet)
        self.assertEqual((counts["checked"], counts["matched"]), (1, 1))
        self.assertEqual(self.status("t_retell"), ("matched", "inv_retell", "rule"))
        self.assertEqual(self.status("t_anth"), ("missing", None, None))
        self.assertEqual(self.judge.calls, [])

    def test_judge_is_skipped_when_rules_cover_everything(self):
        store = MemoryStore()
        store.insert_transactions([txn("t_anth", "2026-09-04", "Anthropic* Claude Team", 150.00)])
        store.upsert_invoices([inv("inv_anth", "Anthropic", 150.00, "2026-09-03"),
                               inv("inv_other", "Other", 999.00, "2026-09-03")])
        judge = FakeGemini()
        counts = matching.match_month(store, judge, 2026, 9, log=quiet)
        self.assertEqual(counts["matched"], 1)
        self.assertEqual(judge.calls, [])

    def test_better_candidate_takes_the_invoice_from_a_possible(self):
        # The earlier "possible" pairing only rested on the amount; a new line from the same vendor,
        # same amount, a day from the invoice date, wins the invoice and the old one goes back to missing.
        self.store.update_transaction("t_possible", {"description": "Zzz", "supplier": "Zzz"})
        self.store.insert_transactions([txn("t_better", "2026-09-02", "Google *Cloud", 12.00)])
        matching.match_month(self.store, None, 2026, 9, use_ai=False, log=quiet)
        self.assertEqual(self.status("t_better"), ("matched", "inv_google", "rule"))
        self.assertEqual(self.status("t_possible"), ("missing", None, None))
        self.assertIsNone(self.store.get_transaction("t_possible")["match_confidence"])


# ---------------------------------------------------------------------------
# invoices: walk + index
# ---------------------------------------------------------------------------
class WalkTests(unittest.TestCase):
    def setUp(self):
        tree, files_by_id = invoice_tree()
        self.drive = FakeDrive(tree, files_by_id)

    def test_month_folder_parsing(self):
        for name, expected in {"2026 09": "2026 09", "2026-9": "2026 09", "2026_10": "2026 10", "202609": "2026 09",
                               "Misc": None, "2026 13": None, "1999 01": None, "Anthropic": None}.items():
            self.assertEqual(invoices.parse_month_folder(name), expected, name)

    def test_walk_whole_tree(self):
        records = {r["id"]: r for r in invoices.walk(self.drive, "root")}
        self.assertEqual(set(records), {"f_a", "f_target", "f_gdoc", "f_loose", "f_docx", "f_txt", "f_b", "f_huge", "f_x"})
        a = records["f_a"]
        self.assertEqual((a["month_folder"], a["vendor_folder"], a["folder_path"]), ("2026 09", "Anthropic", "/2026 09/Anthropic"))
        self.assertEqual((a["file_name"], a["mime_type"], a["size"]), ("a.pdf", "application/pdf", 1000))
        self.assertEqual(a["modified_time"], "2026-09-05T10:00:00Z")
        self.assertEqual(a["web_view_link"], "https://drive.google.com/file/d/f_a/view")
        self.assertEqual(a["_file"]["id"], "f_a")
        # File directly in the month folder: no vendor folder.
        loose = records["f_loose"]
        self.assertEqual((loose["month_folder"], loose["vendor_folder"], loose["folder_path"]), ("2026 09", "", "/2026 09"))
        # Nested deeper than the vendor folder: the vendor folder is still the first level.
        b = records["f_b"]
        self.assertEqual((b["month_folder"], b["vendor_folder"], b["folder_path"]), ("2026 10", "Costco", "/2026 10/Costco/sub"))
        # Not under a month folder at all.
        x = records["f_x"]
        self.assertEqual((x["month_folder"], x["vendor_folder"], x["folder_path"]), ("", "", "/Misc"))
        # The shortcut resolves to its target, under the shortcut's name and the folder it sits in.
        target = records["f_target"]
        self.assertEqual((target["file_name"], target["vendor_folder"], target["month_folder"]),
                         ("shared receipt.pdf", "Anthropic", "2026 09"))
        self.assertEqual(records["f_gdoc"]["size"], 0)

    def test_month_filter(self):
        ids = {r["id"] for r in invoices.walk(self.drive, "root", month_folders={"2026 09"})}
        self.assertEqual(ids, {"f_a", "f_target", "f_gdoc", "f_loose", "f_docx", "f_txt"})
        ids = {r["id"] for r in invoices.walk(self.drive, "root", month_folders={"2026 10"})}
        self.assertEqual(ids, {"f_b", "f_huge"})
        self.assertEqual(list(invoices.walk(self.drive, "root", month_folders={"2025 01"})), [])

    def test_max_depth(self):
        ids = {r["id"] for r in invoices.walk(self.drive, "root", max_depth=2)}
        self.assertNotIn("f_b", ids)      # root/2026 10/Costco/sub is depth 3
        self.assertIn("f_a", ids)

    def test_gemini_mime(self):
        gm = invoices.gemini_mime
        self.assertEqual(gm({"mime_type": "application/pdf", "file_name": "a.pdf"}), "application/pdf")
        self.assertEqual(gm({"mime_type": GDOC, "file_name": "doc"}), "application/pdf")
        self.assertEqual(gm({"mime_type": "image/jpg", "file_name": "a.jpg"}), "image/jpeg")
        self.assertEqual(gm({"mime_type": "application/octet-stream", "file_name": "scan.PNG"}), "image/png")
        self.assertEqual(gm({"mime_type": "text/plain", "file_name": "order.txt"}), "text/plain")
        self.assertEqual(gm({"mime_type": DOCX, "file_name": "notes.docx"}), "")


class IndexInvoicesTests(unittest.TestCase):
    def setUp(self):
        self.tree, files_by_id = invoice_tree()
        self.drive = FakeDrive(self.tree, files_by_id, contents={"f_txt": b"Order 123 total 39.00"})
        self.gemini = FakeGemini(handler=facts_handler)
        self.store = MemoryStore()

    def run_index(self, **kwargs):
        kwargs.setdefault("log", quiet)
        return invoices.index_invoices(self.store, self.drive, self.gemini, "root", **kwargs)

    def test_first_run_extracts_everything_once(self):
        result = self.run_index()
        self.assertEqual((result["files_seen"], result["indexed"], result["remaining"]), (9, 9, 0))
        self.assertEqual(result["month_folders"], "all")
        self.assertEqual({e["file"] for e in result["errors"]}, {"notes.docx", "huge.pdf"})
        self.assertEqual(len(self.gemini.calls), 7)
        self.assertEqual(sorted(self.drive.downloads), ["f_a", "f_b", "f_gdoc", "f_loose", "f_target", "f_txt", "f_x"])
        self.assertEqual(len(self.store.invoices), 9)
        a = self.store.get_invoice("f_a")
        self.assertEqual((a["vendor"], a["total"], a["currency"], a["invoice_date"], a["card_last4"]),
                         ("Anthropic", 150.0, "CAD", "2026-09-03", "1610"))
        self.assertEqual((a["month_folder"], a["vendor_folder"], a["file_name"], a["invoice_number"]),
                         ("2026 09", "Anthropic", "a.pdf", "INV-0001"))
        self.assertTrue(a["is_invoice"])
        self.assertIsNone(a["extraction_error"])
        self.assertTrue(a["extracted_at"])
        self.assertEqual(self.store.get_invoice("f_target")["vendor_folder"], "Anthropic")
        self.assertEqual(self.store.get_invoice("f_target")["file_name"], "shared receipt.pdf")
        x = self.store.get_invoice("f_x")
        self.assertEqual((x["is_invoice"], x["month_folder"], x["total"], x["summary"]), (False, "", None, "a contract"))
        docx = self.store.get_invoice("f_docx")
        self.assertTrue(docx["extraction_error"].startswith("unsupported"))
        self.assertFalse(docx["is_invoice"])
        self.assertNotIn("f_docx", self.drive.downloads)
        huge = self.store.get_invoice("f_huge")
        self.assertEqual(huge["extraction_error"], "unsupported: file larger than 15 MB")
        self.assertNotIn("f_huge", self.drive.downloads)
        # Google Docs are exported as PDF; text files go to the model as text.
        by_name = {file_name_in(c["parts"]): c["parts"] for c in self.gemini.calls}
        self.assertEqual(by_name["Google Workspace invoice"][0]["inlineData"]["mimeType"], "application/pdf")
        self.assertEqual(by_name["a.pdf"][0]["inlineData"]["data"], b"%PDF-1.4 f_a")
        self.assertEqual(len(by_name["order.txt"]), 1)
        self.assertIn("Order 123 total 39.00", by_name["order.txt"][0]["text"])
        self.assertIn('month folder "2026 09", vendor folder "Anthropic", file name "a.pdf"', by_name["a.pdf"][1]["text"])
        self.assertIn('month folder "-", vendor folder "-", file name "x.pdf"', by_name["x.pdf"][1]["text"])
        self.assertEqual({c["model"] for c in self.gemini.calls}, {"fake-extract"})

    def test_second_run_extracts_nothing(self):
        self.run_index()
        result = self.run_index()
        self.assertEqual((result["files_seen"], result["indexed"], result["remaining"], result["errors"]), (9, 0, 0, []))
        self.assertEqual(len(self.gemini.calls), 7)
        self.assertEqual(len(self.drive.downloads), 7)

    def test_changed_file_is_read_again(self):
        self.run_index()
        self.tree["v_anth"][0]["modifiedTime"] = "2026-09-09T09:09:09Z"
        result = self.run_index()
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(len(self.gemini.calls), 8)
        self.assertEqual(self.drive.downloads[-1], "f_a")
        self.assertEqual(self.store.get_invoice("f_a")["modified_time"], "2026-09-09T09:09:09Z")
        self.assertEqual(self.run_index()["indexed"], 0)

    def test_moved_file_keeps_its_facts_and_gets_the_new_folder(self):
        self.run_index()
        a = self.tree["v_anth"].pop(0)
        self.tree["m09"].append(folder("v_anth2", "Anthropic PBC"))
        self.tree["v_anth2"] = [dict(a, name="a renamed.pdf")]
        result = self.run_index()
        self.assertEqual(result["indexed"], 0)
        self.assertEqual(len(self.gemini.calls), 7)
        row = self.store.get_invoice("f_a")
        self.assertEqual((row["vendor_folder"], row["folder_path"], row["file_name"]),
                         ("Anthropic PBC", "/2026 09/Anthropic PBC", "a renamed.pdf"))
        self.assertEqual((row["vendor"], row["total"], row["invoice_date"]), ("Anthropic", 150.0, "2026-09-03"))

    def test_limit_and_remaining(self):
        self.assertEqual([(r["indexed"], r["remaining"]) for r in (self.run_index(limit=4), self.run_index(limit=4),
                                                                   self.run_index(limit=4), self.run_index(limit=4))],
                         [(4, 5), (4, 1), (1, 0), (0, 0)])
        self.assertEqual(len(self.store.invoices), 9)
        self.assertEqual(len(self.gemini.calls), 7)

    def test_month_filter(self):
        result = self.run_index(month_folders={"2026 09"})
        self.assertEqual((result["files_seen"], result["indexed"]), (6, 6))
        self.assertEqual(result["month_folders"], ["2026 09"])
        self.assertEqual(set(self.store.invoices), {"f_a", "f_target", "f_gdoc", "f_loose", "f_docx", "f_txt"})
        result = self.run_index(month_folders={"2026 10", "2026 09"})
        self.assertEqual((result["files_seen"], result["indexed"], result["month_folders"]), (8, 2, ["2026 09", "2026 10"]))

    def test_transient_failure_is_stored_and_retried(self):
        failing = {"b.pdf"}

        def flaky(parts, model):
            if file_name_in(parts) in failing:
                raise RuntimeError("model unavailable")
            return facts_handler(parts, model)
        self.gemini.handler = flaky
        result = self.run_index()
        self.assertIn({"file": "b.pdf", "error": "model unavailable"}, result["errors"])
        b = self.store.get_invoice("f_b")
        self.assertEqual(b["extraction_error"], "model unavailable")
        self.assertNotIn("vendor", b)
        failing.clear()
        result = self.run_index()
        self.assertEqual((result["indexed"], result["errors"]), (1, []))
        b = self.store.get_invoice("f_b")
        self.assertEqual((b["vendor"], b["extraction_error"], b["card_last4"]), ("Costco Wholesale", None, "2301"))
        self.assertEqual(self.run_index()["indexed"], 0)

    def test_list_shaped_answer_and_empty_folder(self):
        self.gemini.handler = lambda parts, model: [dict(INVOICE_FACTS["a.pdf"])]
        self.run_index(month_folders={"2026 09"})
        self.assertEqual(self.store.get_invoice("f_loose")["vendor"], "Anthropic")
        empty = invoices.index_invoices(MemoryStore(), FakeDrive({}), self.gemini, "root", log=quiet)
        self.assertEqual((empty["files_seen"], empty["indexed"], empty["remaining"], empty["errors"]), (0, 0, 0, []))


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------
class ChatCardTests(unittest.TestCase):
    def setUp(self):
        self.card = {"last4": "1610", "label": "Visa 1610", "holder_name": "Yin Wei"}
        self.missing = [txn("t1", "2026-09-04", "Anthropic* Claude Team", 150.00),
                        txn("t2", "2026-09-12", "Www.Retellai.Com", 96.12, source_amount=70.40, source_currency="USD")]
        self.possible = [txn("t3", "2026-09-10", "Amzn Mktp Ca", 45.10, status="possible")]

    def test_payload_shape(self):
        payload = chat.missing_invoices_card(self.card, 2026, 9, self.missing, self.possible, "https://ap.test/",
                                             "https://drive.google.com/drive/folders/abc", uncoded=2)
        json.dumps(payload)
        self.assertEqual(list(payload), ["cardsV2"])
        self.assertEqual(len(payload["cardsV2"]), 1)
        self.assertEqual(payload["cardsV2"][0]["cardId"], "ap-1610-2026-09")
        sections = payload["cardsV2"][0]["card"]["sections"]
        self.assertEqual([s.get("header") for s in sections],
                         [None, "No invoice found", "Possible matches – please confirm", None])
        head = sections[0]["widgets"][0]["textParagraph"]["text"]
        self.assertIn("Invoices needed – Visa 1610</b> (Yin Wei)", head)
        self.assertIn("<b>Month:</b> September 2026", head)
        self.assertIn("<b>Missing:</b> 2 purchases · CAD 246.12", head)
        self.assertIn("<b>To confirm:</b> 1 possible match<", head)
        self.assertIn("<b>Without cost centre:</b> 2", head)
        lines = sections[1]["widgets"][0]["textParagraph"]["text"]
        self.assertIn("09-04 · Anthropic* Claude Team · <b>CAD 150.00</b>", lines)
        self.assertIn("09-12 · Www.Retellai.Com · <b>CAD 96.12</b> (USD 70.40)", lines)
        self.assertIn("09-10 · Amzn Mktp Ca · <b>CAD 45.10</b>", sections[2]["widgets"][0]["textParagraph"]["text"])
        footer = sections[3]["widgets"]
        self.assertIn("textParagraph", footer[0])
        buttons = footer[1]["buttonList"]["buttons"]
        self.assertEqual([b["text"] for b in buttons], ["Open dashboard", "Invoice folder"])
        self.assertEqual(buttons[0]["onClick"]["openLink"]["url"], "https://ap.test/#/2026/09?card=1610")
        self.assertEqual(buttons[1]["onClick"]["openLink"]["url"], "https://drive.google.com/drive/folders/abc")

    def test_minimal_card(self):
        payload = chat.missing_invoices_card({"last4": "4894"}, 2026, 10, [], self.possible)
        sections = payload["cardsV2"][0]["card"]["sections"]
        self.assertEqual([s.get("header") for s in sections], [None, "Possible matches – please confirm", None])
        head = sections[0]["widgets"][0]["textParagraph"]["text"]
        self.assertIn("Invoices needed – Card ending 4894</b><br>", head)
        self.assertIn("<b>Missing:</b> 0 purchases · CAD 0.00", head)
        self.assertNotIn("cost centre", head)
        self.assertEqual(len(sections[-1]["widgets"]), 1)     # no buttons without URLs
        self.assertEqual(payload["cardsV2"][0]["cardId"], "ap-4894-2026-10")

    def test_long_lists_are_truncated(self):
        many = [txn(f"t{i}", "2026-09-01", f"Vendor {i}", 1.0) for i in range(30)]
        payload = chat.missing_invoices_card(self.card, 2026, 9, many, [])
        text = payload["cardsV2"][0]["card"]["sections"][1]["widgets"][0]["textParagraph"]["text"]
        self.assertIn("… and 5 more", text)
        self.assertEqual(text.count("<br>"), 25)

    def test_money_and_labels(self):
        self.assertEqual(chat.money(1234.5), "CAD 1,234.50")
        self.assertEqual(chat.money(-5, "USD"), "-USD 5.00")
        self.assertEqual(chat.month_label(2026, 9), "September 2026")

    def test_webhooks(self):
        hooks = chat.load_webhooks('{"1610": "https://h/1610", "default": "https://h/default", "empty": ""}')
        self.assertEqual(hooks, {"1610": "https://h/1610", "default": "https://h/default"})
        self.assertEqual(chat.webhook_for(hooks, "1610"), "https://h/1610")
        self.assertEqual(chat.webhook_for(hooks, "9999"), "https://h/default")
        self.assertEqual(chat.load_webhooks("", "https://h/global"), {"default": "https://h/global"})
        self.assertEqual(chat.load_webhooks('{"1610": " https://h/1610 "}', "https://h/global"),
                         {"1610": "https://h/1610", "default": "https://h/global"})
        self.assertEqual(chat.load_webhooks('{"default": "https://h/json"}', "https://h/global"),
                         {"default": "https://h/json"})
        with mock.patch("builtins.print"):
            self.assertEqual(chat.load_webhooks("not json"), {})
            self.assertEqual(chat.load_webhooks("[1, 2]"), {})
        self.assertEqual(chat.webhook_for({}, "1610"), "")
        self.assertEqual(chat.post("", {}), (False, "no webhook configured for this card (CARD_WEBHOOKS_JSON)"))


# ---------------------------------------------------------------------------
# main: Flask API
# ---------------------------------------------------------------------------
MEMBER = {"email": "x@superhairpieces.com", "name": "X", "role": "member", "picture": ""}


class ApiTestCase(unittest.TestCase):
    """Fresh in-memory store and a fake Drive per test; AI stays disabled."""

    def setUp(self):
        self.store = MemoryStore()
        self.drive = FakeDrive({})
        main.set_clients(store=self.store, drive=self.drive)
        self.client = main.app.test_client()
        self.xlsx = build_xlsx(sample_rows())

    def upload(self, data=None, name="Transaction Export.xlsx", query=""):
        data = self.xlsx if data is None else data
        return self.client.post("/api/statements" + query, data={"file": (io.BytesIO(data), name)},
                                content_type="multipart/form-data")

    def transactions(self, year=2026, month=9, card=None):
        url = f"/api/transactions?year={year}&month={month}" + (f"&card={card}" if card else "")
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()

    def patch(self, txn_id, body, expect=200):
        r = self.client.patch(f"/api/transactions/{txn_id}", json=body)
        self.assertEqual(r.status_code, expect, r.get_json())
        return r.get_json()

    def ids(self):
        rows = self.transactions()["transactions"]
        return {
            "anth": one(rows, "Anthropic* Claude Team")["id"],
            "amzn": [t for t in by_description(rows, "Amzn Mktp Ca") if t["type"] == "purchase"][0]["id"],
            "retell": one(rows, "Www.Retellai.Com")["id"],
            "costco": one(rows, "Costco Wholesale")["id"],
            "fee": one(rows, "Annual Card Fee")["id"],
        }

    def inject_invoices(self):
        self.store.upsert_invoices([
            inv("inv_anth", "Anthropic", 150.00, "2026-09-03"),
            inv("inv_far", "Dunder Mifflin", 45.10, "2026-06-01", month_folder="2026 08"),
            inv("inv_spare", "Spare Vendor", 999.99, "2026-09-10"),
            inv("inv_old", "Old Vendor", 1.00, "2025-01-01", month_folder="2025 01"),
        ])


class ApiFlowTests(ApiTestCase):
    def test_config_and_health(self):
        config = self.client.get("/api/config").get_json()
        self.assertEqual((config["auth_disabled"], config["ai_enabled"], config["dashboard_url"]),
                         (True, False, "https://ap.test"))
        self.assertTrue(config["invoice_folder_url"].startswith("https://drive.google.com/drive/folders/"))
        health = self.client.get("/healthz").get_json()
        self.assertEqual((health["ok"], health["store"]), (True, "memory"))
        me = self.client.get("/api/me").get_json()
        self.assertEqual((me["role"], me["cards_owned"]), ("ap", []))

    def test_upload_months_and_transactions(self):
        r = self.upload()
        self.assertEqual(r.status_code, 200, r.get_json())
        summary = r.get_json()
        self.assertEqual((summary["status"], summary["transactions"], summary["new"], summary["already_stored"]),
                         ("ok", 10, 10, 0))
        self.assertEqual(summary["months"], [[2026, 9], [2026, 10]])
        self.assertEqual(summary["cards"]["1610"], {"holder_name": "Yin Wei", "count": 5})
        self.assertEqual((summary["period_start"], summary["period_end"]), PERIOD)
        self.assertEqual(summary["format"], "table")
        self.assertIsNone(summary["drive_link"])

        months = self.client.get("/api/months").get_json()
        self.assertEqual([(m["year"], m["month"], m["label"]) for m in months["months"]],
                         [(2026, 10, "October 2026"), (2026, 9, "September 2026")])
        sept = months["months"][1]
        self.assertEqual((sept["transactions"], sept["missing"], sept["matched"], sept["uncoded"]), (9, 6, 0, 6))
        self.assertAlmostEqual(sept["spend"], 389.72)
        self.assertEqual(sept["cards"]["1610"]["label"], "Card ending 1610 - Yin Wei")
        self.assertEqual((sept["cards"]["1610"]["transactions"], sept["cards"]["1610"]["missing"]), (5, 2))
        self.assertAlmostEqual(sept["cards"]["1610"]["spend"], 195.10)
        self.assertEqual(sorted(c["last4"] for c in months["cards"]), ["1610", "2301", "4894"])
        self.assertIn("Marketing", months["cost_centers"])

        page = self.transactions()
        rows = page["transactions"]
        self.assertEqual(len(rows), 9)
        self.assertEqual([t["txn_date"] for t in rows], sorted(t["txn_date"] for t in rows))
        self.assertTrue(all(t["editable"] for t in rows))
        self.assertTrue(all("raw" not in t for t in rows))
        self.assertEqual(one(rows, "Anthropic* Claude Team")["status_label"], "Invoice missing")
        self.assertEqual(one(rows, "Anthropic* Claude Team")["invoice"], None)
        self.assertEqual(one(rows, "Payment")["invoice_status"], "not_required")
        self.assertEqual(one(rows, "Annual Card Fee")["invoice_status"], "not_required")
        self.assertEqual(one(rows, "Annual Card Fee")["status_label"], "No invoice needed")
        self.assertEqual([t["new_count"] for t in page["statements"]], [10])
        self.assertEqual(page["notifications"], [])
        self.assertEqual(len(self.transactions(card="4894")["transactions"]), 3)
        self.assertEqual(len(self.transactions(2026, 10)["transactions"]), 1)
        self.assertEqual(len(self.transactions(2026, 8)["transactions"]), 0)
        statements_page = self.client.get("/api/statements?year=2026&month=10").get_json()
        self.assertEqual(len(statements_page["statements"]), 1)
        self.assertEqual(statements_page["statements"][0]["cards"], ["1610", "2301", "4894"])
        self.assertEqual(statements_page["statements"][0]["months"], ["2026 09", "2026 10"])

    def test_uploading_the_same_file_again_stores_nothing(self):
        self.upload()
        r = self.upload(name="Transaction Export (1).xlsx")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.get_json()["transactions"], r.get_json()["new"], r.get_json()["already_stored"]), (10, 0, 10))
        self.assertEqual(len(self.store.transactions), 10)
        self.assertEqual(len(self.client.get("/api/statements?year=2026&month=9").get_json()["statements"]), 2)
        self.assertEqual(self.client.get("/api/months").get_json()["months"][1]["transactions"], 9)
        # Coding survives a re-upload because the stored row is never overwritten.
        ids = self.ids()
        self.patch(ids["anth"], {"cost_center": "IT & Software"})
        self.upload()
        self.assertEqual(self.store.get_transaction(ids["anth"])["cost_center"], "IT & Software")

    def test_upload_errors(self):
        r = self.client.post("/api/statements", data={}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.upload(b"", name="empty.xlsx").status_code, 400)
        r = self.upload(b"PK\x03\x04", name="notes.docx")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Unsupported file type", r.get_json()["error"])
        r = self.upload(b"%PDF-1.4", name="statement.pdf")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Gemini", r.get_json()["error"])
        self.assertEqual(self.store.transactions, {})

    def test_sync_upload_runs_the_whole_pipeline(self):
        self.inject_invoices()
        with mock.patch.object(chat, "post", return_value=(True, "sent")) as post:
            r = self.upload(query="?sync=1")
        self.assertEqual(r.status_code, 200, r.get_json())
        summary = r.get_json()
        self.assertEqual(summary["status"], "ok")
        self.assertEqual([(i["year"], i["month"], i["indexed"]) for i in summary["index"]], [(2026, 9, 0), (2026, 10, 0)])
        self.assertIn("AI_DISABLED", summary["index"][0]["skipped"])
        self.assertEqual([(m["year"], m["month"], m["matched"], m["possible"]) for m in summary["match"]],
                         [(2026, 9, 1, 1), (2026, 10, 0, 0)])
        self.assertEqual(len(summary["notify"]), 2)
        self.assertEqual([c["card"] for c in summary["notify"][0]["cards"]], ["1610", "2301", "4894"])
        self.assertEqual(post.call_count, 4)
        self.assertEqual(len(self.store.notifications), 4)
        with mock.patch.object(chat, "post", side_effect=AssertionError("must not post")):
            r = self.upload(query="?sync=1&notify=0")
        self.assertEqual(r.get_json()["notify"], [])

    def test_coding_waive_link_unlink(self):
        self.upload()
        ids = self.ids()
        row = self.patch(ids["anth"], {"cost_center": "IT & Software", "usage": "Claude Team seats", "note": "monthly"})["transaction"]
        self.assertEqual((row["cost_center"], row["usage"], row["note"], row["updated_by"]),
                         ("IT & Software", "Claude Team seats", "monthly", "local@dev"))
        self.assertTrue(row["updated_at"])
        self.assertEqual(row["invoice_status"], "missing")
        row = self.patch(ids["anth"], {"usage": "   "})["transaction"]
        self.assertIsNone(row["usage"])
        self.assertEqual(row["cost_center"], "IT & Software")
        row = self.patch(ids["anth"], {"cost_center": "x" * 150})["transaction"]
        self.assertEqual(len(row["cost_center"]), 100)
        months = self.client.get("/api/months").get_json()["months"][1]
        self.assertEqual(months["uncoded"], 5)

        row = self.patch(ids["retell"], {"action": "waive"})["transaction"]
        self.assertEqual((row["invoice_status"], row["match_method"], row["match_note"], row["invoice_id"]),
                         ("waived", "manual", "waived: no invoice available", None))
        self.assertEqual(row["status_label"], "No invoice (waived)")
        self.assertEqual(self.client.get("/api/months").get_json()["months"][1]["waived"], 1)
        row = self.patch(ids["retell"], {"action": "unwaive"})["transaction"]
        self.assertEqual((row["invoice_status"], row["match_method"], row["match_note"]), ("missing", None, None))
        row = self.patch(ids["costco"], {"action": "waive", "reason": "vendor sends no receipts"})["transaction"]
        self.assertEqual(row["match_note"], "waived: vendor sends no receipts")
        # Waiving a fee or an already waived line changes nothing (nothing to update).
        self.patch(ids["fee"], {"action": "waive"}, expect=400)
        self.patch(ids["costco"], {"action": "waive"}, expect=400)
        self.patch(ids["costco"], {"action": "unwaive"})

        self.inject_invoices()
        row = self.patch(ids["retell"], {"action": "link", "invoice_id": "inv_spare"})["transaction"]
        self.assertEqual((row["invoice_status"], row["invoice_id"], row["match_confidence"], row["match_method"],
                          row["match_note"]), ("matched", "inv_spare", 1.0, "manual", "linked by local@dev"))
        self.assertEqual(row["invoice"]["file_name"], "inv_spare.pdf")
        self.assertEqual(row["invoice"]["vendor"], "Spare Vendor")
        self.assertEqual(row["status_label"], "Invoice found")
        r = self.patch(ids["costco"], {"action": "link", "invoice_id": "inv_spare"}, expect=409)
        self.assertIn("already linked", r["error"])
        self.assertEqual(self.store.get_transaction(ids["costco"])["invoice_status"], "missing")
        self.patch(ids["retell"], {"action": "link", "invoice_id": "inv_spare"})          # re-linking itself is fine
        self.patch(ids["costco"], {"action": "link", "invoice_id": "nope"}, expect=404)
        self.patch(ids["costco"], {"action": "link"}, expect=404)
        linked = {i["id"]: i["linked"] for i in self.client.get("/api/invoices?year=2026&month=9").get_json()["invoices"]}
        self.assertEqual(linked, {"inv_anth": False, "inv_far": False, "inv_spare": True})
        row = self.patch(ids["retell"], {"action": "unlink"})["transaction"]
        self.assertEqual((row["invoice_status"], row["invoice_id"], row["invoice"], row["match_method"]),
                         ("missing", None, None, None))
        self.patch(ids["retell"], {"action": "unlink"}, expect=400)
        self.patch(ids["retell"], {"action": "explode"}, expect=400)
        self.patch(ids["retell"], {}, expect=400)
        self.patch("t_missing", {"action": "waive"}, expect=404)
        # A link can be combined with coding in the same request.
        row = self.patch(ids["retell"], {"action": "link", "invoice_id": "inv_spare", "usage": "voice agent"})["transaction"]
        self.assertEqual((row["invoice_id"], row["usage"]), ("inv_spare", "voice agent"))

    def test_match_confirm_invoices_cards_and_notify(self):
        self.upload()
        ids = self.ids()
        self.inject_invoices()

        r = self.client.post("/api/match?year=2026&month=9")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json(), {"status": "ok", "checked": 6, "matched": 1, "possible": 1, "missing": 4,
                                        "invoices_available": 3})
        rows = self.transactions()["transactions"]
        anth = one(rows, "Anthropic* Claude Team")
        self.assertEqual((anth["invoice_status"], anth["invoice_id"], anth["match_method"]), ("matched", "inv_anth", "rule"))
        self.assertEqual(anth["invoice"]["vendor"], "Anthropic")
        amzn = self.store.get_transaction(ids["amzn"])
        self.assertEqual((amzn["invoice_status"], amzn["invoice_id"], amzn["match_confidence"]), ("possible", "inv_far", 0.6))
        self.assertEqual(one(rows, "Costco Wholesale")["invoice_status"], "missing")
        months = self.client.get("/api/months").get_json()["months"][1]
        self.assertEqual((months["matched"], months["possible"], months["missing"]), (1, 1, 4))

        self.patch(ids["anth"], {"action": "confirm"}, expect=400)       # only a possible match can be confirmed
        row = self.patch(ids["amzn"], {"action": "confirm"})["transaction"]
        self.assertEqual((row["invoice_status"], row["match_confidence"], row["match_method"], row["match_note"]),
                         ("matched", 1.0, "manual", "confirmed by local@dev"))
        self.assertEqual(row["invoice_id"], "inv_far")
        r = self.client.post("/api/match?year=2026&month=9")
        self.assertEqual(r.get_json()["checked"], 4)                       # confirmed and matched lines stay put
        self.assertEqual(self.store.get_transaction(ids["amzn"])["match_method"], "manual")

        picker = self.client.get("/api/invoices?year=2026&month=9").get_json()
        self.assertEqual(picker["month_folders"], ["2026 08", "2026 09", "2026 10"])
        self.assertEqual({i["id"]: i["linked"] for i in picker["invoices"]},
                         {"inv_anth": True, "inv_far": True, "inv_spare": False})
        self.assertEqual(set(picker["invoices"][0]) >= {"file_name", "web_view_link", "vendor", "total", "currency",
                                                       "invoice_date", "month_folder", "vendor_folder", "summary"}, True)
        self.assertEqual(self.client.get("/api/invoices?year=2025&month=1").get_json()["invoices"][0]["id"], "inv_old")

        r = self.client.post("/api/cards", json={"last4": "****1610", "label": "Visa 1610 - Yin",
                                                 "owner_email": "Yin@superhairpieces.com", "chat_space": "AP Visa 1610"})
        self.assertEqual(r.status_code, 200, r.get_json())
        cards = {c["last4"]: c for c in r.get_json()["cards"]}
        self.assertEqual((cards["1610"]["label"], cards["1610"]["owner_email"], cards["1610"]["chat_space"],
                          cards["1610"]["holder_name"], cards["1610"]["active"]),
                         ("Visa 1610 - Yin", "yin@superhairpieces.com", "AP Visa 1610", "Yin Wei", True))
        self.assertTrue(cards["1610"]["webhook_configured"])
        self.assertTrue(cards["4894"]["webhook_configured"])          # falls back to the default webhook
        self.assertEqual(self.client.post("/api/cards", json={"last4": "12"}).status_code, 400)
        self.client.post("/api/cards", json={"last4": "2301", "active": False})
        cards = {c["last4"]: c for c in self.client.get("/api/cards").get_json()["cards"]}
        self.assertFalse(cards["2301"]["active"])
        self.assertEqual(cards["2301"]["label"], "Card ending 2301 - Sam Lee")

        with mock.patch.object(chat, "post", side_effect=AssertionError("dry run must not post")):
            r = self.client.post("/api/notify?year=2026&month=9&dry_run=1")
        self.assertEqual(r.status_code, 200, r.get_json())
        results = {c["card"]: c for c in r.get_json()["cards"]}
        self.assertEqual(results["1610"], {"card": "1610", "sent": False, "detail": "nothing missing"})
        self.assertEqual(results["2301"], {"card": "2301", "sent": False, "detail": "card inactive"})
        self.assertEqual((results["4894"]["sent"], results["4894"]["detail"], results["4894"]["missing"],
                          results["4894"]["possible"]), (False, "dry run", 3, 0))
        payload = results["4894"]["payload"]
        self.assertEqual(payload["cardsV2"][0]["cardId"], "ap-4894-2026-09")
        buttons = payload["cardsV2"][0]["card"]["sections"][-1]["widgets"][1]["buttonList"]["buttons"]
        self.assertEqual([b["text"] for b in buttons], ["Open dashboard"])
        self.assertEqual(buttons[0]["onClick"]["openLink"]["url"], "https://ap.test/#/2026/09?card=4894")
        self.assertEqual(self.store.notifications, [])
        self.assertEqual(self.transactions()["notifications"], [])

    def test_notify_posts_to_each_cards_webhook(self):
        self.upload()
        with mock.patch.object(chat, "post", return_value=(True, "sent")) as post:
            r = self.client.post("/api/notify?year=2026&month=9")
        self.assertEqual(r.status_code, 200, r.get_json())
        results = {c["card"]: c for c in r.get_json()["cards"]}
        self.assertEqual({k: (v["sent"], v["missing"]) for k, v in results.items()},
                         {"1610": (True, 2), "2301": (True, 1), "4894": (True, 3)})
        self.assertEqual([call.args[0] for call in post.call_args_list],
                         ["https://chat.test/1610", "https://chat.test/default", "https://chat.test/default"])
        self.assertEqual(post.call_args_list[0].args[1]["cardsV2"][0]["cardId"], "ap-1610-2026-09")
        logged = self.store.list_notifications(2026, 9)
        self.assertEqual([(n["card_last4"], n["missing_count"], n["ok"], n["sent_by"]) for n in logged],
                         [("1610", 2, True, "local@dev"), ("2301", 1, True, "local@dev"), ("4894", 3, True, "local@dev")])
        self.assertEqual(len(logged[2]["transaction_ids"]), 3)
        self.assertEqual(len(self.transactions()["notifications"]), 3)
        with mock.patch.object(chat, "post", return_value=(False, "chat webhook returned 404")):
            r = self.client.post("/api/notify?year=2026&month=9&card=1610")
        self.assertEqual(r.get_json()["cards"], [{"card": "1610", "sent": False, "detail": "chat webhook returned 404",
                                                 "missing": 2, "possible": 0}])

    def test_bad_month_parameters(self):
        for url in ("/api/transactions", "/api/transactions?year=2026", "/api/transactions?year=2026&month=13",
                    "/api/invoices?year=1999&month=1", "/api/statements?year=x&month=1"):
            self.assertEqual(self.client.get(url).status_code, 400, url)
        self.assertEqual(self.client.post("/api/match").status_code, 400)
        self.assertEqual(self.client.post("/api/index?year=2026&month=9").get_json()["skipped"],
                         "AI_DISABLED=1: invoices are not read")


class MemberRoleTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload().status_code, 200)
        self.ids_ = self.ids()

    @staticmethod
    def as_member():
        return mock.patch.object(main, "current_user", return_value=dict(MEMBER))

    def test_member_cannot_upload_or_run_jobs(self):
        with self.as_member():
            r = self.upload()
            self.assertEqual(r.status_code, 403)
            self.assertIn("accounts-payable", r.get_json()["error"])
            for url in ("/api/match?year=2026&month=9", "/api/notify?year=2026&month=9", "/api/index?year=2026&month=9"):
                self.assertEqual(self.client.post(url).status_code, 403, url)
            self.assertEqual(self.client.post("/api/cards", json={"last4": "1610", "label": "mine"}).status_code, 403)
        self.assertEqual(len(self.store.transactions), 10)

    def test_member_edits_only_owned_cards(self):
        with self.as_member():
            me = self.client.get("/api/me").get_json()
            self.assertEqual((me["email"], me["role"], me["cards_owned"]), ("x@superhairpieces.com", "member", []))
            rows = self.transactions()["transactions"]
            self.assertEqual(len(rows), 9)
            self.assertFalse(any(t["editable"] for t in rows))
            r = self.patch(self.ids_["anth"], {"cost_center": "Marketing"}, expect=403)
            self.assertIn("cards you own", r["error"])
            self.assertEqual(self.client.get("/api/months").status_code, 200)
            self.assertEqual(self.client.get("/api/cards").status_code, 200)
            self.assertEqual(self.client.get("/api/invoices?year=2026&month=9").status_code, 200)
        self.assertIsNone(self.store.get_transaction(self.ids_["anth"])["cost_center"])

        r = self.client.post("/api/cards", json={"last4": "1610", "owner_email": "X@superhairpieces.com"})
        self.assertEqual(r.status_code, 200)
        with self.as_member():
            self.assertEqual(self.client.get("/api/me").get_json()["cards_owned"], ["1610"])
            rows = self.transactions()["transactions"]
            self.assertEqual({t["card_last4"] for t in rows if t["editable"]}, {"1610"})
            self.assertEqual({t["card_last4"] for t in rows if not t["editable"]}, {"2301", "4894"})
            row = self.patch(self.ids_["anth"], {"cost_center": "Marketing", "usage": "ads"})["transaction"]
            self.assertEqual((row["cost_center"], row["updated_by"]), ("Marketing", "x@superhairpieces.com"))
            row = self.patch(self.ids_["anth"], {"action": "waive"})["transaction"]
            self.assertEqual(row["invoice_status"], "waived")
            self.patch(self.ids_["retell"], {"cost_center": "Marketing"}, expect=403)
            self.patch(self.ids_["costco"], {"action": "waive"}, expect=403)
        self.assertEqual(self.store.get_transaction(self.ids_["anth"])["cost_center"], "Marketing")
        self.assertEqual(self.store.get_transaction(self.ids_["retell"])["cost_center"], None)

    def test_no_user_is_401(self):
        with mock.patch.object(main, "current_user", return_value=None):
            for url in ("/api/me", "/api/months", "/api/transactions?year=2026&month=9", "/api/cards"):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 401, url)
                self.assertIn("sign in", r.get_json()["error"])
            self.assertEqual(self.upload().status_code, 401)
            self.assertEqual(self.patch(self.ids_["anth"], {"cost_center": "x"}, expect=401)["error"],
                             "sign in with your superhairpieces.com Google account")
        self.assertEqual(self.client.get("/api/config").status_code, 200)   # public


class MaintenanceTests(ApiTestCase):
    def test_requires_api_token(self):
        for url in ("/maintenance/index", "/maintenance/match?year=2026&month=9", "/maintenance/remind"):
            self.assertEqual(self.client.post(url).status_code, 401, url)
            self.assertEqual(self.client.post(url, headers={"X-Api-Token": "wrong"}).status_code, 401, url)
            self.assertEqual(self.client.post(url).get_json(), {"error": "unauthorized"})

    def test_jobs_with_token(self):
        headers = {"X-Api-Token": "test-token"}
        r = self.client.post("/maintenance/index?months=2026 09,2026 10", headers=headers)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual((r.get_json()["status"], r.get_json()["skipped"]), ("ok", "AI_DISABLED=1"))
        r = self.client.post("/maintenance/match?year=2026&month=9", headers=headers)
        self.assertEqual((r.status_code, r.get_json()["checked"]), (200, 0))
        self.assertEqual(self.client.post("/maintenance/match", headers=headers).status_code, 400)
        r = self.client.post("/maintenance/remind", headers=headers)
        self.assertEqual((r.status_code, r.get_json()["reminded"]), (200, []))
        self.upload()
        self.inject_invoices()
        with mock.patch.object(chat, "post", return_value=(True, "sent")) as post:
            r = self.client.post("/maintenance/remind", headers=headers)
        self.assertEqual(r.status_code, 200, r.get_json())
        reminded = r.get_json()["reminded"]
        # Each card's newest month with something missing: 2301 has October, the others September.
        self.assertEqual(sorted((n["year"], n["month"], n["cards"][0]["card"]) for n in reminded),
                         [(2026, 9, "1610"), (2026, 9, "4894"), (2026, 10, "2301")])
        self.assertEqual(post.call_count, 3)
        self.assertEqual(self.store.get_transaction(self.ids()["anth"])["invoice_status"], "matched")
        self.assertEqual([n["sent_by"] for n in self.store.notifications], ["scheduler"] * 3)
        self.assertEqual(self.client.get("/healthz").get_json()["last_job"]["name"], "remind")


if __name__ == "__main__":
    unittest.main()
