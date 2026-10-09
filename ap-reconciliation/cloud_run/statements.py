"""Turn an uploaded credit-card statement into normalised transactions.

Three input shapes:

1. Spreadsheet exports (.xlsx, .csv, .tsv) with a recognisable header row,
   such as the RBC Purchasing Card "Transaction Export" that carries every
   card of the account in one file. Parsed deterministically, no AI.
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
import io
import re

TABULAR_EXTENSIONS = {"xlsx", "xlsm", "csv", "tsv", "txt"}
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
                    "supplier name", "merchant description"],
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


def normalise_type(raw, amount):
    r = (raw or "").lower()
    if re.search(r"\b(payment|payments|pymt|autopay)\b", r) or r.startswith("pay "):
        return "payment"
    if re.search(r"\b(credit|credits|refund|refunds|return|returns|reversal|chargeback|voucher)\b", r):
        return "credit"
    if re.search(r"\b(fee|fees|interest)\b", r):
        return "fee"
    if re.search(r"\b(purchase|purchases|sale|sales|pos|debit)\b", r):
        return "purchase"
    return "purchase" if (amount or 0) >= 0 else "credit"


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


def detect_header(rows):
    """(header row index, {column index: field}) for the row that looks most like a header."""
    best, best_score = None, 0
    for i, row in enumerate(rows[:20]):
        chosen = {}      # field -> (priority, column)
        for j, cell in enumerate(row):
            hit = HEADER_LOOKUP.get(norm_header(cell))
            if hit and (hit[0] not in chosen or hit[1] < chosen[hit[0]][0]):
                chosen[hit[0]] = (hit[1], j)
        mapping = {col: field for field, (_, col) in chosen.items()}
        used = set(mapping.values())
        has_date = "txn_date" in used or "post_date" in used
        has_amount = "amount" in used or "debit" in used or "credit" in used
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
    transactions, warnings = [], []
    period_start, period_end = "", ""
    skipped = 0
    for row in data_rows:
        txn_date = parse_date(get(row, "txn_date"), dayfirst) or parse_date(get(row, "post_date"), dayfirst)
        if "amount" in fields:
            amount = parse_amount(get(row, "amount"))
        else:
            debit, credit = parse_amount(get(row, "debit")), parse_amount(get(row, "credit"))
            amount = None if debit is None and credit is None else round((debit or 0) - (credit or 0), 2)
        if not txn_date or amount is None:
            if any(clean(c) for c in row):
                skipped += 1
            continue
        description = clean(get(row, "description"))
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
            "supplier": clean(get(row, "description")),
            "city": clean(get(row, "city")),
            "country": clean(get(row, "country")),
            "merchant_category": clean(get(row, "merchant_category")),
            "type": normalise_type(raw_type, amount),
            "amount": amount,
            "currency": (clean(get(row, "currency")) or "CAD").upper()[:3],
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
    if not transactions:
        raise StatementError("The file has a header row but no transactions could be read from it.")
    if skipped:
        warnings.append(f"{skipped} line{'s' if skipped != 1 else ''} skipped: no readable date or amount.")
    # Some banks list charges as negative numbers. Judge by the purchase lines when the
    # file names its transaction types, by every line otherwise.
    judged = [t for t in transactions if t["type"] == "purchase"] if "type" in fields else transactions
    negatives = sum(1 for t in judged if t["amount"] < 0)
    if judged and negatives >= 0.8 * len(judged):
        for t in transactions:
            t["amount"] = round(-t["amount"], 2)
        warnings.append("Charges were listed as negative numbers; signs flipped so purchases are positive.")
    if "type" not in fields:
        for t in transactions:
            t["type"] = normalise_type("", t["amount"])
    slash_dates = [v for v in (get(r, "txn_date") for r in data_rows)
                   if re.match(r"^\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}$", clean(v))]
    if slash_dates and not dayfirst:
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
    elif ext in ("csv", "tsv", "txt") or mime_type.startswith("text/"):
        rows = read_csv(data)
    elif ext == "xls":
        raise StatementError("Legacy .xls is not supported: save the export as .xlsx or .csv and upload again.")

    if rows is not None:
        header = detect_header(rows)
        if header:
            result = from_table(rows, header[0], header[1], file_name)
        elif gemini is not None:
            result = from_gemini([gemini.text(STATEMENT_PROMPT + "\nThe statement, as CSV text:\n" + rows_to_text(rows))], gemini)
        else:
            raise StatementError("Could not recognise the columns in this file (need at least a date and an amount column).")
    else:
        gem_mime = GEMINI_MIME.get(ext) or (mime_type if mime_type in GEMINI_MIME.values() else "")
        if not gem_mime:
            raise StatementError(f"Unsupported file type '.{ext or '?'}'. Upload .xlsx, .csv, .pdf or an image.")
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
