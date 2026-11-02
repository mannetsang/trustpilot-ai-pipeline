"""Turn an uploaded credit-card statement into normalised transactions.

Three input shapes:

1. Spreadsheet exports (.xlsx, .csv, .tsv, .txt) with a recognisable header row,
   such as the RBC Purchasing Card "Transaction Export" that carries every
   card of the account in one file, or a headerless text export whose columns
   can be told apart by their contents (a masked card number, a YYYYMMDD date,
   a 0/1 debit-credit flag, the description, a currency code, an amount).
   A text export saved into a Word document (.docx) is read from the
   document's paragraphs. Parsed deterministically, no AI.
2. Spreadsheets whose columns are not recognised: the text is handed to
   Gemini with the same output schema.
3. PDF statements and photos: Gemini reads them inline.

Every transaction comes out as:

  card_last4, holder_name, txn_date, post_date, description, supplier, city,
  country, merchant_category, type (purchase | credit | payment | fee),
  amount (positive for purchases and fees, negative for credits and payments,
  in the billing currency), currency, source_amount, source_currency, raw

and gets a deterministic id, so uploading the same export twice adds nothing.
"""

import csv
import datetime as dt
import hashlib
import html
import io
import re
import zipfile

TABULAR_EXTENSIONS = {"xlsx", "xlsm", "csv", "tsv", "txt", "docx"}
DOCX_MAX_XML = 20 * 1024 * 1024        # word/document.xml, uncompressed
GEMINI_MIME = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "webp": "image/webp", "gif": "image/gif", "heic": "image/heic", "heif": "image/heif"}
MAX_GEMINI_TEXT = 120_000

# normalised header text -> field. Normalisation: lowercase, runs of non-alphanumerics -> one space.
COLUMNS = {
    "txn_date": ["transaction date", "trans date", "tran date", "date", "purchase date", "date of transaction",
                 "trans dt", "transaction dt"],
    "post_date": ["posting date", "post date", "posted date", "date posted", "posted", "settlement date"],
    "description": ["supplier", "description", "merchant", "merchant name", "payee", "details",
                    "transaction description", "narrative", "vendor", "transaction details", "memo",
                    "supplier name", "merchant description", "description 1"],
    "description_2": ["description 2"],
    "account_type": ["account type"],
    "amount": ["amount", "billing amount", "transaction amount", "amount cad", "total", "amt", "billed amount",
               "amount billed"],
    "debit": ["debit", "debits", "charges", "charge", "withdrawals", "purchases", "amount debit"],
    "credit": ["credit", "credits", "payments", "deposits", "amount credit"],
    "currency": ["billing currency", "currency", "amount currency", "billed currency"],
    "source_amount": ["source amount", "original amount", "foreign amount", "transaction currency amount",
                      "local amount", "amount in source currency"],
    "source_currency": ["source currency", "original currency", "foreign currency", "transaction currency",
                        "local currency"],
    "card": ["account", "account number", "card number", "card", "card no", "card last 4", "last 4",
             "last four", "account no", "card ending", "card ending in", "masked card number"],
    "holder_first": ["cardholder first name", "first name", "employee first name"],
    "holder_last": ["cardholder last name", "last name", "employee last name"],
    "holder": ["cardholder name", "cardholder", "card holder", "employee name", "name on card", "card member",
               "card member name"],
    "type": ["transaction type", "type", "trans type", "tran type"],
    "city": ["supplier city", "merchant city", "city"],
    "country": ["supplier country", "merchant country", "country"],
    "merchant_category": ["merchant category", "mcc description", "category", "merchant group", "mcc",
                          "merchant category code description"],
    "period_start": ["period start date", "statement start", "period start", "statement period start"],
    "period_end": ["period end date", "statement end", "period end", "statement date", "statement period end"],
    "reference": ["reference", "reference number", "transaction id", "reference no", "transaction reference"],
}
# normalised header -> (field, priority): the earlier a name sits in its list, the
# more specific it is, so "card number" beats "account" when a file has both.
HEADER_LOOKUP = {}
for _field, _names in COLUMNS.items():
    for _priority, _n in enumerate(_names):
        HEADER_LOOKUP.setdefault(_n, (_field, _priority))
