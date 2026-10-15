"""Receipts added from the dashboard, the server core: checking the upload (receipts.py), the
Drive client's new writes (drive.py), the invoice row of an upload and the "gone" fix
(invoices.py, store.py), the checks shown next to an uploaded receipt and the month-wide
matcher's compare-and-set (matching.py).

No Pillow: the JPEG fixtures are tiny real files kept as base64 (baseline, progressive,
greyscale, CMYK, and a tall strip), made once with an image library outside this repo."""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import FakeDrive, FakeGemini, file_, folder, inv, quiet, txn  # noqa: E402  (sets the env)

import drive as drive_module  # noqa: E402
import invoices  # noqa: E402
import matching  # noqa: E402
import receipts  # noqa: E402
import store as store_module  # noqa: E402
from receipts import ReceiptError  # noqa: E402
from store import INVOICE_FIELDS, MemoryStore, StoreError, SupabaseStore  # noqa: E402

BASELINE = base64.b64decode(  # 16 x 8, RGB, baseline (SOF0)
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABsSFBcUERsXFhceHBsgKEIrKCUlKFE6PTBCYFVlZF9VXVtqeJmBanGQc1tdhbWGkJ6jq62rZ4C8ybqm"
    "x5moq6T/2wBDARweHigjKE4rK06kbl1upKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKT/wAARCAAIABAD"
    "ASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKB"
    "kaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZ"
    "mqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQF"
    "BgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
    "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX"
    "2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCD+wbr/npD+Z/wrdtozFbRRtglECnHsKKK0IP/2Q==")
PROGRESSIVE = base64.b64decode(  # 8 x 16, RGB, progressive (SOF2)
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABsSFBcUERsXFhceHBsgKEIrKCUlKFE6PTBCYFVlZF9VXVtqeJmBanGQc1tdhbWGkJ6jq62rZ4C8ybqm"
    "x5moq6T/2wBDARweHigjKE4rK06kbl1upKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKT/wgARCAAQAAgD"
    "ASIAAhEBAxEB/8QAFgABAQEAAAAAAAAAAAAAAAAAAAME/8QAFAEBAAAAAAAAAAAAAAAAAAAAAv/aAAwDAQACEAMQAAABm3Cv/8QAGBAAAgMAAAAA"
    "AAAAAAAAAAAAAAECBDL/2gAIAQEAAQUCr7UUj//EABURAQEAAAAAAAAAAAAAAAAAAAAB/9oACAEDAQE/AY//xAAVEQEBAAAAAAAAAAAAAAAAAAAA"
    "Af/aAAgBAgEBPwGv/8QAFxAAAwEAAAAAAAAAAAAAAAAAAAEhgf/aAAgBAQAGPwLCJH//xAAYEAACAwAAAAAAAAAAAAAAAAAAARExcf/aAAgBAQAB"
    "PyGwMpSxH//aAAwDAQACAAMAAAAQD//EABYRAQEBAAAAAAAAAAAAAAAAAAERAP/aAAgBAwEBPxAQm//EABYRAQEBAAAAAAAAAAAAAAAAAAEAEf/a"
    "AAgBAgEBPxBa3//EABgQAAMBAQAAAAAAAAAAAAAAAAABEaGx/9oACAEBAAE/ENbqKIMlQj//2Q==")
GREY = base64.b64decode(  # 8 x 8, one component
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABsSFBcUERsXFhceHBsgKEIrKCUlKFE6PTBCYFVlZF9VXVtqeJmBanGQc1tdhbWGkJ6jq62rZ4C8ybqm"
    "x5moq6T/wAALCAAIAAgBAREA/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEG"
    "E1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJ"
    "ipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/AH6Zp01pcNJI"
    "yEFNvyk+o9q//9k=")
CMYK = base64.b64decode(  # 8 x 8, four components, Adobe APP14 marker first
    "/9j/7gAOQWRvYmUAZAAAAAAA/9sAQwAbEhQXFBEbFxYXHhwbIChCKyglJShROj0wQmBVZWRfVV1baniZgWpxkHNbXYW1hpCeo6utq2eAvMm6pseZ"
    "qKuk/8AAFAgACAAIBEMRAE0RAFkRAEsRAP/EAB8AAAEFAQEBAQEBAAAAAAAAAAABAgMEBQYHCAkKC//EALUQAAIBAwMCBAMFBQQEAAABfQECAwAE"
    "EQUSITFBBhNRYQcicRQygZGhCCNCscEVUtHwJDNicoIJChYXGBkaJSYnKCkqNDU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6"
    "g4SFhoeIiYqSk5SVlpeYmZqio6Slpqeoqaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz9PX29/j5+v/aAA4EQwBNAFkA"
    "SwAAPwDpAMVtO4YYGa0LidZUCqCDnPNdDFGUbJI6V//Z")
TALL = base64.b64decode(  # 8 x 320: 595 pt wide it would be 23800 pt tall
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABsSFBcUERsXFhceHBsgKEIrKCUlKFE6PTBCYFVlZF9VXVtqeJmBanGQc1tdhbWGkJ6jq62rZ4C8ybqm"
    "x5moq6T/2wBDARweHigjKE4rK06kbl1upKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKT/wAARCAFAAAgD"
    "ASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKB"
    "kaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZ"
    "mqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQF"
    "BgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
    "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX"
    "2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwBnhb/kIyf9cj/MUUeFv+QjJ/1yP8xRTe4kHhb/AJCMn/XI/wAxRR4W/wCQjJ/1yP8A"
    "MUUPcEHhb/kIyf8AXI/zFFHhb/kIyf8AXI/zFFD3BB4W/wCQjJ/1yP8AMUUeFv8AkIyf9cj/ADFFD3BB4W/5CMn/AFyP8xRR4W/5CMn/AFyP8xRQ"
    "9wQeFv8AkIyf9cj/ADFFHhb/AJCMn/XI/wAxRQ9wQeFv+QjJ/wBcj/MUUeFv+QjJ/wBcj/MUUPcEHhb/AJCMn/XI/wAxRR4W/wCQjJ/1yP8AMUUP"
    "cEHhb/kIyf8AXI/zFFHhb/kIyf8AXI/zFFD3BB4W/wCQjJ/1yP8AMUUeFv8AkIyf9cj/ADFFD3BHSRW1vC26KCKNiMZVADiipaKQwooooAKKKKAC"
    "iiigAooooAKKKKACiiigAooooAKKKKACiiigD//Z")
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 20
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 20
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 20
HEIC_MIF1 = b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1heic" + b"\x00" * 20
PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\ntrailer\n<< >>\n%%EOF\n"
MB = 1024 * 1024


