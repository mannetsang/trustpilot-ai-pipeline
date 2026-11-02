"""Statement uploads that are not spreadsheets with a header row: a headerless text export (card,
YYYYMMDD date, 0/1 debit-credit flag, description, currency, amount) as .txt, and the same text saved
into a Word document (.docx). The lines below are made up; only the layout is the bank's."""
import io
import os
import sys
import unittest
from unittest import mock  # noqa: F401  (unittest.mock.patch below)
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import ApiTestCase  # noqa: E402
import statements  # noqa: E402

EXPORT_LINES = [
    '00000003,"4514xxxxxxxx7777",20260513,0,"PAYPAL *EXAMPLESHOP ","USD",000002025.00',
    '00000003,"4514xxxxxxxx7777",20260601,0,"AMAZON MKTPL*AAA111","USD",000000007.27',
    '00000003,"4514xxxxxxxx7777",20260601,0,"AMAZON MKTPL*BBB222","USD",000000007.27',
    '00000003,"4514xxxxxxxx7777",20260629,1,"AUTOMATIC PAYMENT -T","USD",000002046.81',
    '00000003,"4514xxxxxxxx7777",20260702,1,"REFUND EXAMPLESHOP","USD",000000012.50',
    '00000003,"4514xxxxxxxx7777",20260903,0,"PAYPAL * EXAMPLESHOP","USD",000003504.00',
]


def docx(paragraphs, table_rows=()):
    """A minimal Word document: each paragraph's text XML-escaped the way Word saves it."""
    def esc(text):
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    body = "".join(f'<w:p w:rsidR="00A1"><w:pPr><w:spacing w:after="0"/></w:pPr><w:r><w:t xml:space="preserve">{esc(p)}</w:t></w:r></w:p>'
                   for p in paragraphs)
    if table_rows:
        body += "<w:tbl>" + "".join(
            "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{esc(c)}</w:t></w:r></w:p></w:tc>" for c in row) + "</w:tr>"
            for row in table_rows) + "</w:tbl>"
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + body + "<w:p/></w:body></w:document>")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return out.getvalue()