CARD_NUMBER_RE = re.compile(r"(?<!\d)(\d{4})\d{5,8}(\d{4})(?!\d)")
# An amount column per currency, named after it: RBC's online-banking export has "CAD$" and "USD$",
# each line filled in one of them, money going out negative.
CURRENCY_AMOUNT_RE = re.compile(r"^\s*([A-Za-z]{3})\s*\$\s*$")
# Layouts known by their columns, with what the file itself does not say: Chase's card activity
# export (US accounts, so USD; money going out negative whatever the line's type). Memo is often
# deleted, so it is not required. A file's own currency column still wins.
KNOWN_LAYOUTS = [
    {"columns": {"transaction date", "post date", "description", "category", "type", "amount"},
     "currency": "USD", "charges_negative": True},
]
# RBC's online-banking export covers every account of the login: only card lines are statements.
CARD_ACCOUNT_RE = re.compile(r"visa|master\s*card|amex|american express|\bcard\b", re.I)   # not a line of credit
# Its Description 2 holds the original amount of a foreign purchase: "213.99 USD @ 1.42801".
FX_NOTE_RE = re.compile(r"^([\d,]+(?:\.\d+)?)\s+([A-Z]{3})\s*@\s*[\d.]+$")


def mask_card_numbers(value):
    """Keep only the first and last four digits of anything that looks like a full card number."""
    return CARD_NUMBER_RE.sub(lambda m: m.group(1) + "*" * 8 + m.group(2), value)