def receipt_txn(description, supplier="", city="", **extra):
    return dict({"description": description, "supplier": supplier, "city": city}, **extra)


# ---------------------------------------------------------------------------
# The upload: types, JPEG headers, the merged PDF
# ---------------------------------------------------------------------------
class SniffTests(unittest.TestCase):
    def test_known_types_by_their_magic_bytes(self):
        self.assertEqual(receipts.sniff(BASELINE), "image/jpeg")
        self.assertEqual(receipts.sniff(PROGRESSIVE), "image/jpeg")
        self.assertEqual(receipts.sniff(PNG), "image/png")
        self.assertEqual(receipts.sniff(WEBP), "image/webp")
        self.assertEqual(receipts.sniff(HEIC), "image/heic")
        self.assertEqual(receipts.sniff(HEIC_MIF1), "image/heic")
        self.assertEqual(receipts.sniff(PDF), "application/pdf")

    def test_anything_else_is_none(self):
        for data in (b"GIF89a\x01\x00\x01\x00", b"<!doctype html><html>receipt.jpg</html>", b"PK\x03\x04zipzip",
                     b"", b"\xff\xd8", b"RIFF\x00\x00\x00\x00WAVEfmt ", b"\x00\x00\x00\x18ftypavif\x00\x00",
                     None):
            self.assertIsNone(receipts.sniff(data), data)


class JpegInfoTests(unittest.TestCase):
    def test_baseline_progressive_and_greyscale(self):
        self.assertEqual(receipts.jpeg_info(BASELINE), (16, 8, 3))
        self.assertEqual(receipts.jpeg_info(PROGRESSIVE), (8, 16, 3))
        self.assertEqual(receipts.jpeg_info(GREY), (8, 8, 1))
        self.assertEqual(receipts.jpeg_info(TALL), (8, 320, 3))

    def test_fill_bytes_before_a_marker_are_skipped(self):
        self.assertEqual(receipts.jpeg_info(BASELINE[:2] + b"\xff\xff" + BASELINE[2:]), (16, 8, 3))

    def test_unreadable_headers_raise_415(self):
        sof = BASELINE.index(b"\xff\xc0")
        too_wide = BASELINE[:sof + 7] + (10001).to_bytes(2, "big") + BASELINE[sof + 9:]
        for data in (BASELINE[:sof + 6], BASELINE[:60], BASELINE[:2], PNG, PDF, b"\xff\xd8\xff\xd9", CMYK, too_wide):
            with self.assertRaises(ReceiptError) as caught:
                receipts.jpeg_info(data)
            self.assertEqual(caught.exception.status, 415)
        with self.assertRaisesRegex(ReceiptError, "greyscale"):
            receipts.jpeg_info(CMYK)


def pdf_objects(pdf):
    """{object number: body} and the checks of the cross-reference table: every offset must point at
    'N 0 obj' and startxref at 'xref'."""
    startxref = int(re.search(rb"startxref\n(\d+)\n%%EOF\n$", pdf).group(1))
    assert pdf[startxref:startxref + 5] == b"xref\n", "startxref does not point at the xref table"
    table = pdf[startxref:].split(b"trailer")[0].split(b"\n")
    first, count = (int(x) for x in table[1].split())
    entries = table[2:2 + count]
    assert first == 0 and entries[0] == b"0000000000 65535 f ", entries[0]
    objects = {}
    for number, entry in enumerate(entries[1:], 1):
        assert len(entry) == 19 and entry.endswith(b" 00000 n "), entry
        offset = int(entry[:10])
        head = f"{number} 0 obj\n".encode()
        assert pdf[offset:offset + len(head)] == head, f"object {number} is not at {offset}"
        objects[number] = pdf[offset + len(head):pdf.index(b"\nendobj\n", offset)]
    assert re.search(rb"/Size %d\b" % count, pdf)
    return objects