class HeaderlessExportTests(unittest.TestCase):
    def check(self, result):
        txns = sorted(result["transactions"], key=lambda t: (t["txn_date"], t["description"]))
        self.assertEqual(result["format"], "table (no header row)")
        self.assertEqual(result["cards"], {"7777": {"holder_name": "", "count": 6}})
        self.assertEqual((result["period_start"], result["period_end"]), ("2026-05-13", "2026-09-03"))
        self.assertIn("no header row", result["warnings"][0])
        rows = [(t["txn_date"], t["type"], t["amount"], t["currency"], t["description"]) for t in txns]
        self.assertEqual(rows, [
            ("2026-05-13", "purchase", 2025.0, "USD", "PAYPAL *EXAMPLESHOP"),
            ("2026-06-01", "purchase", 7.27, "USD", "AMAZON MKTPL*AAA111"),
            ("2026-06-01", "purchase", 7.27, "USD", "AMAZON MKTPL*BBB222"),
            ("2026-06-29", "payment", -2046.81, "USD", "AUTOMATIC PAYMENT -T"),
            ("2026-07-02", "credit", -12.5, "USD", "REFUND EXAMPLESHOP"),
            ("2026-09-03", "purchase", 3504.0, "USD", "PAYPAL * EXAMPLESHOP"),
        ])
        self.assertEqual(len({t["id"] for t in txns}), 6)          # same day and amount, different lines
        self.assertEqual(txns[0]["raw"]["Card number"], "4514xxxxxxxx7777")
        return txns

    def test_a_txt_export(self):
        self.check(statements.parse_statement("NFG_7777_Apr_1-Oct_21_2026.txt", "\n".join(EXPORT_LINES).encode()))

    def test_the_same_export_saved_into_word(self):
        txt = self.check(statements.parse_statement("x.txt", "\n".join(EXPORT_LINES).encode()))
        doc = self.check(statements.parse_statement("NFG_7777_Apr_1-Oct_21_2026.docx", docx(EXPORT_LINES + [""])))
        self.assertEqual([t["id"] for t in txt], [t["id"] for t in doc])   # uploading both adds nothing twice

    def test_a_word_table_and_a_mime_type_without_extension(self):
        rows = [line.replace('"', "").split(",") for line in EXPORT_LINES]
        result = statements.parse_statement(
            "statement", docx([], table_rows=rows),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.check(result)

    def test_the_banks_own_fees_are_fees_not_purchases(self):
        lines = EXPORT_LINES + ['00000003,"4514xxxxxxxx7777",20260428,0,"OVERLIMIT FEE       ","USD",000000029.00',
                                '00000003,"4514xxxxxxxx7777",20261001,0,"ANNUAL FEE          ","USD",000000065.00',
                                '00000003,"4514xxxxxxxx7777",20261002,0,"FEES R US CONSULTING","USD",000000100.00']
        txns = {t["description"]: t for t in statements.parse_statement("x.txt", "\n".join(lines).encode())["transactions"]}
        self.assertEqual((txns["OVERLIMIT FEE"]["type"], txns["OVERLIMIT FEE"]["amount"]), ("fee", 29.0))
        self.assertEqual(txns["ANNUAL FEE"]["type"], "fee")
        self.assertEqual(txns["FEES R US CONSULTING"]["type"], "purchase")      # a supplier, not the bank
        plain = {t["id"] for t in statements.parse_statement("x.txt", "\n".join(EXPORT_LINES).encode())["transactions"]}
        self.assertTrue(plain <= set(t["id"] for t in txns.values()))          # ids do not depend on the type
        # A table without a type column, charges negative: the sign is flipped first, then the fee named.
        csv = b"Date,Description,Amount\n2026-03-01,ANNUAL FEE,-175.00\n2026-03-02,SQ *SQUARE PAID SERVICES,-45.20\n"
        rows = {t["description"]: t for t in statements.parse_statement("x.csv", csv)["transactions"]}
        self.assertEqual((rows["ANNUAL FEE"]["type"], rows["ANNUAL FEE"]["amount"]), ("fee", 175.0))
        self.assertEqual(rows["SQ *SQUARE PAID SERVICES"]["type"], "purchase")

    def test_a_file_with_a_header_row_is_read_as_before(self):
        result = statements.parse_statement("x.docx", docx(["Date,Description,Amount", "2026-09-03,ANTHROPIC,150.00"]))
        self.assertEqual(result["format"], "table")
        self.assertEqual(result["transactions"][0]["amount"], 150.0)

    def test_what_cannot_be_read(self):
        with self.assertRaises(statements.StatementError) as ctx:
            statements.parse_statement("x.docx", b"not a zip")
        self.assertIn("could not be opened", str(ctx.exception))
        with self.assertRaises(statements.StatementError) as ctx:
            statements.parse_statement("x.docx", docx([]))
        self.assertIn("no text", str(ctx.exception))
        # Text with no amount column is not guessed at: without Gemini it is refused as before.
        with self.assertRaises(statements.StatementError) as ctx:
            statements.parse_statement("x.txt", b"hello,world\nfoo,bar\n")
        self.assertIn("Could not recognise the columns", str(ctx.exception))

    def test_a_word_file_too_big_to_read(self):
        with self.assertRaises(statements.StatementError) as ctx:
            with unittest.mock.patch.object(statements, "DOCX_MAX_XML", 100):
                statements.parse_statement("x.docx", docx(EXPORT_LINES))
        self.assertIn("too large", str(ctx.exception))


class BankExportTests(unittest.TestCase):
    """Two more exports found in the Bank Transactions folder (made-up lines, the banks' layouts)."""

    RBC_BANKING = (b"Account Type,Account Number,Transaction Date,Cheque Number,Description 1,Description 2,CAD$,USD$\r\n"
                   b"Visa,4516070100670000,1/1/2026,,SQ *SQUARE PAID SERVICES square.com,,-237.3,\r\n"
                   b"Visa,4516070100670000,1/3/2026,,ROGERS ******2689 888-764-3771,,-106.21,\r\n"
                   b"Visa,4516070100670000,1/5/2026,,AMAZON WEB SERVICES,AWS.AMAZON.COM,,-12.50\r\n"
                   b"Visa,4516070100670000,1/19/2026,,AUTOMATIC PAYMENT -THANK YOU,,1009.02,\r\n"
                   b"Visa,4516070100670000,3/1/2026,,ANNUAL FEE,,-175,\r\n")

    def test_rbc_online_banking_export(self):
        """Account Number, Description 1 and 2, and an amount column per currency (CAD$, USD$), money
        going out negative."""
        r = statements.parse_statement("True Top 0000 Jan 1-Mar 31 2026.csv", self.RBC_BANKING)
        rows = [(t["card_last4"], t["txn_date"], t["type"], t["amount"], t["currency"], t["description"])
                for t in r["transactions"]]
        self.assertEqual(rows, [
            ("0000", "2026-01-01", "purchase", 237.3, "CAD", "SQ *SQUARE PAID SERVICES square.com"),
            ("0000", "2026-01-03", "purchase", 106.21, "CAD", "ROGERS ******2689 888-764-3771"),
            ("0000", "2026-01-05", "purchase", 12.5, "USD", "AMAZON WEB SERVICES AWS.AMAZON.COM"),
            ("0000", "2026-01-19", "payment", -1009.02, "CAD", "AUTOMATIC PAYMENT -THANK YOU"),
            ("0000", "2026-03-01", "fee", 175.0, "CAD", "ANNUAL FEE"),
        ])

    def test_rbc_online_banking_signs_hold_for_a_short_file(self):
        """Mostly payments in a short file: the 80% rule alone would not flip it; the layout says how."""
        data = (b"Account Type,Account Number,Transaction Date,Cheque Number,Description 1,Description 2,CAD$,USD$\n"
                b"Visa,4516070100670000,2/17/2026,,AUTOMATIC PAYMENT -THANK YOU,,1009.02,\n"
                b"Visa,4516070100670000,2/18/2026,,AUTOMATIC PAYMENT -THANK YOU,,500.00,\n"
                b"Visa,4516070100670000,2/19/2026,,PETRO-CANADA 00453,,-109.98,\n")
        types = [(t["type"], t["amount"]) for t in statements.parse_statement("x.csv", data)["transactions"]]
        self.assertEqual(types, [("payment", -1009.02), ("payment", -500.0), ("purchase", 109.98)])

    def test_chase_card_activity(self):
        """No card column (the card is in the file name), no currency column (Chase accounts are USD),
        Type Sale / Return / Payment / Fee, sales negative."""
        data = (b"Transaction Date,Post Date,Description,Category,Type,Amount,Memo\r\n"
                b"10/19/2026,10/21/2026,DELTA AIR   0067514863335,Travel,Sale,-188.4,\r\n"
                b"09/08/2026,09/10/2026,ANCHOR YACHT CHARTERS,Entertainment,Return,2061.98,\r\n"
                b"09/04/2026,09/06/2026,ANCHOR YACHT CHARTERS,Entertainment,Sale,-2061.98,\r\n"
                b"09/15/2026,09/15/2026,Payment Thank You-Mobile,,Payment,3000.00,\r\n"
                b"09/20/2026,09/20/2026,LATE FEE,Fees & Adjustments,Fee,-39.00,\r\n")
        r = statements.parse_statement("US Chase4692_Activity_20260101-1022.csv", data)
        rows = [(t["card_last4"], t["txn_date"], t["post_date"], t["type"], t["amount"], t["currency"])
                for t in r["transactions"]]
        self.assertEqual(rows, [
            ("4692", "2026-10-19", "2026-10-21", "purchase", 188.4, "USD"),
            ("4692", "2026-09-08", "2026-09-10", "credit", -2061.98, "USD"),
            ("4692", "2026-09-04", "2026-09-06", "purchase", 2061.98, "USD"),
            ("4692", "2026-09-15", "2026-09-15", "payment", -3000.0, "USD"),
            ("4692", "2026-09-20", "2026-09-20", "fee", 39.0, "USD"),
        ])
        self.assertEqual(r["transactions"][0]["description"], "DELTA AIR   0067514863335")

    def test_a_chase_layout_with_its_own_currency_column_keeps_it(self):
        data = (b"Transaction Date,Post Date,Description,Category,Type,Amount,Memo,Currency\n"
                b"10/19/2026,10/21/2026,DELTA AIR,Travel,Sale,-188.4,,CAD\n")
        self.assertEqual(statements.parse_statement("x.csv", data)["transactions"][0]["currency"], "CAD")


class BankExportEdgeTests(unittest.TestCase):
    """What the review of the real files turned up."""

    def test_chase_adjustments_payments_and_fees_without_any_sale(self):
        """The layout fixes the signs, so a quiet month (no Sale line to judge by) reads the same way,
        and an Adjustment (no telling word) follows its sign after the flip: a statement credit."""
        data = (b"Card,Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
                b"9783,09/15/2026,09/16/2026,TRAVEL CREDIT $300/YEAR,Fees & Adjustments,Adjustment,202.26,\n"
                b"9783,09/20/2026,09/20/2026,Payment Thank You - Web,,Payment,4000.00,\n"
                b"9783,09/21/2026,09/21/2026,ANNUAL MEMBERSHIP FEE,Fees & Adjustments,Fee,-795.00,\n")
        rows = [(t["type"], t["amount"], t["currency"]) for t in statements.parse_statement("x.csv", data)["transactions"]]
        self.assertEqual(rows, [("credit", -202.26, "USD"), ("payment", -4000.0, "USD"), ("fee", 795.0, "USD")])

    def test_chase_without_its_memo_column_is_still_chase(self):
        data = (b"Transaction Date,Post Date,Description,Category,Type,Amount\n"
                b"10/19/2026,10/21/2026,R&amp;R CORP,Travel,Sale,-188.40\n")
        t = statements.parse_statement("US Chase4692_x.csv", data)["transactions"][0]
        self.assertEqual((t["currency"], t["amount"], t["description"], t["supplier"]), ("USD", 188.4, "R&R CORP", "R&R CORP"))

    def test_rbc_all_accounts_download_keeps_the_card_lines_only(self):
        data = (b"Account Type,Account Number,Transaction Date,Cheque Number,Description 1,Description 2,CAD$,USD$\n"
                b"Chequing,00102-1234567,2/17/2026,,ONLINE BANKING PAYMENT - 0000,RBC VISA,-1009.02,\n"
                b"Chequing,00102-1234567,2/18/2026,,PAYROLL DEPOSIT,,5000.00,\n"
                b"Visa,4516070100670000,2/19/2026,,PETRO-CANADA 00453,,-109.98,\n")
        r = statements.parse_statement("x.csv", data)
        self.assertEqual([(t["card_last4"], t["amount"]) for t in r["transactions"]], [("0000", 109.98)])
        self.assertIn("2 lines of a chequing or savings account left out.", r["warnings"])
        with self.assertRaises(statements.StatementError) as ctx:
            statements.parse_statement("x.csv", b"\n".join(data.split(b"\n")[:3]))
        self.assertIn("only chequing or savings", str(ctx.exception))

    def test_rbc_foreign_purchase_keeps_its_original_amount(self):
        data = (b"Account Type,Account Number,Transaction Date,Cheque Number,Description 1,Description 2,CAD$,USD$\n"
                b"Visa,4516070100675018,4/9/2026,,Dyson Direct Inc Chicago,213.99 USD @ 1.42801,-305.58,\n"
                b"Visa,4516070100675018,4/12/2026,,Vrbo Fee 512-759-0902,187.0 USD @ 1.362887,254.86,\n")
        rows = [(t["type"], t["amount"], t["source_amount"], t["source_currency"], t["description"])
                for t in statements.parse_statement("x.csv", data)["transactions"]]
        self.assertEqual(rows, [("purchase", 305.58, 213.99, "USD", "Dyson Direct Inc Chicago"),
                                ("credit", -254.86, -187.0, "USD", "Vrbo Fee 512-759-0902")])

    def test_month_first_dates_the_file_proves_need_no_warning(self):
        data = b"Date,Description,Amount\n1/19/2026,A,10.00\n2/3/2026,B,20.00\n"
        self.assertFalse([w for w in statements.parse_statement("visa_1234.csv", data)["warnings"] if "month/day" in w])
        data = b"Date,Description,Amount\n1/9/2026,A,10.00\n2/3/2026,B,20.00\n"
        self.assertTrue([w for w in statements.parse_statement("visa_1234.csv", data)["warnings"] if "month/day" in w])


class DocxUploadTests(ApiTestCase):
    def test_uploading_a_word_export_adds_its_card_and_lines(self):
        r = self.upload(docx(EXPORT_LINES), name="NFG_7777_Apr_1-Oct_21_2026.docx")
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertEqual((body["transactions"], body["new"]), (6, 6))
        self.assertIn("7777", [c["last4"] for c in self.store.list_cards()])
        again = self.upload(docx(EXPORT_LINES), name="NFG_7777_Apr_1-Oct_21_2026.docx").get_json()
        self.assertEqual(again["new"], 0)


if __name__ == "__main__":
    unittest.main()
