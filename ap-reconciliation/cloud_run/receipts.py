"""Receipts added from the dashboard: checking the upload, choosing the folder, naming the file.

A card holder photographs or uploads the receipt of one purchase. The file goes
to Invoice/<YYYY MM of the purchase>/<vendor folder>/ in the AP Drive, where the
invoice index reads it like any other file. Everything here is pure (no network):
main.py makes the Drive and Store calls.

- prepare_upload checks the parts by their magic bytes, never by the name the
  phone gave them, and turns several photos of one long receipt into ONE PDF.
  The PDF embeds each JPEG unchanged (DCTDecode), so no pixel is ever decoded
  and nothing the user typed ends up inside it.
- suggest_vendor_folder picks the folder: an existing folder whose name the
  charge's descriptor covers (this month's first, then earlier months'), else a
  name cleaned from the descriptor, snapped to an existing spelling.
- receipt_file_name never carries the amount or the card digits: INVOICE_PROMPT
  passes the file name to Gemini as a hint, and it must not read its own answer.
"""

import io
import re
import struct
import unicodedata

import invoices
from chat import money
from matching import tokens

MAX_PAGES = 10
MAX_IMAGE_BYTES = invoices.MAX_IMAGE_BYTES     # 7 MB, what Gemini takes inline
MAX_PDF_BYTES = invoices.MAX_BYTES             # 15 MB
MAX_SIDE = 10000                               # px; anything bigger is not a phone photo
FOLDER_MAX = 60
FILE_NAME_MAX = 120
UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic",
              "application/pdf": "pdf"}

# Card descriptors: "<processor>*<merchant>" names the merchant on the right, "<platform>*<product>"
# the product of a big platform, anything else "<merchant>*<reference>".
PROCESSORS = {"sq", "tst", "sp", "pp", "paypal", "py", "sumup", "zettle", "ic", "dd", "cko"}
PLATFORMS = {"google", "apple", "microsoft", "msft"}
NOISE = {"mktp", "marketplace", "ca", "com", "subscr", "subscription", "preauth", "inc", "ltd", "llc", "corp",
         "co", "payment", "purchase", "online", "www"}
DISPLAY_NAMES = {"amzn": "Amazon", "amazon": "Amazon", "aircan": "Air Canada", "aircanada": "Air Canada",
                 "chatgpt": "OpenAI", "openai": "OpenAI", "msft": "Microsoft", "goog": "Google", "adbe": "Adobe",
                 "sqsp": "Squarespace", "fb": "Facebook", "facebk": "Facebook", "mcdonalds": "McDonald's"}
UNSAFE_CHARS = re.compile(r'[/\\:*?"<>|]')


