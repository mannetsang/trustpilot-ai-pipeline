"""Index the AP Invoice folder.

The folder is laid out as Invoice/YYYY MM/<vendor or person>/<files>, with the
odd file sitting directly in a month folder. Every PDF or image is read ONCE by
Gemini (vendor, invoice number, date, total, currency, card digits when
printed) and the result is stored in the `invoices` table keyed by Drive file
id; a file is only re-read when its Drive modifiedTime changes. The month and
vendor folder names travel with each record because they are strong matching
hints: a receipt filed under "2026 09/Anthropic" is an Anthropic charge from
around September.
"""

import concurrent.futures
import datetime as dt
import re

from drive import EXPORT_AS_PDF, FOLDER
from statements import clean, last4, parse_amount, parse_date

MONTH_RE = re.compile(r"^(\d{4})[ _\-./]?(\d{1,2})$")
SUPPORTED_MIME = {"application/pdf", "image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif",
                  "image/heic", "image/heif"}
TEXT_MIME = {"text/plain", "text/csv", "text/html"}
EXT_MIME = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "gif": "image/gif", "heic": "image/heic"}
MAX_BYTES = 15 * 1024 * 1024

INVOICE_PROMPT = """This file comes from an accounts-payable folder of invoices and receipts that were paid with company credit cards.
Folder hints: month folder "{month}", vendor folder "{vendor}", file name "{name}".

Extract the facts needed to match it to a credit-card charge. Return strict JSON:
{{"is_invoice": true,
  "vendor": "merchant or supplier name as a card statement would show it",
  "invoice_number": "invoice, receipt or order number, or null",
  "invoice_date": "YYYY-MM-DD of the invoice/receipt/payment, or null",
  "total": 12.34,
  "currency": "CAD",
  "card_last4": "last four digits of the card if printed, else null",
  "payment_method": "e.g. Visa ending 1610, PayPal, bank transfer, or null",
  "summary": "one line: what was bought"}}

Rules:
- total is the amount actually charged to the card: the grand total after tax, tips, shipping and
  discounts. If the document shows an amount paid that differs from the invoice total, use the amount paid.
- currency is a 3-letter code. Infer it from symbols and addresses when it is not printed
  ($ with a Canadian address = CAD, $ with a US address = USD, EUR for euro).
- If the file is not an invoice or receipt (a statement, a contract, a blank page), set is_invoice to false
  and say what it is in summary. If it holds several receipts, describe the first one.
- Never guess digits: card_last4 only when four digits of a card number are visible.
"""


def parse_month_folder(name):
    m = MONTH_RE.match(clean(name))
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        return None
    return f"{year} {month:02d}"


def walk(drive, root_id, month_folders=None, max_depth=6):
    """Yield file records under the Invoice root.

    month_folders: set of 'YYYY MM' names to restrict the walk to (None = all).
    Folders named like a month become the record's month_folder; the first
    folder level below it is the vendor_folder."""
    wanted = set(month_folders) if month_folders else None

    def recurse(folder_id, path, month_folder, vendor_folder, depth):
        if depth > max_depth:
            return
        for f in drive.list_children(folder_id):
            name = f.get("name", "")
            if f.get("mimeType") == FOLDER:
                parsed = parse_month_folder(name) if not month_folder else None
                if parsed:
                    if wanted is not None and parsed not in wanted:
                        continue
                    yield from recurse(f["id"], f"{path}/{name}", parsed, "", depth + 1)
                else:
                    sub_vendor = vendor_folder or (name if month_folder else "")
                    yield from recurse(f["id"], f"{path}/{name}", month_folder, sub_vendor, depth + 1)
                continue
            if wanted is not None and not month_folder:
                continue
            yield {
                "id": f["id"], "file_name": name, "mime_type": f.get("mimeType", ""),
                "web_view_link": f.get("webViewLink", ""), "modified_time": f.get("modifiedTime", ""),
                "size": int(f.get("size") or 0), "folder_path": path, "month_folder": month_folder or "",
                "vendor_folder": vendor_folder, "_file": f,
            }

    yield from recurse(root_id, "", None, "", 0)