class JpegsToPdfTests(unittest.TestCase):
    def setUp(self):
        self.pdf = receipts.jpegs_to_pdf([BASELINE, GREY])

    def test_header_trailer_and_pages(self):
        self.assertTrue(self.pdf.startswith(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"))
        self.assertTrue(self.pdf.endswith(b"%%EOF\n"))
        self.assertIn(b"/Type /Pages /Kids [3 0 R 6 0 R] /Count 2", self.pdf)
        self.assertEqual(self.pdf.count(b"/Filter /DCTDecode"), 2)
        self.assertEqual(self.pdf.count(b"/Type /Page "), 2)

    def test_each_jpeg_is_embedded_byte_for_byte(self):
        objects = pdf_objects(self.pdf)
        self.assertEqual(len(objects), 8)          # catalog, pages, then page + image + content per page
        for number, jpg, space, size in ((4, BASELINE, b"DeviceRGB", (16, 8)), (7, GREY, b"DeviceGray", (8, 8))):
            image = objects[number]
            self.assertIn(b"/Subtype /Image /Width %d /Height %d " % size, image)
            self.assertIn(b"/ColorSpace /" + space + b" /BitsPerComponent 8", image)
            self.assertIn(b"/Length %d >>" % len(jpg), image)
            self.assertEqual(image.split(b"stream\n", 1)[1], jpg + b"\nendstream")

    def test_page_size_and_the_cap_on_very_long_receipts(self):
        objects = pdf_objects(self.pdf)
        self.assertIn(b"/MediaBox [0 0 595 297.5]", objects[3])                  # 16 x 8 -> 595 wide
        self.assertIn(b"/MediaBox [0 0 595 595]", objects[6])
        self.assertEqual(objects[5], b"<< /Length 32 >>\nstream\nq 595 0 0 297.5 0 0 cm /Im0 Do Q\nendstream")
        tall = pdf_objects(receipts.jpegs_to_pdf([TALL]))
        self.assertIn(b"/MediaBox [0 0 360 14400]", tall[3])                     # proportions kept, narrower
        self.assertIn(b"q 360 0 0 14400 0 0 cm /Im0 Do Q", tall[5])

    def test_nothing_from_the_user_but_the_jpeg_bytes(self):
        text = self.pdf.replace(BASELINE, b"").replace(GREY, b"")
        self.assertNotIn(b"receipt", text.lower())
        self.assertNotIn(b"/Info", text)

    def test_a_pdf_reader_accepts_it(self):
        """An extra check with poppler's pdfinfo and pdfimages when this machine has them."""
        if not shutil.which("pdfinfo"):
            self.skipTest("pdfinfo is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "receipt.pdf")
            with open(path, "wb") as f:
                f.write(receipts.jpegs_to_pdf([BASELINE, PROGRESSIVE, GREY]))
            info = subprocess.run(["pdfinfo", path], capture_output=True, text=True, timeout=30)
            self.assertEqual(info.returncode, 0, info.stderr)
            self.assertRegex(info.stdout, r"Pages:\s+3")
            self.assertRegex(info.stdout, r"PDF version:\s+1\.4")
            self.assertNotIn("Syntax Error", info.stderr)
            if shutil.which("pdfimages"):
                listed = subprocess.run(["pdfimages", "-list", path], capture_output=True, text=True, timeout=30)
                rows = [line.split() for line in listed.stdout.splitlines()[2:] if line.strip()]
                self.assertEqual([(r[3], r[4], r[5], r[8]) for r in rows],
                                 [("16", "8", "rgb", "jpeg"), ("8", "16", "rgb", "jpeg"), ("8", "8", "gray", "jpeg")])


class PrepareUploadTests(unittest.TestCase):
    def refused(self, parts, status, message=None):
        with self.assertRaises(ReceiptError) as caught:
            receipts.prepare_upload(parts)
        self.assertEqual(caught.exception.status, status, caught.exception.message)
        if message:
            self.assertEqual(caught.exception.message, message)

    def test_one_file_is_kept_as_it_is(self):
        self.assertEqual(receipts.prepare_upload([("IMG_0001.HEIC", BASELINE)]), (BASELINE, "image/jpeg", "jpg", 1))
        self.assertEqual(receipts.prepare_upload([("scan.jpg", PDF)]), (PDF, "application/pdf", "pdf", 1))
        self.assertEqual(receipts.prepare_upload([("x", HEIC)])[1:], ("image/heic", "heic", 1))
        self.assertEqual(receipts.prepare_upload([("x", PNG)])[1:], ("image/png", "png", 1))
        self.assertEqual(receipts.prepare_upload([("x", WEBP)])[1:], ("image/webp", "webp", 1))

    def test_several_photos_become_one_pdf(self):
        data, mime, ext, pages = receipts.prepare_upload([("1.jpg", BASELINE), ("2.jpg", PROGRESSIVE)])
        self.assertEqual((mime, ext, pages), ("application/pdf", "pdf", 2))
        self.assertEqual(data, receipts.jpegs_to_pdf([BASELINE, PROGRESSIVE]))

    def test_refusals(self):
        self.refused([], 400, "Attach the receipt as one or more 'file' parts.")
        self.refused([("a.jpg", BASELINE), ("b.jpg", b"")], 400, "The file is empty.")
        self.refused([("p.jpg", BASELINE)] * 11, 400, "At most 10 pages per receipt.")
        self.refused([("a.pdf", PDF), ("b.jpg", BASELINE)], 400, "Several pages must all be photos; add a PDF on its own.")
        self.refused([("a.png", PNG), ("b.jpg", BASELINE)], 400, "Several pages must all be photos; add a PDF on its own.")
        self.refused([("notes.txt", b"Total 23.45")], 415,
                     "That file isn't a photo or a PDF. Choose a JPEG, PNG, WebP or HEIC photo, or a PDF.")
        self.refused([("a.jpg", BASELINE), ("b.jpg", b"\xff\xd8\xff\xe0 broken"), ("c.jpg", BASELINE)], 415,
                     "Page 2 could not be read as a photo; take it again.")
        self.refused([("a.jpg", BASELINE), ("b.jpg", CMYK)], 415, "Page 2 could not be read as a photo; take it again.")

    def test_size_limits(self):
        self.assertEqual(receipts.prepare_upload([("big.jpg", BASELINE + b"\0" * (7 * MB - len(BASELINE)))])[1], "image/jpeg")
        self.refused([("big.jpg", BASELINE + b"\0" * (7 * MB + 1 - len(BASELINE)))], 413, "That photo is larger than 7 MB.")
        self.refused([("big.heic", HEIC + b"\0" * (7 * MB))], 413, "That photo is larger than 7 MB.")
        self.refused([("big.pdf", PDF + b"\0" * (15 * MB + 1 - len(PDF)))], 413, "That PDF is larger than 15 MB.")
        self.refused([("a.jpg", BASELINE), ("big.jpg", BASELINE + b"\0" * (7 * MB))], 413, "That photo is larger than 7 MB.")
        # Each photo is within 7 MB (bytes after the end-of-image marker are kept as they are), together over 15 MB.
        page = BASELINE + b"\0" * (5 * MB + 400 * 1024)
        self.refused([("1.jpg", page), ("2.jpg", page), ("3.jpg", page)], 413,
                     "These pages come to more than 15 MB as one PDF; remove a page.")


# ---------------------------------------------------------------------------
# Folder and file names
# ---------------------------------------------------------------------------
class SafeFolderNameTests(unittest.TestCase):
    def test_names_are_made_safe(self):
        self.assertEqual(receipts.safe_folder_name("Thai Express"), "Thai Express")
        self.assertEqual(receipts.safe_folder_name("a/b"), "a-b")
        self.assertEqual(receipts.safe_folder_name("x\\"), "x")
        self.assertEqual(receipts.safe_folder_name('Q: "A" <b> | c?*'), 'Q- -A- -b- - c')
        self.assertEqual(receipts.safe_folder_name("Uber\x00​\tEats\n"), "Uber Eats")
        self.assertEqual(receipts.safe_folder_name("  Uber   Eats. "), "Uber Eats")
        self.assertEqual(receipts.safe_folder_name("Café Ñ"), "Café Ñ")       # NFC
        self.assertEqual(receipts.safe_folder_name("x" * 300), "x" * 60)

    def test_names_that_are_refused(self):
        for name in ("", None, ".", "..", " . ", "...", "../../x", "./Uber", "--", "#!?", "2026 09", "2026-09", "2026_9"):
            self.assertIsNone(receipts.safe_folder_name(name), name)


class VendorFolderTests(unittest.TestCase):
    def suggest(self, description, this_month=(), earlier=None, supplier="", city=""):
        return receipts.suggest_vendor_folder(receipt_txn(description, supplier, city), list(this_month), earlier or {})

    def test_the_examples(self):
        cases = [
            ("Uber* Trip", ["Uber", "Uber Eats"], {}, ("Uber", "this_month")),
            ("Uber* Trip", ["Uber Eats"], {}, ("Uber", "new")),
            ("Uber* Eats", ["Uber", "Uber Eats"], {}, ("Uber Eats", "this_month")),
            ("Google *cloud Pcx2zf", ["Google Ads"], {}, ("Google Cloud", "new")),
            ("Google *cloud Pcx2zf", ["Google Ads"], {"Google Cloud": (3, "2026 07")}, ("Google Cloud", "earlier_month")),
            ("Google *ads1234567", ["Google Ads"], {}, ("Google Ads", "this_month")),
            ("Air Can*", [], {}, ("Air Canada", "new")),
            ("Air Can*", [], {"Air Canada": (2, "2026 05")}, ("Air Canada", "earlier_month")),
            ("AIRCANADA*ABC123", [], {"Air Canada": (2, "2026 05")}, ("Air Canada", "earlier_month")),
            ("Mcdonald's #400", [], {}, ("McDonald's", "new")),
            ("Openai *chatgpt Subscr", [], {}, ("OpenAI", "new")),
            ("Chatgpt Subscription", [], {}, ("OpenAI", "new")),
            ("Amzn Mktp Ca*AB12C", [], {}, ("Amazon", "new")),
            ("Www.Retellai.Com", [], {}, ("Retellai", "new")),
            ("The Home Depot #7001", [], {}, ("The Home Depot", "new")),
            ("The Home Depot #7001", [], {"The Home Depot": (2, "2026 08")}, ("The Home Depot", "earlier_month")),
            ("Telus Mobility Preauth", [], {"TELUS Mobility": (4, "2026 08")}, ("TELUS Mobility", "earlier_month")),
            ("Telus Communications", [], {"TELUS Mobility": (4, "2026 08")}, ("Telus Communications", "new")),
            ("PAYPAL *SHOPIFY", [], {}, ("Shopify", "new")),
            ("Tim Hortons #123", [], {}, ("Tim Hortons", "new")),
            ("#4012 12345", [], {}, ("Receipts", "new")),
            ("Anthropic* Claude Team", ["Anthropic"], {}, ("Anthropic", "this_month")),
            ("Costco Wholesale #535", ["Costco"], {}, ("Costco", "this_month")),
        ]
        for description, this_month, earlier, expected in cases:
            self.assertEqual(self.suggest(description, this_month, earlier), expected, description)
        self.assertEqual(self.suggest("Sq *coffee Shop Toronto", ["Sushi Shop"], city="Toronto"), ("Coffee Shop", "new"))
        self.assertEqual(self.suggest("Thai Express", ["Thai Express", "Sushi Shop"], city="Toronto"),
                         ("Thai Express", "this_month"))

    def test_clean_vendor_name(self):
        self.assertEqual(receipts.clean_vendor_name("Uber* Eats"), "Uber")
        self.assertEqual(receipts.clean_vendor_name("Sq *coffee Shop Toronto", city="Toronto"), "Coffee Shop")
        self.assertEqual(receipts.clean_vendor_name("Google *ads1234567"), "Google")
        self.assertEqual(receipts.clean_vendor_name("Air Can*"), "Air Canada")
        self.assertEqual(receipts.clean_vendor_name("Mcdonald's #400"), "McDonald's")
        self.assertEqual(receipts.clean_vendor_name("TIM'S BAKERY"), "Tim's Bakery")
        self.assertEqual(receipts.clean_vendor_name("Big Long Name Of The Shop"), "Big Long Name Of")
        self.assertEqual(receipts.clean_vendor_name("#4012 12345", supplier="Tim Hortons #12"), "Tim Hortons")
        self.assertEqual(receipts.clean_vendor_name("", supplier=""), "Receipts")
        self.assertEqual(receipts.clean_vendor_name("2026 10"), "Receipts")

    def test_this_month_beats_an_earlier_month_and_specific_beats_general(self):
        self.assertEqual(self.suggest("Uber* Eats", ["Uber"], {"Uber Eats": (9, "2026 08")}), ("Uber", "this_month"))
        self.assertEqual(self.suggest("Uber* Eats", [], {"Uber": (9, "2026 08"), "Uber Eats": (1, "2026 01")}),
                         ("Uber Eats", "earlier_month"))
        # Equally specific: more files, then the newer month.
        self.assertEqual(self.suggest("Uber* Trip", [], {"UBER": (2, "2026 08"), "Uber": (5, "2026 01")}),
                         ("Uber", "earlier_month"))
        self.assertEqual(self.suggest("Uber* Trip", [], {"UBER": (2, "2026 08"), "Uber": (2, "2025 12")}),
                         ("UBER", "earlier_month"))

    def test_a_snap_keeps_the_existing_spelling(self):
        self.assertEqual(self.suggest("AIRCANADA*ABC123", ["air-canada"]), ("air-canada", "this_month"))
        self.assertEqual(receipts.snap_folder_name("uber", ["Uber", "Uber Eats"], ["UBER"]), ("Uber", True))
        self.assertEqual(receipts.snap_folder_name("uber eats", ["Uber"], ["Uber-Eats"]), ("Uber-Eats", False))
        self.assertEqual(receipts.snap_folder_name("Lyft", ["Uber"], ["Uber Eats"]), ("Lyft", False))
        self.assertEqual(receipts.snap_folder_name("東京", ["京都"], []), ("東京", False))   # no a-z0-9 to compare
        self.assertEqual(receipts.snap_folder_name("東京", ["東京"], []), ("東京", True))

    def test_folder_matches(self):
        charge = receipts.name_tokens("Air Can*")
        self.assertTrue(receipts.folder_matches("Air Canada", charge))           # truncated descriptor
        self.assertFalse(receipts.folder_matches("AB", receipts.name_tokens("Ab Cd")))  # no word of 3 letters
        self.assertFalse(receipts.folder_matches("Receipts", charge))
        self.assertEqual(receipts.name_tokens("Google *ads1234567 Toronto", "Toronto"), {"google", "ads"})


class FileNameTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(receipts.receipt_file_name("2026-09-04", "Uber", "jpg", []), "2026-09-04 Uber receipt.jpg")
        self.assertEqual(receipts.receipt_file_name("2026-09-04", "Uber", "pdf", ["2026-09-04 Uber receipt.jpg"]),
                         "2026-09-04 Uber receipt.pdf")
        taken = ["2026-09-04 Uber receipt.jpg", "2026-09-04 uber receipt (2).JPG"]
        self.assertEqual(receipts.receipt_file_name("2026-09-04", "Uber", "jpg", taken[:1]), "2026-09-04 Uber receipt (2).jpg")
        self.assertEqual(receipts.receipt_file_name("2026-09-04", "Uber", "jpg", taken), "2026-09-04 Uber receipt (3).jpg")

    def test_never_the_amount_or_the_card_and_at_most_120_characters(self):
        t = txn("t1", "2026-09-04", "Uber* Trip", 23.45, card="4421")
        name = receipts.receipt_file_name(t["txn_date"], "Uber", "jpg", [])
        for secret in ("23", "45", "4421"):
            self.assertNotIn(secret, name)
        long_name = receipts.receipt_file_name("2026-09-04", "V" * 200, "jpg", [])
        self.assertEqual(len(long_name), 120)
        self.assertTrue(long_name.endswith(" receipt.jpg"))
        self.assertLessEqual(len(receipts.receipt_file_name("2026-09-04", "V" * 200, "jpg", [long_name])), 120)

    def test_drive_description(self):
        t = txn("t1", "2026-09-04", "Uber* Trip", 1234.5, card="1610")
        text = receipts.drive_description(t, "bo@superhairpieces.com", "2026-10-15T12:00:00Z")
        self.assertEqual(text, "Receipt for Uber* Trip (CAD 1,234.50) on 2026-09-04, card ending 1610. Uploaded from "
                               "the AP dashboard by bo@superhairpieces.com on 2026-10-15T12:00:00Z.")
        self.assertLessEqual(len(receipts.drive_description(dict(t, description="x" * 900), "a@b.c", "now")), 500)

    def test_upload_id_pattern(self):
        self.assertTrue(receipts.UPLOAD_ID_RE.match("0f8b1c2e-6a3d-4c5e-9f00-123456789abc"))
        for bad in ("short", "x" * 65, "abc def ghi", "../../etc/passwd"):
            self.assertIsNone(receipts.UPLOAD_ID_RE.match(bad), bad)


# ---------------------------------------------------------------------------
# matching.invoice_checks
# ---------------------------------------------------------------------------
class InvoiceChecksTests(unittest.TestCase):
    def setUp(self):
        self.txn = txn("t1", "2026-09-04", "Uber* Trip", 47.20, card="4421")
        self.inv = inv("i1", "Uber", 47.20, "2026-09-04", card="4421")

    def checks(self, txn_extra=None, **inv_extra):
        return matching.invoice_checks(dict(self.txn, **(txn_extra or {})), dict(self.inv, **inv_extra))

    def test_everything_agrees(self):
        self.assertEqual(self.checks(), [])
        self.assertEqual(self.checks(total=47.80), [])               # within 2%
        self.assertEqual(self.checks(card_last4=None, invoice_date=None, currency=None), [])
        self.assertEqual(matching.invoice_checks(self.txn, None), [])

    def test_not_read_yet(self):
        unread = dict(extracted_at=None, extraction_error=None, total=None, vendor=None, invoice_date=None, card_last4=None)
        self.assertEqual(self.checks(**unread), [{"kind": "unread", "level": "info", "text": "Receipt not read yet."}])
        self.assertEqual(matching.invoice_checks(self.txn, dict(self.inv, **unread), reading=True),
                         [{"kind": "unread", "level": "info", "text": "Reading the receipt…"}])
        self.assertEqual(self.checks(extracted_at="2026-10-15T12:00:00Z", extraction_error="Gemini 503", total=None),
                         [{"kind": "unreadable", "level": "info",
                           "text": "Couldn't read the receipt yet; it is read again with the next invoice run."}])
        self.assertEqual(self.checks(extracted_at=None, total=None,
                                     extraction_error="upload withdrawn: the purchase changed while it was saving"), [])

    def test_not_a_receipt_and_another_card(self):
        self.assertEqual(self.checks(is_invoice=False, summary="a bank statement"),
                         [{"kind": "not_invoice", "level": "warn",
                           "text": "This doesn't look like a receipt (a bank statement)."}])
        self.assertEqual(self.checks(card_last4="1610"),
                         [{"kind": "card", "level": "warn", "text": "The receipt shows card ending 1610, not 4421."}])

    def test_amounts(self):
        self.assertEqual(self.checks(total=45.00),                   # 4.7% off: a tip or a tax, info
                         [{"kind": "amount", "level": "info", "text": "Receipt reads CAD 45.00; the charge is CAD 47.20."}])
        self.assertEqual(self.checks(total=40.00),
                         [{"kind": "amount", "level": "info", "text": "Receipt reads CAD 40.00; the charge is CAD 47.20."}])
        self.assertEqual(self.checks(total=23.45),                   # half: probably another receipt
                         [{"kind": "amount", "level": "warn", "text": "Receipt reads CAD 23.45; the charge is CAD 47.20."}])
        self.assertEqual(self.checks({"amount": 61.20}, total=45.00, currency="USD"),
                         [{"kind": "amount", "level": "info", "text": "Receipt is in USD 45.00; the charge is CAD 61.20 "
                                                                     "with no USD amount to compare."}])

    def test_a_foreign_currency_receipt_is_compared_with_the_foreign_amount(self):
        foreign = {"amount": 61.20, "source_amount": 45.00, "source_currency": "USD"}
        self.assertEqual(self.checks(foreign, total=45.00, currency="USD"), [])
        self.assertEqual(self.checks(foreign, total=20.00, currency="USD"),
                         [{"kind": "amount", "level": "warn", "text": "Receipt reads USD 20.00; the charge is USD 45.00."}])

    def test_dates(self):
        self.assertEqual(self.checks(invoice_date="2026-08-20"), [])          # 15 days: within the window
        self.assertEqual(self.checks(invoice_date="2026-07-01"),
                         [{"kind": "date", "level": "warn", "text": "The receipt is dated 2026-07-01, 65 days from the charge."}])

    def test_several_at_once_in_a_fixed_order(self):
        kinds = [c["kind"] for c in self.checks(is_invoice=False, summary="x", card_last4="1610", total=1.0,
                                                invoice_date="2026-01-01")]
        self.assertEqual(kinds, ["not_invoice", "card", "amount", "date"])


# ---------------------------------------------------------------------------
# drive.Drive: the new writes, through a fake `requests`
# ---------------------------------------------------------------------------
class FakeHttpResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.headers = status, headers or {}
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)
        self.content = self.text.encode()

    def json(self):
        return self._body


class DriveClientTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.responses = [], []
        self.drive = drive_module.Drive("client", "secret", "refresh")
        patcher = mock.patch.object(drive_module.Drive, "token", return_value="tok")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(drive_module.requests, "request", self.fake_request)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_request(self, method, url, params=None, headers=None, timeout=None, **kwargs):
        self.calls.append(dict(kwargs, method=method, url=url, params=dict(params or {}), headers=dict(headers or {})))
        return self.responses.pop(0) if self.responses else FakeHttpResponse(body={"id": "new", "files": []})

    def test_search_values_escape_the_backslash_then_the_quote(self):
        self.assertEqual(drive_module._q("O'Brien \\ Co"), "O\\'Brien \\\\ Co")
        self.assertEqual(drive_module._q("a\\'b"), "a\\\\\\'b")
        self.responses = [FakeHttpResponse(body={"files": [{"id": "f1", "name": "x"}]})]
        self.assertEqual(self.drive.find_child_folder("root", "Tim's \\ Bakery"), "f1")
        self.assertEqual(self.calls[0]["params"]["q"],
                         "'root' in parents and name = 'Tim\\'s \\\\ Bakery' "
                         "and mimeType = 'application/vnd.google-apps.folder' and trashed = false")

    def test_get_takes_the_fields(self):
        self.drive.get("f1", fields="id,appProperties")
        self.drive.get("f2")
        self.assertEqual([c["params"]["fields"] for c in self.calls], ["id,appProperties", drive_module.FILE_FIELDS])
        self.assertTrue(self.calls[0]["url"].endswith("/files/f1"))

    def test_a_small_upload_is_one_multipart_request_with_the_metadata(self):
        self.responses = [FakeHttpResponse(body={"id": "up1", "name": "r.jpg"})]
        meta = self.drive.upload("vendor", "2026-09-04 Uber receipt.jpg", BASELINE, "image/jpeg",
                                 description="Receipt for Uber* Trip", app_properties={"apTxn": "t1", "apUpload": "u" * 10})
        self.assertEqual(meta["id"], "up1")
        call = self.calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", drive_module.UPLOAD))
        self.assertEqual(call["params"], {"uploadType": "multipart", "supportsAllDrives": "true",
                                          "fields": "id,name,mimeType,modifiedTime,size,webViewLink"})
        sent = json.loads(call["files"]["metadata"][1])
        self.assertEqual(sent, {"name": "2026-09-04 Uber receipt.jpg", "parents": ["vendor"],
                                "description": "Receipt for Uber* Trip",
                                "appProperties": {"apTxn": "t1", "apUpload": "u" * 10}})
        self.assertEqual(call["files"]["file"], ("2026-09-04 Uber receipt.jpg", BASELINE, "image/jpeg"))
        # The statement copy's call is unchanged: no description, no appProperties.
        self.drive.upload("stmts", "Export.xlsx", b"PK", "")
        self.assertEqual(json.loads(self.calls[1]["files"]["metadata"][1]), {"name": "Export.xlsx", "parents": ["stmts"]})

    def test_a_big_upload_opens_a_resumable_session_and_puts_the_bytes(self):
        data = b"%PDF-" + b"\0" * (5 * MB)
        session = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=abc"
        self.responses = [FakeHttpResponse(headers={"Location": session}),
                          FakeHttpResponse(body={"id": "up2", "size": str(len(data))})]
        meta = self.drive.upload("vendor", "r.pdf", data, "application/pdf", description="d", app_properties={"apTxn": "t"})
        self.assertEqual(meta["id"], "up2")
        start, put = self.calls
        self.assertEqual((start["method"], start["url"]), ("POST", drive_module.UPLOAD))
        self.assertEqual(start["params"]["uploadType"], "resumable")
        self.assertEqual(start["params"]["fields"], "id,name,mimeType,modifiedTime,size,webViewLink")
        self.assertEqual(start["json"], {"name": "r.pdf", "parents": ["vendor"], "description": "d",
                                         "appProperties": {"apTxn": "t"}})
        self.assertEqual((start["headers"]["X-Upload-Content-Type"], start["headers"]["X-Upload-Content-Length"]),
                         ("application/pdf", str(len(data))))
        self.assertEqual((put["method"], put["url"], put["data"]), ("PUT", session, data))
        self.assertEqual(put["headers"]["Content-Type"], "application/pdf")
        # Exactly 5 MB still goes in one multipart request.
        self.calls = []
        self.drive.upload("vendor", "r.pdf", b"\0" * (5 * MB), "application/pdf")
        self.assertEqual([c["params"].get("uploadType") for c in self.calls], ["multipart"])

    def test_a_session_without_a_location_is_an_error(self):
        self.responses = [FakeHttpResponse()]
        with self.assertRaises(drive_module.DriveError):
            self.drive.upload("vendor", "r.pdf", b"\0" * (6 * MB), "application/pdf")

    def test_trash(self):
        self.drive.trash("up1")
        call = self.calls[0]
        self.assertEqual((call["method"], call["json"], call["params"]["supportsAllDrives"]), ("PATCH", {"trashed": True}, "true"))
        self.assertTrue(call["url"].endswith("/files/up1"))


# ---------------------------------------------------------------------------
# Store and index: update_invoices, the "gone" fix, the upload's placeholder row
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, body="[]", headers=None):
        self.status_code, self.text, self.headers = status, body, headers or {}

    def json(self):
        return json.loads(self.text)


class UpdateInvoicesTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.responses = [], []
        self.store = SupabaseStore("https://x.supabase.co", "service-key")

    def fake_request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "table": url.rsplit("/", 1)[1], "params": dict(params or {}),
                           "json": json, "prefer": (headers or {}).get("Prefer", "")})
        return self.responses.pop(0) if self.responses else FakeResponse(status=204, body="")

    def test_supabase_patches_in_chunks_of_100(self):
        ids = [f"f{i}" for i in range(250)]
        with mock.patch.object(store_module.requests, "request", self.fake_request):
            self.store.update_invoices(ids, {"removed_at": "2026-10-15T00:00:00Z"})
            self.store.update_invoices([], {"removed_at": "x"})
        self.assertEqual([c["method"] for c in self.calls], ["PATCH"] * 3)
        self.assertEqual(self.calls[0]["params"]["id"], "in.(" + ",".join(f'"f{i}"' for i in range(100)) + ")")
        self.assertEqual(self.calls[2]["params"]["id"].count('"'), 100)
        self.assertTrue(all(c["json"] == {"removed_at": "2026-10-15T00:00:00Z"} and c["table"] == "invoices"
                            for c in self.calls))

    def test_files_gone_from_drive_are_patched_never_upserted_without_file_name(self):
        known = [{"id": "f_gone", "modified_time": "2026-09-05T10:00:00Z", "extraction_error": None,
                  "month_folder": "2026 09", "removed_at": None},
                 {"id": "f_kept", "modified_time": "2026-09-05T10:00:00Z", "extraction_error": None,
                  "month_folder": "2026 09", "removed_at": None}]
        self.responses = [FakeResponse(body=json.dumps(known), headers={"Content-Range": "0-1/2"})]
        drive = FakeDrive({"root": [folder("m09", "2026 09")], "m09": [file_("f_kept", "kept.pdf")]})
        with mock.patch.object(store_module.requests, "request", self.fake_request):
            result = invoices.index_invoices(self.store, drive, FakeGemini(), "root", log=quiet)
        self.assertEqual((result["removed"], result["indexed"]), (1, 0))
        writes = [c for c in self.calls if c["method"] != "GET"]
        self.assertEqual([(c["method"], c["params"].get("id")) for c in writes],
                         [("POST", None), ("PATCH", 'in.("f_gone")')])
        moved, gone = writes
        self.assertTrue(all("file_name" in row for row in moved["json"]))     # the refresh of f_kept: a full key set
        self.assertEqual(list(gone["json"]), ["removed_at"])

    def test_memory_store_updates_only_rows_it_has(self):
        store = MemoryStore()
        store.upsert_invoices([inv("i1", "Uber", 10.0, "2026-09-04")])
        store.update_invoices(["i1", "nope"], {"removed_at": "2026-10-15T00:00:00Z", "extraction_error": "x"})
        self.assertEqual((store.get_invoice("i1")["removed_at"], store.get_invoice("i1")["total"]),
                         ("2026-10-15T00:00:00Z", 10.0))
        self.assertIsNone(store.get_invoice("nope"))


