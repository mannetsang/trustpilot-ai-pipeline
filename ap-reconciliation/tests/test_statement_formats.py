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
