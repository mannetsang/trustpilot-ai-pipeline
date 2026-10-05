#!/usr/bin/env python3
"""File invoice PDFs from ap@superhairpieces.com's Gmail into Drive.

    My Drive / Invoice / <YYYY MM> / <Vendor> / <original file name>.pdf

For every Gmail message with a PDF attachment that this script hasn't handled
yet, Gemini (Vertex AI) reads each PDF and says whether it is an invoice,
bill, receipt or statement, who issued it, and its date. Invoices are filed by
the date printed on the document (the email's received date when Gemini finds
none); anything else is skipped.

Handled messages get a Gmail label so the next run skips them:
  "Invoice Filer/Filed"        at least one PDF was saved (or already was)
  "Invoice Filer/Not invoice"  every PDF was judged not to be an invoice
A message whose Gemini call or upload failed gets no label and is retried on
the next run. Search Gmail for label:invoice-filer-not-invoice to review skips;
remove the label to have a message reconsidered.

The same PDF forwarded several times is saved once: uploads carry an MD5 in
Drive, and anything already under Invoice/ with the same checksum is skipped.

Auth: Gmail and Drive act as ap@ through lib/google_workspace.py (a user OAuth
refresh token in Secret Manager); Vertex AI uses Application Default
Credentials (claude-sessions).

Usage:
    python invoice-filer/file_invoices.py [--since 2026-01-01] [--limit N] [--dry-run]
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.google_workspace import FOLDER_MIME, WorkspaceClient, WorkspaceError  # noqa: E402

GCP_PROJECT = os.environ.get("GCP_PROJECT", "shp-ai-bot-2026")
VERTEX_LOCATION = "us-central1"
VERTEX_MODEL = "gemini-2.5-flash"
TIMEZONE = ZoneInfo("America/Toronto")

ROOT_FOLDER = "Invoice"            # in ap@'s My Drive; created if missing
LABEL_FILED = "Invoice Filer/Filed"
LABEL_SKIPPED = "Invoice Filer/Not invoice"
APP_SOURCE = "invoice-filer"       # appProperties.source on everything we create
MAX_GEMINI_PDF_BYTES = 15 * 1024 * 1024  # larger PDFs are judged from the email alone

EXTRACTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_invoice": {"type": "BOOLEAN"},
        "document_type": {"type": "STRING"},
        "vendor": {"type": "STRING"},
        "invoice_date": {"type": "STRING"},
        "invoice_number": {"type": "STRING"},
    },
    "required": ["is_invoice", "document_type", "vendor", "invoice_date", "invoice_number"],
}

PROMPT = """You sort the accounts-payable mailbox of Superhairpieces, a hairpiece retailer.
Its group companies (Superhairpieces, New Frontier Global Ltd., True Top Inc.,
Frontier Beauty Supply Ltd., Gen'C Beauty) are usually the customer being billed.

Look at the attached PDF and the email it came with, then answer:

- is_invoice: true if the PDF is an invoice, bill, receipt, payment receipt,
  credit note or account statement from a supplier or service provider.
  false for anything else: quotes, purchase orders, contracts, agreements,
  packing lists, shipping labels, price lists, reports, marketing.
- document_type: a short label, e.g. "invoice", "receipt", "statement".
- vendor: the company that ISSUED the document and is owed or was paid - not
  the bill-to party, and not the colleague who forwarded the email. Use the
  short trading name in title case without legal suffixes ("GK Logistics", not
  "GK LOGISTICS INC."). If it is the same company as one of these existing
  vendor folders, return that folder name exactly: {vendors}
  Empty string if is_invoice is false.
- invoice_date: the issue date printed on the document as YYYY-MM-DD (for a
  statement, the statement date). Empty string if none is printed.
- invoice_number: as printed, or empty string.

Email
  From: {sender}
  Subject: {subject}
  Received: {received}
  Attachment file name: {filename}
  Body excerpt: {snippet}
"""


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
_vertex_creds = None


def vertex_token():
    global _vertex_creds
    import google.auth
    import google.auth.transport.requests
    if _vertex_creds is None:
        _vertex_creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not _vertex_creds.valid:
        _vertex_creds.refresh(google.auth.transport.requests.Request())
    return _vertex_creds.token


def classify(pdf, email, filename, vendors):
    prompt = PROMPT.format(
        vendors=", ".join(sorted(vendors)) or "(none yet)",
        sender=email["from"], subject=email["subject"],
        received=email["received"].strftime("%Y-%m-%d"), filename=filename,
        snippet=email["snippet"][:500])
    parts = [{"text": prompt}]
    if len(pdf) <= MAX_GEMINI_PDF_BYTES:
        parts.insert(0, {"inlineData": {"mimeType": "application/pdf",
                                        "data": base64.b64encode(pdf).decode()}})
    url = (f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT}"
           f"/locations/{VERTEX_LOCATION}/publishers/google/models/{VERTEX_MODEL}:generateContent")
    body = json.dumps({
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "responseSchema": EXTRACTION_SCHEMA},
    }).encode()
    for attempt in range(4):
        request = urllib.request.Request(url, data=body, headers={
            "Authorization": f"Bearer {vertex_token()}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.load(response)
            text = result["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except urllib.error.HTTPError as err:
            if err.code in (429, 500, 503) and attempt < 3:
                time.sleep(5 * 2 ** attempt)
                continue
            raise RuntimeError(f"Vertex AI HTTP {err.code}: {err.read()[:300]!r}") from err


# ---------------------------------------------------------------------------
# Drive tree: Invoice / <YYYY MM> / <Vendor>
# ---------------------------------------------------------------------------
def drive_name(text):
    """Strip characters that make Drive names awkward, and quote for queries."""
    return re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", text).strip(" .") or "Unknown"


LEGAL_SUFFIX = re.compile(
    r"[,\s]+(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|pbc|plc"
    r"|gmbh|s\.?a|b\.?v|ulc|lp|llp)\.?$", re.IGNORECASE)


def vendor_name(text):
    """'Anthropic, PBC' -> 'Anthropic'; repeated so 'Foo Co., Ltd.' -> 'Foo'."""
    name = drive_name(text)
    while True:
        shorter = LEGAL_SUFFIX.sub("", name).strip(" ,.")
        if shorter == name or not shorter:
            return name
        name = shorter


def q_escape(text):
    return text.replace("\\", "\\\\").replace("'", "\\'")


class InvoiceTree:
    def __init__(self, client, dry_run):
        self.client = client
        self.dry_run = dry_run
        self.root = self._find_or_create(ROOT_FOLDER, "root")
        self.months = {}   # "2026 09" -> folder id
        self.vendors = {}  # (month id, vendor lower) -> (folder id, name)
        self.names = {}    # folder id -> set of lower-cased file names
        self.md5s = set()
        self._index()

    def _children(self, parent, folders):
        kind = "=" if folders else "!="
        return self.client.list_files(
            f"'{parent}' in parents and mimeType {kind} '{FOLDER_MIME}' and trashed=false",
            fields="id,name,md5Checksum", limit=10000) if parent else []

    def _find_or_create(self, name, parent):
        found = self.client.list_files(
            f"'{parent}' in parents and name = '{q_escape(name)}' "
            f"and mimeType = '{FOLDER_MIME}' and trashed=false", fields="id,name", limit=1)
        if found:
            return found[0]["id"]
        if self.dry_run:
            return None
        return self.client.create_folder(name, parent, {"source": APP_SOURCE})["id"]

    def _index(self):
        for month in self._children(self.root, folders=True):
            self.months[month["name"]] = month["id"]
            for vendor in self._children(month["id"], folders=True):
                self.vendors[(month["id"], vendor["name"].lower())] = (vendor["id"], vendor["name"])
                files = self._children(vendor["id"], folders=False)
                self.names[vendor["id"]] = {f["name"].lower() for f in files}
                self.md5s.update(f["md5Checksum"] for f in files if f.get("md5Checksum"))

    def vendor_names(self):
        return {name for _, name in self.vendors.values()}

    def folder(self, month, vendor):
        """Folder id for Invoice/<month>/<vendor>, creating the missing levels."""
        if month not in self.months:
            self.months[month] = self._find_or_create(month, self.root)
        month_id = self.months[month]
        key = (month_id, vendor.lower())
        if key not in self.vendors:
            # Reuse a vendor folder name from another month so spelling stays consistent.
            vendor = next((n for n in self.vendor_names() if n.lower() == vendor.lower()), vendor)
            vendor_id = self._find_or_create(vendor, month_id) if month_id else None
            self.vendors[key] = (vendor_id, vendor)
            self.names.setdefault(vendor_id, set())
        return self.vendors[key][0]

    def unique_name(self, folder_id, filename):
        taken = self.names.setdefault(folder_id, set())
        stem, ext = os.path.splitext(filename)
        name, n = filename, 2
        while name.lower() in taken:
            name, n = f"{stem} ({n}){ext}", n + 1
        taken.add(name.lower())
        return name


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------
def pdf_parts(payload):
    """Yield (part_id, filename, attachment_id or inline data) for every PDF part."""
    stack = [payload]
    while stack:
        part = stack.pop()
        stack.extend(part.get("parts", []))
        filename = part.get("filename") or ""
        if part.get("mimeType") == "application/pdf" or filename.lower().endswith(".pdf"):
            body = part.get("body", {})
            if body.get("attachmentId") or body.get("data"):
                yield part.get("partId", ""), filename or "attachment.pdf", body


def decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def email_info(message):
    headers = {h["name"].lower(): h["value"] for h in message["payload"].get("headers", [])}
    received = dt.datetime.fromtimestamp(int(message["internalDate"]) / 1000, TIMEZONE)
    return {"from": headers.get("from", ""), "subject": headers.get("subject", ""),
            "received": received, "snippet": message.get("snippet", "")}


def month_for(invoice_date, received):
    """'YYYY MM' from the printed date when plausible, else from the email date."""
    try:
        date = dt.date.fromisoformat((invoice_date or "").strip())
        if dt.date(2000, 1, 1) <= date <= received.date() + dt.timedelta(days=60):
            return date.strftime("%Y %m"), "invoice date"
    except ValueError:
        pass
    return received.strftime("%Y %m"), "email date"


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", default="2026-01-01", help="only mail on/after this date")
    parser.add_argument("--limit", type=int, default=0, help="stop after N messages")
    parser.add_argument("--dry-run", action="store_true",
                        help="classify and print, but create, upload and label nothing")
    args = parser.parse_args()

    client = WorkspaceClient()
    tree = InvoiceTree(client, args.dry_run)
    label_filed = label_skipped = None
    if not args.dry_run:
        label_filed = client.ensure_label(LABEL_FILED)
        label_skipped = client.ensure_label(LABEL_SKIPPED)

    since = dt.date.fromisoformat(args.since).strftime("%Y/%m/%d")
    query = (f"has:attachment filename:pdf after:{since} "
             f"-label:invoice-filer-filed -label:invoice-filer-not-invoice")
    stubs = client.search_messages(query, limit=args.limit or 5000)
    stubs.reverse()  # oldest first, so vendor names settle in date order
    print(f"{len(stubs)} message(s) to check (query: {query})")

    counts = {"saved": 0, "duplicate": 0, "not invoice": 0, "error": 0}
    for i, stub in enumerate(stubs, 1):
        message = client.get_message(stub["id"])
        if {label_filed, label_skipped} & set(message.get("labelIds", [])) - {None}:
            continue
        email = email_info(message)
        print(f"[{i}/{len(stubs)}] {email['received']:%Y-%m-%d} {email['from'][:40]} | "
              f"{email['subject'][:70]}")
        outcomes = []
        for part_id, filename, body in pdf_parts(message["payload"]):
            try:
                if body.get("attachmentId"):
                    data = decode(client.get_attachment(stub["id"], body["attachmentId"])["data"])
                else:
                    data = decode(body["data"])
                md5 = hashlib.md5(data).hexdigest()
                if md5 in tree.md5s:
                    print(f"    = {filename}: already saved")
                    outcomes.append("duplicate")
                    continue
                info = classify(data, email, filename, tree.vendor_names())
                if not info.get("is_invoice") or not info.get("vendor", "").strip():
                    print(f"    - {filename}: not an invoice ({info.get('document_type')})")
                    outcomes.append("not invoice")
                    continue
                vendor = vendor_name(info["vendor"])
                month, basis = month_for(info.get("invoice_date"), email["received"])
                folder_id = tree.folder(month, vendor)
                name = tree.unique_name(folder_id, drive_name(filename))
                print(f"    + {ROOT_FOLDER}/{month}/{tree.vendors[(tree.months[month], vendor.lower())][1]}"
                      f"/{name}  ({info.get('document_type')}, {basis})")
                if not args.dry_run:
                    client.upload_file(name, folder_id, data, "application/pdf", {
                        "source": APP_SOURCE, "gmailMessageId": stub["id"],
                        "invoiceNumber": (info.get("invoice_number") or "")[:100]})
                tree.md5s.add(md5)
                outcomes.append("saved")
            except (WorkspaceError, RuntimeError, KeyError, ValueError) as exc:
                print(f"    ! {filename}: {exc}")
                outcomes.append("error")
        if not outcomes:  # "filename:pdf" matched, but no PDF part (e.g. inside an .eml)
            print("    - no PDF attachment")
            outcomes = ["not invoice"]
        for outcome in outcomes:
            counts[outcome] += 1
        if args.dry_run or "error" in outcomes:
            continue  # unlabelled: retried next run
        filed = {"saved", "duplicate"} & set(outcomes)
        client.modify_labels(stub["id"], add=[label_filed if filed else label_skipped])

    print("\nPDFs: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    if counts["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