class StatementError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Cell helpers
# ---------------------------------------------------------------------------
def norm_header(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def clean(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return str(value).strip()


def parse_amount(value):
    """'$1,234.56' -> 1234.56; '(12.34)' and '12.34 CR' -> -12.34; '' -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    s = str(value).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if re.search(r"\bCR\b", s, re.I):
        negative = True
    s = re.sub(r"[^0-9.,\-]", "", s)
    if s.count(",") and s.count("."):
        s = s.replace(",", "")
    elif s.count(",") == 1 and len(s.split(",")[1]) == 2:
        s = s.replace(",", ".")      # 12,34 European decimal
    else:
        s = s.replace(",", "")
    if not s or s in ("-", ".", "-."):
        return None
    try:
        amount = float(s)
    except ValueError:
        return None
    return round(-amount if negative else amount, 2)


DATE_FORMATS_YMD = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")
DATE_FORMATS_TEXT = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%d-%b-%Y", "%d-%b-%y",
                     "%b-%d-%Y", "%b %d", "%d %b")


def parse_date(value, dayfirst=False):
    """-> 'YYYY-MM-DD' or ''. Slash/dash numeric dates are month-first unless dayfirst."""
    if value is None or value == "":
        return ""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    s = str(value).strip()
    for fmt in DATE_FORMATS_YMD:
        try:
            return dt.datetime.strptime(s[:len(fmt) + 9] if "T" in fmt or " " in fmt else s, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        day, month = (a, b) if (dayfirst or a > 12) else (b, a)
        if a > 12 and b > 12:
            return ""
        try:
            return dt.date(y, month, day).isoformat()
        except ValueError:
            return ""
    for fmt in DATE_FORMATS_TEXT:
        try:
            parsed = dt.datetime.strptime(s, fmt)
            if "%Y" not in fmt and "%y" not in fmt:
                parsed = parsed.replace(year=dt.date.today().year)
            return parsed.date().isoformat()
        except ValueError:
            continue
    return ""


def last4(value):
    s = clean(value)
    digits = re.findall(r"\d", s)
    return "".join(digits[-4:]) if len(digits) >= 4 else ""


def card_from_file_name(file_name):
    """Card digits named in a file name such as visa_4894_sep_2026.csv -> '4894'.

    Exports are often named after their month too, so prefer a standalone
    four-digit group that is not a year; otherwise fall back to the last four
    digits in the name."""
    for group in re.findall(r"(?<!\d)\d{4}(?!\d)", file_name or ""):
        if not 1990 <= int(group) <= 2100:
            return group
    return last4(file_name)


def type_from_words(raw):
    """The type a transaction-type cell names ("Sale", "Payment", "Return", "Fee"), or None."""
    r = (raw or "").lower()
    if re.search(r"\b(payment|payments|pymt|autopay)\b", r) or r.startswith("pay "):
        return "payment"
    if re.search(r"\b(credit|credits|refund|refunds|return|returns|reversal|chargeback|voucher)\b", r):
        return "credit"
    if re.search(r"\b(fee|fees|interest)\b", r):
        return "fee"
    if re.search(r"\b(purchase|purchases|sale|sales|pos|debit)\b", r):
        return "purchase"
    return None


def normalise_type(raw, amount):
    return type_from_words(raw) or ("purchase" if (amount or 0) >= 0 else "credit")


# The bank's own charges, named as such at the start of a line ("OVERLIMIT FEE", "ANNUAL FEE",
# "PURCHASE INTEREST"): a fee, not a purchase that needs an invoice, when no type column says so.
BANK_FEE_RE = re.compile(
    r"^(?:annual|overlimit|over[- ]limit|late(?: payment)?|cash advance|foreign (?:transaction|exchange)|"
    r"returned payment|nsf|card replacement|additional card|monthly(?: account| card)?)\s+fees?\b|"
    r"^(?:purchase|cash advance|cash)\s+interest\b|^interest charges?\b", re.I)


def norm_description(value):
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def transaction_id(t, occurrence):
    key = "|".join([t["card_last4"], t["txn_date"], t.get("post_date") or "", norm_description(t["description"]),
                    f"{float(t['amount']):.2f}", t.get("currency") or "", str(occurrence)])
    return "t_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def assign_ids(transactions):
    """Deterministic ids; identical rows in one file get an occurrence counter."""
    seen = {}
    for t in transactions:
        base = transaction_id(t, 0)
        n = seen.get(base, 0)
        seen[base] = n + 1
        t["id"] = base if n == 0 else transaction_id(t, n)
        year, month = t["txn_date"][:4], t["txn_date"][5:7]
        t["year"], t["month"] = int(year), int(month)
    return transactions


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------
def read_xlsx(data):
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise StatementError(f"The spreadsheet could not be opened: {exc}") from exc
    best = []
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(c not in (None, "") for c in r)]
        if len(rows) > len(best):
            best = rows
    return best


def read_csv(data):
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        rows = [row for row in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in row)]
    except csv.Error as exc:
        raise StatementError(f"The CSV could not be read: {exc}") from exc
    return rows


def read_docx(data):
    """A text export someone saved into Word: one line per paragraph (a table row's cells joined by
    tabs), then read like a CSV. The XML is taken apart with plain patterns, never an XML parser, so
    nothing in the file is expanded or fetched."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        info = z.getinfo("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise StatementError("The Word document could not be opened; save it again as .docx, or upload "
                             "the original .txt or .csv.") from exc
    if info.file_size > DOCX_MAX_XML:
        raise StatementError("The Word document is too large to read; upload the original .txt or .csv.")
    xml = z.read(info).decode("utf-8", errors="replace")

    def runs(fragment):
        out = []
        for m in re.finditer(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>|<w:tab\s*/>|<w:(?:br|cr)\b[^>]*/>", fragment, re.S):
            out.append(m.group(1) if m.group(1) is not None else ("\t" if "tab" in m.group(0) else "\n"))
        return html.unescape("".join(out))

    lines = []
    for block in re.finditer(r"<w:tr[\s>].*?</w:tr>|<w:p[\s>].*?</w:p>", xml, re.S):
        if block.group(0).startswith("<w:tr"):
            lines.append("\t".join(runs(c) for c in re.findall(r"<w:tc[\s>].*?</w:tc>", block.group(0), re.S)))
        else:
            lines.append(runs(block.group(0)))
    text = "\n".join(line for line in lines if line.strip())
    if not text:
        raise StatementError("The Word document holds no text.")
    return read_csv(text.encode("utf-8"))


# Headerless exports: what each column's values look like.
MASKED_CARD_RE = re.compile(r"^\d{4,6}[xX*#•]{2,}\d{4}$|^\d{13,19}$")
YMD8_RE = re.compile(r"^(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")
AMOUNT_RE = re.compile(r"^-?\$?\d[\d,]*\.\d{2}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
DC_FLAGS = ({"0", "1"}, {"D", "C"}, {"DR", "CR"})
CREDIT_FLAGS = {"1", "C", "CR"}
PAYMENT_RE = re.compile(r"\b(payment|pymt|autopay)\b", re.I)


def detect_headerless(rows):
    """For a file with no header row: (rows with a made-up header row on top, {column: field}) when
    its columns can be told apart by their values, else None. Needs a date, an amount and a
    description column; a 0/1 (or D/C) column says which lines are credits and payments, and is
    folded into the amount's sign and a type column."""
    rows = [r for r in rows if any(clean(c) for c in r)]
    width = max((len(r) for r in rows), default=0)
    if len(rows) < 1 or width < 3:
        return None

    def share(col, test):
        values = [clean(r[col]) for r in rows if col < len(r)]
        return sum(1 for v in values if v and test(v)) / len(rows)

    mapping, taken = {}, set()

    def pick(field, test, threshold=0.9):
        for col in range(width):
            if col not in taken and share(col, test) >= threshold:
                mapping[col] = field
                taken.add(col)
                return col
        return None

    pick("card", lambda v: bool(MASKED_CARD_RE.match(v)))
    date_col = pick("txn_date", lambda v: bool(YMD8_RE.match(v)))
    if date_col is None:
        date_col = pick("txn_date", lambda v: bool(parse_date(v)))
    amount_col = pick("amount", lambda v: bool(AMOUNT_RE.match(v)))
    pick("currency", lambda v: bool(CURRENCY_RE.match(v)))
    flag_col = None
    for col in range(width):
        values = {clean(r[col]).upper() for r in rows if col < len(r) and clean(r[col])}
        if col not in taken and values and any(values <= flags for flags in DC_FLAGS):
            flag_col = col
            taken.add(col)
            break
    letters = {col: sum(sum(ch.isalpha() for ch in clean(r[col])) for r in rows if col < len(r))
               for col in range(width) if col not in taken}
    desc_col = max(letters, key=letters.get) if letters and max(letters.values()) > 0 else None
    if date_col is None or amount_col is None or desc_col is None:
        return None
    mapping[desc_col] = "description"

    names = {"card": "Card number", "txn_date": "Transaction date", "amount": "Amount", "currency": "Currency",
             "description": "Description", "type": "Type"}
    header = [names.get(mapping.get(col), f"Column {col + 1}") for col in range(width)]
    if flag_col is not None:
        header[flag_col] = "Debit/credit"
    out = [header]
    for r in rows:
        r = list(r) + [""] * (width - len(r))
        if flag_col is not None:
            credit = clean(r[flag_col]).upper() in CREDIT_FLAGS
            amount = clean(r[amount_col]).lstrip("-")
            r[amount_col] = ("-" + amount) if credit and amount else amount
            r.append(("payment" if PAYMENT_RE.search(clean(r[desc_col])) else "credit") if credit else "purchase")
        out.append(r)
    if flag_col is not None:
        out[0].append("Type")
        mapping[width] = "type"
    return out, mapping


def detect_header(rows):
    """(header row index, {column index: field}) for the row that looks most like a header."""
    best, best_score = None, 0
    for i, row in enumerate(rows[:20]):
        chosen = {}      # field -> (priority, column)
        for j, cell in enumerate(row):
            hit = HEADER_LOOKUP.get(norm_header(cell))
            ccy = CURRENCY_AMOUNT_RE.match(str(cell or ""))
            if ccy:
                hit = ("amount_" + ccy.group(1).upper(), 0)
            if hit and (hit[0] not in chosen or hit[1] < chosen[hit[0]][0]):
                chosen[hit[0]] = (hit[1], j)
        mapping = {col: field for field, (_, col) in chosen.items()}
        used = set(mapping.values())
        has_date = "txn_date" in used or "post_date" in used
        has_amount = "amount" in used or "debit" in used or "credit" in used or any(f.startswith("amount_") for f in used)
        score = len(used) + (2 if has_date and has_amount else 0)
        if has_date and has_amount and score > best_score:
            best, best_score = (i, mapping), score
    return best


def _dayfirst(values):
    """True when any numeric slash date has its first part above 12 (so the file is day-first)."""
    for v in values:
        m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$", clean(v))
        if m and int(m.group(1)) > 12:
            return True
    return False


def from_table(rows, header_index, mapping, file_name=""):
    fields = {f: j for j, f in mapping.items()}
    get = lambda row, f: row[fields[f]] if f in fields and fields[f] < len(row) else None  # noqa: E731
    data_rows = rows[header_index + 1:]
    dayfirst = _dayfirst([get(r, "txn_date") for r in data_rows] + [get(r, "post_date") for r in data_rows])
    fallback_card = card_from_file_name(file_name) if "card" not in fields else ""
    by_currency = sorted((f[len("amount_"):], j) for f, j in fields.items() if f.startswith("amount_"))
    headers = {norm_header(h) for h in rows[header_index]}
    layout = next((lay for lay in KNOWN_LAYOUTS if lay["columns"] <= headers), {})
    # How this layout signs its lines: an amount column per currency (RBC online banking) and the
    # known layouts list money going out as negative, whatever a given file happens to contain.
    charges_negative = bool(by_currency) or bool(layout.get("charges_negative"))
    transactions, warnings = [], []
    period_start, period_end = "", ""
    skipped = not_card = 0
    for row in data_rows:
        account_type = clean(get(row, "account_type"))
        if account_type and not CARD_ACCOUNT_RE.search(account_type):
            not_card += 1                   # a chequing or savings line in an all-accounts download
            continue
        txn_date = parse_date(get(row, "txn_date"), dayfirst) or parse_date(get(row, "post_date"), dayfirst)
        row_currency = ""
        if "amount" in fields:
            amount = parse_amount(get(row, "amount"))
        elif by_currency:
            amount = None
            for ccy, j in by_currency:
                value = parse_amount(row[j] if j < len(row) else None)
                if value is not None:
                    amount, row_currency = value, ccy
                    break
        else:
            debit, credit = parse_amount(get(row, "debit")), parse_amount(get(row, "credit"))
            amount = None if debit is None and credit is None else round((debit or 0) - (credit or 0), 2)
        if not txn_date or amount is None:
            if any(clean(c) for c in row):
                skipped += 1
            continue
        extra = html.unescape(clean(get(row, "description_2")))
        fx = FX_NOTE_RE.match(extra)
        description = " ".join(x for x in (html.unescape(clean(get(row, "description"))), "" if fx else extra) if x)
        raw_type = clean(get(row, "type"))
        if not description:
            # Payments to the card and some fees carry no supplier text; keep them visible.
            description = clean(get(row, "reference")) or raw_type or "(no description)"
        holder = clean(get(row, "holder")) or " ".join(
            x for x in (clean(get(row, "holder_first")), clean(get(row, "holder_last"))) if x)
        t = {
            "card_last4": last4(get(row, "card")) or fallback_card or "0000",
            "holder_name": holder,
            "txn_date": txn_date,
            "post_date": parse_date(get(row, "post_date"), dayfirst),
            "description": description,
            "supplier": html.unescape(clean(get(row, "description"))),
            "city": clean(get(row, "city")),
            "country": clean(get(row, "country")),
            "merchant_category": clean(get(row, "merchant_category")),
            "type": normalise_type(raw_type, amount),
            "_named": type_from_words(raw_type),
            "_fx": (abs(parse_amount(fx.group(1))), fx.group(2)) if fx else None,
            "amount": amount,
            "currency": (clean(get(row, "currency")) or row_currency or layout.get("currency") or "CAD").upper()[:3],
            "source_amount": parse_amount(get(row, "source_amount")),
            "source_currency": (clean(get(row, "source_currency")) or "").upper()[:3],
            "raw": {str(rows[header_index][j]): mask_card_numbers(clean(c))
                    for j, c in enumerate(row) if j < len(rows[header_index]) and clean(c)},
        }
        if t["source_currency"] == t["currency"] or t["source_amount"] is None:
            t["source_amount"], t["source_currency"] = None, ""
        ps, pe = parse_date(get(row, "period_start"), dayfirst), parse_date(get(row, "period_end"), dayfirst)
        period_start = min(period_start or ps, ps) if ps else period_start
        period_end = max(period_end, pe) if pe else period_end
        transactions.append(t)
    if not transactions and not_card:
        raise StatementError("The file holds only chequing or savings lines, no card lines: download the card's "
                             "transactions on their own.")
    if not transactions:
        raise StatementError("The file has a header row but no transactions could be read from it.")
    if skipped:
        warnings.append(f"{skipped} line{'s' if skipped != 1 else ''} skipped: no readable date or amount.")
    if not_card:
        warnings.append(f"{not_card} line{'s' if not_card != 1 else ''} of a chequing or savings account left out.")
    if fallback_card:
        warnings.append(f"The file has no card number column: its lines are filed under card {fallback_card}, "
                        "taken from the file name.")
    elif "card" not in fields:
        warnings.append("Neither the file nor its name gives a card number: its lines are filed under card 0000.")
    # Some banks list charges as negative numbers. The layout may say so; otherwise judge by the
    # purchase lines when the file names its transaction types, by every line when it does not.
    judged = [t for t in transactions if t["type"] == "purchase"] if "type" in fields else transactions
    negatives = sum(1 for t in judged if t["amount"] < 0)
    flip = charges_negative or (judged and negatives >= 0.8 * len(judged))
    if flip:
        for t in transactions:
            t["amount"] = round(-t["amount"], 2)
        if not charges_negative:
            warnings.append("Charges were listed as negative numbers; signs flipped so purchases are positive.")
    for t in transactions:
        named, fx = t.pop("_named"), t.pop("_fx")
        if "type" in fields and not named and flip:
            # A type cell with no telling word ("Adjustment"): its sign decided it, and the sign
            # just turned round: a statement credit is a credit, not a purchase.
            t["type"] = "purchase" if t["amount"] >= 0 else "credit"
        if fx:
            t["source_amount"] = fx[0] if t["amount"] >= 0 else -fx[0]
            t["source_currency"] = fx[1] if fx[1] != t["currency"] else ""
            if not t["source_currency"]:
                t["source_amount"] = None
    if "type" not in fields:
        for t in transactions:
            t["type"] = normalise_type("", t["amount"])
            if t["type"] == "credit" and PAYMENT_RE.search(t["description"]):
                t["type"] = "payment"           # "AUTOMATIC PAYMENT -THANK YOU"
    for t in transactions:
        if t["type"] == "purchase" and BANK_FEE_RE.match(t["description"]):
            t["type"] = "fee"
    slash_dates = [re.match(r"^\d{1,2}[/\-.](\d{1,2})[/\-.]\d{2,4}$", clean(v)) for v in (get(r, "txn_date") for r in data_rows)]
    slash_dates = [m for m in slash_dates if m]
    month_first_shown = any(int(m.group(1)) > 12 for m in slash_dates)       # 1/19/2026
    if slash_dates and not dayfirst and not month_first_shown:
        warnings.append("Numeric dates were read as month/day/year; check one against the statement.")
    dates = sorted(t["txn_date"] for t in transactions)
    return {
        "format": "table",
        "transactions": assign_ids(transactions),
        "period_start": period_start or dates[0],
        "period_end": period_end or dates[-1],
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Gemini fallback (PDF, images, unrecognised tables)
# ---------------------------------------------------------------------------
STATEMENT_PROMPT = """You are reading a credit card statement or card transaction export for an accounts-payable team.
Extract EVERY transaction line. Return strict JSON with this shape and nothing else:
{
  "cards": [{"last4": "1610", "holder_name": "Yin Wei"}],
  "period_start": "YYYY-MM-DD", "period_end": "YYYY-MM-DD",
  "currency": "CAD",
  "transactions": [
    {"card_last4": "1610", "txn_date": "YYYY-MM-DD", "post_date": "YYYY-MM-DD or null",
     "description": "merchant text exactly as printed", "amount": 12.34, "currency": "CAD",
     "source_amount": null, "source_currency": null,
     "type": "purchase", "city": "", "country": ""}
  ]
}
Rules:
- amount is in the billing currency: positive for purchases and fees, negative for payments received,
  refunds and credits. type is one of purchase, credit, payment, fee.
- When a line shows a foreign amount (e.g. "USD 70.40") put it in source_amount/source_currency and the
  billed amount in amount.
- card_last4 is the last four digits of the card the line belongs to; statements with several cards list
  the card before its transactions. Use "0000" if no card number appears anywhere.
- Dates: infer the year from the statement period when a line only shows day and month.
- Do not invent lines, do not merge lines, do not include running balances, subtotals or totals.
"""


def from_gemini(parts, gemini, model=None):
    data = gemini.generate_json(parts, model=model or gemini.extract_model)
    cards = {}
    for c in data.get("cards") or []:
        l4 = last4(c.get("last4"))
        if l4:
            cards[l4] = {"holder_name": clean(c.get("holder_name"))}
    transactions = []
    for row in data.get("transactions") or []:
        txn_date = parse_date(row.get("txn_date"))
        amount = parse_amount(row.get("amount"))
        if not txn_date or amount is None:
            continue
        l4 = last4(row.get("card_last4")) or (next(iter(cards)) if len(cards) == 1 else "0000")
        currency = (clean(row.get("currency")) or clean(data.get("currency")) or "CAD").upper()[:3]
        source_currency = (clean(row.get("source_currency")) or "").upper()[:3]
        source_amount = parse_amount(row.get("source_amount"))
        if source_currency == currency or source_amount is None:
            source_amount, source_currency = None, ""
        transactions.append({
            "card_last4": l4,
            "holder_name": cards.get(l4, {}).get("holder_name", ""),
            "txn_date": txn_date,
            "post_date": parse_date(row.get("post_date")),
            "description": clean(row.get("description")),
            "supplier": clean(row.get("description")),
            "city": clean(row.get("city")), "country": clean(row.get("country")),
            "merchant_category": "",
            "type": normalise_type(clean(row.get("type")), amount),
            "amount": amount, "currency": currency,
            "source_amount": source_amount, "source_currency": source_currency,
            "raw": {k: clean(v) for k, v in row.items() if clean(v)},
        })
    if not transactions:
        raise StatementError("Gemini found no transactions in the file.")
    dates = sorted(t["txn_date"] for t in transactions)
    return {
        "format": "gemini",
        "transactions": assign_ids(transactions),
        "period_start": parse_date(data.get("period_start")) or dates[0],
        "period_end": parse_date(data.get("period_end")) or dates[-1],
        "warnings": ["Transactions were read by AI from a document; spot-check amounts against the statement."],
    }


def rows_to_text(rows, limit=MAX_GEMINI_TEXT):
    out = io.StringIO()
    writer = csv.writer(out)
    for row in rows:
        writer.writerow([clean(c) for c in row])
        if out.tell() > limit:
            break
    return out.getvalue()[:limit]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_statement(file_name, data, mime_type="", gemini=None):
    """-> {format, transactions, period_start, period_end, warnings, cards}"""
    ext = (file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "").strip()
    mime_type = (mime_type or "").lower()
    rows = None
    if ext in ("xlsx", "xlsm") or "spreadsheetml" in mime_type:
        rows = read_xlsx(data)
    elif ext == "docx" or "wordprocessingml" in mime_type:
        rows = read_docx(data)
    elif ext in ("csv", "tsv", "txt") or mime_type.startswith("text/"):
        rows = read_csv(data)
    elif ext == "xls":
        raise StatementError("Legacy .xls is not supported: save the export as .xlsx or .csv and upload again.")

    if rows is not None:
        header = detect_header(rows)
        headerless = None if header else detect_headerless(rows)
        if header:
            result = from_table(rows, header[0], header[1], file_name)
        elif headerless:
            result = from_table(headerless[0], 0, headerless[1], file_name)
            result["format"] = "table (no header row)"
            result["warnings"].insert(0, "The file has no header row: its columns were recognised from their "
                                         "contents. Check a few lines against the statement.")
        elif gemini is not None:
            result = from_gemini([gemini.text(STATEMENT_PROMPT + "\nThe statement, as CSV text:\n" + rows_to_text(rows))], gemini)
        else:
            raise StatementError("Could not recognise the columns in this file (need at least a date and an amount column).")
    else:
        gem_mime = GEMINI_MIME.get(ext) or (mime_type if mime_type in GEMINI_MIME.values() else "")
        if not gem_mime:
            raise StatementError(f"Unsupported file type '.{ext or '?'}'. Upload .xlsx, .csv, .txt, .docx, .pdf or an image.")
        if gemini is None:
            raise StatementError("PDF and image statements need Gemini, which is not configured.")
        if len(data) > 15 * 1024 * 1024:
            raise StatementError("File is larger than 15 MB; split the statement and upload the parts.")
        result = from_gemini([gemini.inline(data, gem_mime), gemini.text(STATEMENT_PROMPT)], gemini)

    cards = {}
    for t in result["transactions"]:
        card = cards.setdefault(t["card_last4"], {"holder_name": "", "count": 0})
        card["count"] += 1
        if t.get("holder_name") and not card["holder_name"]:
            card["holder_name"] = t["holder_name"]
    result["cards"] = cards
    return result