def uploaded_receipt(drive, data=BASELINE, mime="image/jpeg", name="2026-09-04 Uber receipt.jpg"):
    """Put a receipt into Invoice/2026 09/Uber the way the dashboard does; returns (meta, placeholder record)."""
    month = drive.ensure_folder("root", "2026 09")
    vendor = drive.ensure_folder(month, "Uber")
    meta = drive.upload(vendor, name, data, mime, description="Receipt for Uber* Trip",
                        app_properties={"apTxn": "t_uber", "apBy": "bo@x.com", "apUpload": ""})
    record = {"id": meta["id"], "file_name": meta["name"], "mime_type": meta["mimeType"],
              "web_view_link": meta["webViewLink"], "folder_path": "/2026 09/Uber", "month_folder": "2026 09",
              "vendor_folder": "Uber", "size": len(data)}
    return meta, record


class UploadedReceiptIndexTests(unittest.TestCase):
    def setUp(self):
        self.drive = FakeDrive({"root": []})
        self.store = MemoryStore()
        self.gemini = FakeGemini(handler=lambda parts, model: {
            "is_invoice": True, "document_type": "receipt", "vendor": "Uber", "invoice_date": "2026-09-04",
            "total": 23.45, "currency": "CAD", "card_last4": None, "summary": "trip"})

    def test_the_placeholder_has_every_column_and_no_facts(self):
        meta, record = uploaded_receipt(self.drive)
        row = invoices.upload_placeholder(record)
        self.assertEqual(set(row), set(INVOICE_FIELDS))
        self.assertEqual((row["id"], row["file_name"], row["mime_type"], row["folder_path"], row["month_folder"],
                          row["vendor_folder"], row["size"]),
                         (meta["id"], "2026-09-04 Uber receipt.jpg", "image/jpeg", "/2026 09/Uber", "2026 09", "Uber",
                          len(BASELINE)))
        for key in ("modified_time", "total", "extracted_at", "extraction_error", "removed_at", "is_invoice"):
            self.assertIsNone(row[key], key)
        self.store.upsert_invoices([row])
        self.assertEqual(matching.candidate_invoices(self.store, 2026, 9), [])   # no total: no match job pairs it

    def test_reading_the_bytes_in_hand_downloads_nothing(self):
        meta, record = uploaded_receipt(self.drive)
        record = dict(record, modified_time=meta["modifiedTime"], _file=meta)
        row = invoices.extract_invoice(self.gemini, self.drive, record, content=(BASELINE, "image/jpeg"))
        self.assertEqual(self.drive.downloads, [])
        self.assertEqual(self.gemini.calls[0]["parts"][0], {"inlineData": {"mimeType": "image/jpeg", "data": BASELINE}})
        self.assertEqual((row["total"], row["vendor"], row["modified_time"], row["extraction_error"]),
                         (23.45, "Uber", meta["modifiedTime"], None))
        self.assertIn('file name "2026-09-04 Uber receipt.jpg"', self.gemini.prompt_text())
        self.assertNotIn("Receipt for", self.gemini.prompt_text())             # the description stays in Drive

    def test_the_index_reads_an_unread_upload_exactly_once(self):
        _, record = uploaded_receipt(self.drive)
        self.store.upsert_invoices([invoices.upload_placeholder(record)])
        first = invoices.index_invoices(self.store, self.drive, self.gemini, "root", log=quiet)
        self.assertEqual((first["indexed"], first["removed"]), (1, 0))
        self.assertEqual(self.drive.downloads, [record["id"]])
        self.assertEqual(self.store.get_invoice(record["id"])["total"], 23.45)
        second = invoices.index_invoices(self.store, self.drive, self.gemini, "root", log=quiet)
        self.assertEqual(second["indexed"], 0)
        self.assertEqual(len(self.drive.downloads), 1)

    def test_fake_drive_writes_show_in_listings(self):
        meta, _ = uploaded_receipt(self.drive)
        vendor = self.drive.uploads[0]["parent"]
        self.assertEqual([f["name"] for f in self.drive.list_children(vendor)], ["2026-09-04 Uber receipt.jpg"])
        self.assertEqual(self.drive.get(meta["id"], fields="id,appProperties")["appProperties"]["apTxn"], "t_uber")
        self.drive.trash(meta["id"])
        self.assertEqual(self.drive.list_children(vendor), [])
        self.assertEqual(self.drive.trashed, [meta["id"]])
        with self.assertRaisesRegex(drive_module.DriveError, "404"):
            self.drive.get(meta["id"])
        self.assertEqual(self.drive.ensure_folder("root", "2026 09"), self.drive.created_folders[0]["id"])
        self.assertEqual(len(self.drive.created_folders), 2)