class ReceiptError(Exception):
    """An upload the server refuses; status is the HTTP status, message is shown verbatim."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


# ---------------------------------------------------------------------------
# The upload
# ---------------------------------------------------------------------------
def sniff(data):
    """The file's type from its first bytes, or None. The client's name and type are never trusted."""
    data = data or b""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in HEIC_BRANDS:
        return "image/heic"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    return None


def jpeg_info(data):
    """(width, height, components) from the first frame header (SOFn) of a JPEG.

    Walks the marker segments without decoding anything. Raises ReceiptError(415) when the data is
    cut short, has no frame header, is not greyscale or RGB (CMYK would need an inverted Decode
    array in the PDF), or has a side over MAX_SIDE pixels."""
    def bad(why):
        return ReceiptError(415, f"The photo could not be read: {why}.")

    if data[:2] != b"\xff\xd8":
        raise bad("not a JPEG")
    i, n = 2, len(data)
    while i + 1 < n:
        if data[i] != 0xFF:
            raise bad("damaged marker")
        marker = data[i + 1]
        if marker == 0xFF:                              # fill byte before a marker
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:   # SOI, TEM, RSTn carry no length
            i += 2
            continue
        if marker in (0xD9, 0xDA):                      # end of image or scan data before any frame header
            break
        if i + 4 > n:
            raise bad("cut short")
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if length < 2:
            raise bad("damaged segment")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 10 > n or length < 8:
                raise bad("cut short")
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            components = data[i + 9]
            if components not in (1, 3):
                raise bad("only colour or greyscale photos are supported")
            if not width or not height:
                raise bad("no size")
            if width > MAX_SIDE or height > MAX_SIDE:
                raise bad(f"larger than {MAX_SIDE} pixels")
            return width, height, components
        i += 2 + length
    raise bad("no frame header")


def _num(value):
    """A PDF number: 595.0 -> '595', 841.890 -> '841.89'."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def jpegs_to_pdf(pages, page_width=595.0, max_height=14400.0):
    """One PDF with one JPEG per page, each embedded byte for byte (DCTDecode).

    Every page is page_width points wide (A4's width) with the height in proportion; a very long
    receipt is capped at max_height (the PDF 1.4 limit is 14400) and keeps its proportions by
    getting narrower."""
    objects, kids = [], []
    for n, jpg in enumerate(pages):
        width, height, components = jpeg_info(jpg)
        pw, ph = page_width, height * page_width / width
        if ph > max_height:
            pw, ph = width * max_height / height, max_height
        page_obj = 3 + 3 * n
        content = f"q {_num(pw)} 0 0 {_num(ph)} 0 0 cm /Im0 Do Q".encode()
        image = (f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                 f"/ColorSpace /Device{'RGB' if components == 3 else 'Gray'} /BitsPerComponent 8 "
                 f"/Filter /DCTDecode /Length {len(jpg)} >>\nstream\n").encode() + jpg + b"\nendstream"
        page = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_num(pw)} {_num(ph)}] "
                f"/Resources << /XObject << /Im0 {page_obj + 1} 0 R >> >> /Contents {page_obj + 2} 0 R >>").encode()
        objects += [page, image, b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"]
        kids.append(f"{page_obj} 0 R")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()] + objects
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")         # the binary comment tells tools the file is binary
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def prepare_upload(parts):
    """parts: [(client file name, bytes)] in page order. Returns (data, mime, ext, pages).

    One photo or one PDF is kept as it is; several photos (JPEGs, which is what the page sends after
    preparing them) become one PDF. Raises ReceiptError with the status and the text to show."""
    if not parts:
        raise ReceiptError(400, "Attach the receipt as one or more 'file' parts.")
    if any(not data for _, data in parts):
        raise ReceiptError(400, "The file is empty.")
    if len(parts) > MAX_PAGES:
        raise ReceiptError(400, f"At most {MAX_PAGES} pages per receipt.")
    kinds = [sniff(data) for _, data in parts]
    if None in kinds:
        raise ReceiptError(415, "That file isn't a photo or a PDF. Choose a JPEG, PNG, WebP or HEIC photo, or a PDF.")
    if len(parts) == 1:
        data, mime = parts[0][1], kinds[0]
        if mime == "application/pdf" and len(data) > MAX_PDF_BYTES:
            raise ReceiptError(413, f"That PDF is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB.")
        if mime != "application/pdf" and len(data) > MAX_IMAGE_BYTES:
            raise ReceiptError(413, f"That photo is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
        return data, mime, EXTENSIONS[mime], 1
    if any(kind != "image/jpeg" for kind in kinds):
        raise ReceiptError(400, "Several pages must all be photos; add a PDF on its own.")
    pages = []
    for number, (_, data) in enumerate(parts, 1):
        if len(data) > MAX_IMAGE_BYTES:
            raise ReceiptError(413, f"That photo is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
        try:
            jpeg_info(data)
        except ReceiptError:
            raise ReceiptError(415, f"Page {number} could not be read as a photo; take it again.") from None
        pages.append(data)
    pdf = jpegs_to_pdf(pages)
    if len(pdf) > MAX_PDF_BYTES:
        raise ReceiptError(413, f"These pages come to more than {MAX_PDF_BYTES // (1024 * 1024)} MB as one PDF; "
                                "remove a page.")
    return pdf, "application/pdf", "pdf", len(pages)


# ---------------------------------------------------------------------------
# The vendor folder
# ---------------------------------------------------------------------------
def compact(s):
    """The comparison key of a folder name: 'Uber Eats' and 'uber-eats' are the same folder."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def name_tokens(text, city=""):
    """matching.tokens without trailing reference digits ('ads1234567' -> 'ads') and the city."""
    out = set()
    for tok in tokens(text):
        tok = re.sub(r"\d+$", "", tok)
        if len(tok) >= 2:
            out.add(tok)
    return out - tokens(city)


def _covered(folder_token, charge_tokens):
    for c in charge_tokens:
        # 'Can' in a truncated descriptor covers 'Canada'; a folder's 'Tele' covers 'Telecom'.
        if c == folder_token or (len(c) >= 3 and folder_token.startswith(c)) \
                or (len(folder_token) >= 4 and c.startswith(folder_token)):
            return True
    return False


def folder_matches(name, charge_tokens):
    """Does the charge's descriptor cover every word of this folder name? 'Uber* Trip' fits 'Uber'
    but not 'Uber Eats'; a folder of short words only ('AB') never fits."""
    folder_tokens = name_tokens(name)
    return bool(folder_tokens) and any(len(t) >= 3 for t in folder_tokens) \
        and all(_covered(t, charge_tokens) for t in folder_tokens)


def _title(word):
    """'SHOPIFY' -> 'Shopify', "tim's" -> "Tim's" (str.title would give "Tim'S")."""
    return re.sub(r"[A-Za-z]+(?:'[A-Za-z]+)?", lambda m: m.group(0)[0].upper() + m.group(0)[1:].lower(), word)


def clean_vendor_name(description, supplier="", city=""):
    """A folder name from a card descriptor: 'Sq *coffee Shop Toronto' -> 'Coffee Shop',
    'Amzn Mktp Ca*AB12C' -> 'Amazon', '#4012 12345' -> 'Receipts'."""
    text = unicodedata.normalize("NFKC", description or "").strip()
    if "*" in text:
        left, right = text.split("*", 1)
        if compact(left) in PROCESSORS:
            text = right
        elif compact(left) in PLATFORMS:
            word = next((w for w in right.split() if re.fullmatch(r"[A-Za-z][A-Za-z'&.-]{2,}", w)), "")
            text = f"{left.strip()} {word}".strip()
        else:
            text = left
    text = re.sub(r"(?i)^www\.", "", text.strip())
    text = re.sub(r"(?i)\.(com|ca|net|io|org|co|ai)\b", " ", text)
    if compact(text) in DISPLAY_NAMES:
        return DISPLAY_NAMES[compact(text)]
    city_words = {w.lower() for w in (city or "").split()}
    words = []
    for i, word in enumerate(text.split()):
        if any(ch.isdigit() for ch in word) or "#" in word or word.lower() in city_words:
            continue
        if i > 0 and word.lower().strip(".") in NOISE:
            continue
        words.append(word)
    if words and compact(words[0]) in DISPLAY_NAMES:
        words[0] = DISPLAY_NAMES[compact(words[0])]
    words = [_title(w) if w.islower() or w.isupper() else w for w in words]
    name = " ".join(words[:4])[:FOLDER_MAX].strip(" .-_")
    name = DISPLAY_NAMES.get(compact(name), name)
    if not name:
        return clean_vendor_name(supplier, "", city) if supplier else "Receipts"
    return safe_folder_name(name) or "Receipts"


def _same_name(a, b):
    """Folder names that are the same folder: equal compact keys, or equal text when there are no
    a-z0-9 characters to compare (a name in another script)."""
    key = compact(a)
    return key == compact(b) if key else a.casefold() == (b or "").casefold()


def _month_rank(month_folder):
    try:
        return int(str(month_folder or "").replace(" ", ""))
    except ValueError:
        return 0


def suggest_vendor_folder(txn, this_month_names, earlier):
    """(folder name, source) for the receipt of txn.

    this_month_names: the folders in the month folder now (a live listing, so folders a person just
    made count). earlier: {vendor folder: (file count, latest 'YYYY MM')} of indexed invoices around
    the month. source is "this_month", "earlier_month" or "new"."""
    city = txn.get("city") or ""
    charge = name_tokens(txn.get("description"), city) | name_tokens(txn.get("supplier"), city)
    this_month_names = [n for n in this_month_names or [] if n]
    earlier = {n: (v if isinstance(v, tuple) else (v, "")) for n, v in (earlier or {}).items() if n}
    candidates = []
    for name in this_month_names:
        if folder_matches(name, charge):
            candidates.append(((0, -len(name_tokens(name)), 0, 0, name), name, "this_month"))
    for name, (files, latest) in earlier.items():
        if folder_matches(name, charge):
            candidates.append(((1, -len(name_tokens(name)), -int(files or 0), -_month_rank(latest), name),
                               name, "earlier_month"))
    if candidates:
        _, name, source = min(candidates)
        return name, source
    name = clean_vendor_name(txn.get("description"), txn.get("supplier") or "", city)
    return _snap(name, this_month_names, list(earlier))


def _snap(name, this_month_names, earlier_names):
    for existing in this_month_names or []:
        if existing and _same_name(name, existing):
            return existing, "this_month"
    for existing in earlier_names or []:
        if existing and _same_name(name, existing):
            return existing, "earlier_month"
    return name, "new"


def snap_folder_name(name, this_month_names, earlier_names):
    """(spelling, exists in this month): an existing folder with the same compact key keeps its
    spelling ('uber' files into 'Uber'), this month's first, so no near-duplicate folder is made."""
    spelling, source = _snap(name, this_month_names, earlier_names)
    return spelling, source == "this_month"


def safe_folder_name(name):
    """A typed folder name made safe for Drive and for people syncing it to Windows, or None.

    None for names that are empty, only dots, without a letter or digit, a path climbing out
    ('../../x'), or a month ('2026 10' would be read as a month folder by the index)."""
    text = unicodedata.normalize("NFC", str(name or ""))
    segments = re.split(r"[/\\]", text)
    if len(segments) > 1 and any(part.strip() in (".", "..") for part in segments):
        return None
    text = re.sub(r"\s", " ", text)
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("C"))
    text = UNSAFE_CHARS.sub("-", text)
    text = re.sub(r" +", " ", text).strip(" .-_")
    text = text[:FOLDER_MAX].strip(" .-_")
    if text in ("", ".", "..") or not any(ch.isalnum() for ch in text) or invoices.parse_month_folder(text):
        return None
    return text


def receipt_file_name(txn_date, vendor, ext, taken=()):
    """'2026-09-04 Uber receipt.jpg', or '… receipt (2).jpg' when that name is taken in the folder.
    Never the amount or the card digits (the file name is a hint to Gemini)."""
    date = str(txn_date or "")[:10]
    taken = {str(t).casefold() for t in taken or ()}
    for n in range(1, 1000):
        suffix = f" ({n})" if n > 1 else ""
        room = FILE_NAME_MAX - len(f"{date}  receipt{suffix}.{ext}")
        name = f"{date} {str(vendor)[:max(room, 1)].strip()} receipt{suffix}.{ext}"
        if name.casefold() not in taken:
            return name
    raise ReceiptError(409, "That folder already holds too many receipts for this date.")


def drive_description(txn, email, stamp):
    """The Drive file's description: where the file came from. Drive shows it in the file's
    details; it is never sent to Gemini."""
    description = str(txn.get("description") or "")
    if len(description) > 200:
        description = description[:199] + "…"
    amount = money(txn["amount"], txn.get("currency") or "CAD")
    text = (f"Receipt for {description} ({amount}) on {txn.get('txn_date')}, card ending {txn.get('card_last4')}. "
            f"Uploaded from the AP dashboard by {email} on {stamp}.")
    return text[:500]