def gemini_mime(record):
    mime = (record.get("mime_type") or "").lower()
    if mime in EXPORT_AS_PDF:
        return "application/pdf"
    if mime in SUPPORTED_MIME:
        return "image/jpeg" if mime == "image/jpg" else mime
    if mime in TEXT_MIME:
        return mime
    ext = record.get("file_name", "").rsplit(".", 1)[-1].lower() if "." in record.get("file_name", "") else ""
    return EXT_MIME.get(ext, "")


def extract_invoice(gemini, drive, record):
    """Download one file and ask Gemini for its facts. Returns the row to store."""
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {k: record[k] for k in ("id", "file_name", "mime_type", "web_view_link", "folder_path",
                                  "month_folder", "vendor_folder", "modified_time", "size")}
    row.update({"extracted_at": now, "extraction_error": None})
    mime = gemini_mime(record)
    if not mime:
        row["extraction_error"] = f"unsupported file type {record.get('mime_type') or record['file_name']}"
        row["is_invoice"] = False
        return row
    if record.get("size", 0) > MAX_BYTES:
        row["extraction_error"] = "unsupported: file larger than 15 MB"
        row["is_invoice"] = False
        return row
    try:
        data, actual = drive.download(record["_file"])
        if actual in EXPORT_AS_PDF:
            actual = "application/pdf"
        prompt = INVOICE_PROMPT.format(month=record.get("month_folder") or "-",
                                       vendor=record.get("vendor_folder") or "-", name=record["file_name"])
        if mime in TEXT_MIME:
            parts = [gemini.text(prompt + "\nThe file's text:\n" + data.decode("utf-8", errors="replace")[:60000])]
        else:
            parts = [gemini.inline(data, actual if actual in SUPPORTED_MIME else mime), gemini.text(prompt)]
        facts = gemini.generate_json(parts, model=gemini.extract_model)
        if isinstance(facts, list):
            facts = facts[0] if facts else {}
        row.update({
            "is_invoice": bool(facts.get("is_invoice", True)),
            "vendor": clean(facts.get("vendor"))[:200],
            "invoice_number": clean(facts.get("invoice_number"))[:100] or None,
            "invoice_date": parse_date(facts.get("invoice_date")) or None,
            "total": parse_amount(facts.get("total")),
            "currency": (clean(facts.get("currency")) or "").upper()[:3] or None,
            "card_last4": last4(facts.get("card_last4")) or None,
            "payment_method": clean(facts.get("payment_method"))[:100] or None,
            "summary": clean(facts.get("summary"))[:500],
        })
    except Exception as exc:  # noqa: BLE001 - stored per file, the run goes on
        row["extraction_error"] = str(exc)[:500]
    return row


def index_invoices(store, drive, gemini, root_id, month_folders=None, limit=40, workers=4, log=print):
    """Read every new or changed file under the Invoice root (optionally only some
    month folders), at most `limit` per call. Returns counts and what is left."""
    known = store.invoice_index()
    files = list(walk(drive, root_id, month_folders))
    todo, moved = [], []
    for rec in files:
        prior = known.get(rec["id"])
        if prior is None or (prior.get("modified_time") or "") != rec["modified_time"]:
            todo.append(rec)
        elif prior.get("extraction_error") and not str(prior["extraction_error"]).startswith("unsupported"):
            todo.append(rec)
        else:
            moved.append(rec)   # unchanged content; refresh folder/name metadata only
    if moved:
        store.upsert_invoices([{k: r[k] for k in ("id", "file_name", "web_view_link", "folder_path",
                                                 "month_folder", "vendor_folder")} for r in moved])
    batch = todo[:limit]
    rows, errors = [], []
    if batch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for row in pool.map(lambda r: extract_invoice(gemini, drive, r), batch):
                rows.append(row)
                if row.get("extraction_error"):
                    errors.append({"file": row["file_name"], "error": row["extraction_error"]})
                else:
                    log(f"indexed {row['month_folder']}/{row['vendor_folder']}/{row['file_name']}: "
                        f"{row.get('vendor')} {row.get('total')} {row.get('currency')} {row.get('invoice_date')}")
        store.upsert_invoices(rows)
    return {"files_seen": len(files), "indexed": len(rows), "remaining": max(len(todo) - len(batch), 0),
            "errors": errors, "month_folders": sorted(month_folders) if month_folders else "all"}