# ---------------------------------------------------------------------------
# match_month: compare and set
# ---------------------------------------------------------------------------
class MatchMonthCompareAndSetTests(unittest.TestCase):
    def test_an_upload_made_while_the_matcher_ran_survives(self):
        store = MemoryStore()
        store.insert_transactions([txn("t_uber", "2026-09-04", "Uber* Trip", 23.45)])
        store.upsert_invoices([inv("inv_old", "Uber", 23.45, "2026-09-04")])
        stale = store.list_transactions(2026, 9)
        # Meanwhile the card holder uploads the receipt: matched by hand to the new file.
        store.upsert_invoices([dict(inv("up_1", "Uber", None, None), total=None, extracted_at=None)])
        upload = {"invoice_status": "matched", "invoice_id": "up_1", "match_confidence": 1.0, "match_method": "manual",
                  "match_note": "receipt uploaded by bo@x.com"}
        store.update_transaction("t_uber", upload)
        lines = []
        with mock.patch.object(store, "list_transactions", return_value=stale):
            counts = matching.match_month(store, None, 2026, 9, log=lines.append)
        self.assertEqual(store.get_transaction("t_uber")["invoice_id"], "up_1")
        self.assertEqual(store.get_transaction("t_uber")["match_method"], "manual")
        self.assertEqual((counts["skipped"], counts["matched"], counts["checked"]), (1, 0, 1))
        self.assertEqual(sum(1 for line in lines if line.startswith("skipped")), 1)

    def test_a_released_row_whose_new_link_fails_ends_missing(self):
        store = MemoryStore()
        store.insert_transactions([
            txn("t_dm", "2026-09-05", "Dunder Mifflin Paper", 33.00, status="possible", invoice_id="inv_cafe", method="rule"),
            txn("t_cafe", "2026-09-01", "Sq *Corner Cafe", 33.00),
            txn("t_aug", "2026-08-20", "Dunder Mifflin", 33.00),
        ])
        store.upsert_invoices([inv("inv_dm", "Dunder Mifflin", 33.00, "2026-09-05"),
                               inv("inv_cafe", "Corner Cafe", 33.00, "2026-09-01")])
        stale_used = store.used_invoice_ids()
        # Meanwhile someone links inv_dm by hand to an August purchase.
        store.update_transaction("t_aug", {"invoice_status": "matched", "invoice_id": "inv_dm", "match_method": "manual"})
        with mock.patch.object(store, "used_invoice_ids", return_value=stale_used):
            counts = matching.match_month(store, None, 2026, 9, log=quiet)
        t_dm = store.get_transaction("t_dm")
        self.assertEqual((t_dm["invoice_status"], t_dm["invoice_id"], t_dm["match_method"], t_dm["match_confidence"]),
                         ("missing", None, None, None))
        self.assertEqual(store.get_transaction("t_cafe")["invoice_id"], "inv_cafe")     # its new link went through
        self.assertEqual(store.get_transaction("t_aug")["invoice_id"], "inv_dm")
        self.assertEqual((counts["skipped"], counts["matched"]), (1, 1))

    def test_a_row_changed_before_its_release_is_not_written_at_all(self):
        store = MemoryStore()
        store.insert_transactions([
            txn("t_old", "2026-09-01", "Sq *Corner Cafe", 33.00, status="possible", invoice_id="inv_x", method="rule"),
            txn("t_new", "2026-09-05", "Dunder Mifflin Paper", 33.00),
        ])
        store.upsert_invoices([inv("inv_x", "Dunder Mifflin", 33.00, "2026-09-05")])
        stale = store.list_transactions(2026, 9)
        store.update_transaction("t_old", {"invoice_status": "matched", "match_method": "manual",
                                           "match_note": "confirmed by ap@x.com"})       # a Confirm meanwhile
        with mock.patch.object(store, "list_transactions", return_value=stale):
            counts = matching.match_month(store, None, 2026, 9, log=quiet)
        self.assertEqual((store.get_transaction("t_old")["invoice_status"], store.get_transaction("t_old")["invoice_id"]),
                         ("matched", "inv_x"))
        # t_new would have taken inv_x; the unique index refuses it, so it stays missing and is counted as skipped.
        self.assertEqual(store.get_transaction("t_new")["invoice_status"], "missing")
        self.assertEqual(counts["skipped"], 2)

    def test_a_clean_run_has_no_skipped_count(self):
        store = MemoryStore()
        store.insert_transactions([txn("t1", "2026-09-04", "Uber* Trip", 23.45)])
        store.upsert_invoices([inv("i1", "Uber", 23.45, "2026-09-04")])
        counts = matching.match_month(store, None, 2026, 9, log=quiet)
        self.assertEqual(counts, {"checked": 1, "matched": 1, "possible": 0, "missing": 0, "invoices_available": 1})

    def test_supabase_writes_carry_the_snapshot_as_filters(self):
        calls = []

        def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
            calls.append((method, dict(params or {}), json))
            return FakeResponse(body="[]")       # nothing matched the filters: every write loses

        store = SupabaseStore("https://x.supabase.co", "k")
        old = txn("t_old", "2026-09-01", "Sq *Corner Cafe", 33.00, status="possible", invoice_id="inv_x", method="rule")
        new = txn("t_new", "2026-09-05", "Dunder Mifflin Paper", 33.00)
        with mock.patch.object(store, "list_transactions", return_value=[old, new]), \
                mock.patch.object(store, "list_invoices", return_value=[inv("inv_x", "Dunder Mifflin", 33.00, "2026-09-05")]), \
                mock.patch.object(store, "used_invoice_ids", return_value={"inv_x"}), \
                mock.patch.object(store_module.requests, "request", fake_request):
            counts = matching.match_month(store, None, 2026, 9, log=quiet)
        patches = [(p, body) for method, p, body in calls if method == "PATCH"]
        self.assertEqual(patches[0], ({"id": "eq.t_old", "invoice_status": "eq.possible", "invoice_id": "eq.inv_x",
                                       "match_method": "eq.rule"}, {"invoice_id": None}))
        self.assertEqual(patches[1][0], {"id": "eq.t_new", "invoice_status": "eq.missing", "invoice_id": "is.null",
                                         "match_method": "is.null"})
        self.assertEqual(len(patches), 2)        # t_old lost its release, so it is not written again
        self.assertEqual(counts["skipped"], 2)


if __name__ == "__main__":
    unittest.main()
