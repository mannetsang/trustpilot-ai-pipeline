"""Storage for the AP credit-card reconciliation service.

Two implementations with the same methods:

- SupabaseStore: the Supabase Postgres project, reached through PostgREST with
  the service-role key. Server side only; the key never reaches the browser.
- MemoryStore: in-process dicts. Used by the tests and by `STORE=memory` local
  runs, so the dashboard can be tried without a database.

Tables are created by ../schema.sql (see README). Amounts are floats here and
numeric(12,2) in Postgres; dates are ISO strings ("2026-09-04").
"""

import datetime as dt
import json
import re
import threading
import uuid

import requests

TRANSACTION_FIELDS = (
    "id", "statement_id", "card_last4", "holder_name", "txn_date", "post_date", "year", "month",
    "description", "supplier", "city", "country", "merchant_category", "type", "amount", "currency",
    "source_amount", "source_currency", "invoice_status", "invoice_id", "match_confidence",
    "match_method", "match_note", "cost_center", "usage", "note", "updated_by", "updated_at",
    "created_at", "raw", "rejected_invoice_ids", "invoice_part", "person_id", "also_invoice_ids",
)
CARD_FIELDS = ("last4", "label", "holder_name", "owner_email", "chat_space", "active", "updated_at",
               "group_name", "card_number", "primary_last4", "name_on_card", "person_id",
               "default_cost_center", "receipt_upload", "bank", "network", "product")
# What the face of a card shows (schema.sql, "The card as issued"): cards.network takes one of these or
# null (a check constraint); cards.product is at most CARD_PRODUCT_MAX characters. cards.bank is a key
# of main.BANKS, a registry in code, so it has no constraint of its own.
CARD_NETWORKS = ("visa", "mastercard", "amex")
CARD_PRODUCT_MAX = 60
INVOICE_FIELDS = (
    "id", "file_name", "mime_type", "web_view_link", "folder_path", "month_folder", "vendor_folder",
    "modified_time", "size", "vendor", "invoice_number", "invoice_date", "total", "currency",
    "card_last4", "payment_method", "summary", "is_invoice", "extracted_at", "extraction_error",
    "document_type", "removed_at", "card_charges", "charge_date", "paid_invoice_numbers",
)
# The admin pages' tables (schema.sql, "Admin portal"). Inserts carry every key of their tuple, so
# PostgREST gets uniform rows and MemoryStore keeps the same keys Postgres would.
COST_CENTER_FIELDS = ("name", "sort", "active", "description", "updated_by", "updated_at")
USE_CASE_FIELDS = ("id", "name", "description", "group_name", "sort", "active",
                   "created_by", "created_at", "updated_by", "updated_at")
AUDIT_FIELDS = ("actor", "auth_mode", "entity", "entity_id", "action", "before", "after", "note")
PERSON_VIEW = ("id", "name", "email")                    # what list_people/get_person/create_person return
PERSON_FIELDS = ("id", "name", "email", "created_by", "created_at", "updated_by", "updated_at")
VENDOR_FIELDS = ("id", "name", "kind", "code_each", "active", "note",
                 "created_by", "created_at", "updated_by", "updated_at")
VENDOR_FOLDER_FIELDS = ("id", "vendor_id", "folder", "folder_key", "created_by", "created_at")
VENDOR_PATTERN_FIELDS = ("id", "vendor_id", "kind", "match_type", "pattern", "status", "source", "note",
                         "created_by", "created_at", "updated_by", "updated_at")
VENDOR_CODING_FIELDS = ("id", "vendor_id", "group_name", "use_case_id", "mode",
                        "created_by", "created_at", "updated_by", "updated_at")
# The catalogue (admin.py) reads every transaction and invoice once per 5 minutes: only these columns.
ADMIN_TXN_COLUMNS = ("id", "card_last4", "type", "txn_date", "year", "month", "description", "supplier",
                     "amount", "currency", "source_amount", "source_currency", "invoice_status", "invoice_id",
                     "also_invoice_ids", "match_method", "match_note", "cost_center", "usage", "person_id",
                     "updated_by")
ADMIN_INVOICE_COLUMNS = ("id", "file_name", "web_view_link", "vendor_folder", "vendor", "month_folder",
                         "invoice_date", "total", "currency", "card_last4", "document_type", "removed_at")
AUDIT_LIST_COLUMNS = ("id", "at", "actor", "auth_mode", "entity", "entity_id", "action", "before", "after", "note")
NOTIFICATION_VIEW = ("card_last4", "year", "month", "sent_at", "sent_by", "ok", "detail")
# schema_status(): probe name -> (table, column) read with limit 1. False means schema.sql is not applied yet.
SCHEMA_PROBES = (
    ("admin_audit", ("admin_audit", "id")),
    ("use_cases", ("use_cases", "id")),
    ("cost_centers.active", ("cost_centers", "active")),
    ("cards.default_cost_center", ("cards", "default_cost_center")),
    ("cards.receipt_upload", ("cards", "receipt_upload")),
    ("cards.bank", ("cards", "bank")),
    ("people.updated_at", ("people", "updated_at")),
    ("vendors", ("vendors", "id")),
    ("vendor_folders", ("vendor_folders", "id")),
    ("vendor_patterns", ("vendor_patterns", "id")),
    ("vendor_coding", ("vendor_coding", "id")),
)

def _whole_unless_part(patch):
    """A patch that sets invoice_id without saying which charge links the whole invoice: invoice_part is
    cleared, so a row never keeps the part of a link it no longer has (a PATCH leaves unnamed columns).
    Likewise the documents paid with it (also_invoice_ids): a new link, or none, starts without them."""
    if "invoice_id" in patch and "invoice_part" not in patch:
        patch = dict(patch, invoice_part=None)
    if "invoice_id" in patch and "also_invoice_ids" not in patch:
        patch = dict(patch, also_invoice_ids=[])
    if "also_invoice_ids" in patch:              # one spelling of a set: sorted, no repeats, never null
        patch = dict(patch, also_invoice_ids=sorted({d for d in patch["also_invoice_ids"] or () if d}))
    return patch


def _parts(rows):
    """{invoice id: parts linked}: a listed charge's index, "whole" for a whole link, and "whole" for every
    document a row's charge also pays (also_invoice_ids)."""
    out = {}
    for r in rows:
        if r.get("invoice_id"):
            part = r.get("invoice_part")
            out.setdefault(r["invoice_id"], set()).add("whole" if part is None else int(part))
        for doc in r.get("also_invoice_ids") or ():
            out.setdefault(doc, set()).add("whole")
    return out


def _also_conflict(rows, txn_id, after):
    """The document a row's link would share with another row, or None. The rules of schema.sql's
    transactions_invoice_link_guard and unique indexes: an invoice is linked whole to one row, or one row
    per listed charge, never both; and a row's also document is nobody else's invoice or also document."""
    wanted, part = after.get("invoice_id"), after.get("invoice_part")
    also = set(after.get("also_invoice_ids") or ())
    if wanted and wanted in also:
        return wanted
    for o in rows:
        if o["id"] == txn_id:
            continue
        theirs = set(o.get("also_invoice_ids") or ())
        if wanted and (wanted in theirs or (o.get("invoice_id") == wanted and (
                part is None or o.get("invoice_part") is None or o.get("invoice_part") == part))):
            return wanted
        clash = also & (theirs | ({o["invoice_id"]} if o.get("invoice_id") else set()))
        if clash:
            return sorted(clash)[0]
    return None


PAGE = 1000
NEVER = "1970-01-01T00:00:00Z"


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(when):
    """A UTC time as PostgREST and the lease compare it: 2026-11-02T14:05:00Z."""
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _key(text):
    """How names are compared (admin.name_key): single spaces, case ignored."""
    return " ".join(str(text or "").split()).casefold()


def _blank_to_none(value):
    """An e-mail of "" or blanks is no e-mail (NULL): two people without one never clash on
    people_email_unique."""
    if value is None:
        return None
    return str(value).strip() or None


def _row(fields, data, **defaults):
    """A row with every key of `fields` (None where nothing was given), then `defaults` for the keys
    still None: the columns that are NOT NULL with a default in Postgres, where an explicit null fails."""
    row = {k: data.get(k) for k in fields}
    for k, v in defaults.items():
        if row.get(k) is None:
            row[k] = v
    return row


def _only(patch, fields, never=("id", "created_by", "created_at")):
    """The patch's keys that are columns of the table, without the ones no update may change."""
    return {k: v for k, v in (patch or {}).items() if k in fields and k not in never}


def _is_uuid(value):
    return bool(UUID_RE.match(str(value or "")))


def _conflict(message):
    """MemoryStore's unique violation, with the status and code PostgREST gives one."""
    return StoreError(f"duplicate key (23505): {message}", status=409, code="23505")


def _missing_fk(message):
    """MemoryStore's foreign-key violation, with the status and code PostgREST gives one."""
    return StoreError(f"foreign key (23503): {message}", status=409, code="23503")


def _check_violation(message):
    """MemoryStore's check-constraint violation, with the status and code PostgREST gives one."""
    return StoreError(f"check constraint (23514): {message}", status=400, code="23514")


def _card_checks(row):
    """schema.sql's cards_network_check and cards_product_check, for MemoryStore."""
    if row.get("network") is not None and row["network"] not in CARD_NETWORKS:
        raise _check_violation(f"cards.network {row['network']!r} is not one of {', '.join(CARD_NETWORKS)}")
    if row.get("product") is not None and len(row["product"]) > CARD_PRODUCT_MAX:
        raise _check_violation(f"cards.product is longer than {CARD_PRODUCT_MAX} characters")


def person_named(people, name):
    """The person in `people` with this name, ignoring case and extra spaces, else None."""
    key = " ".join(str(name or "").split()).lower()
    return next((p for p in people if " ".join(str(p.get("name") or "").split()).lower() == key), None)


def month_folder_name(year, month):
    """(2026, 9) -> '2026 09', the folder convention under the AP Invoice folder."""
    return f"{int(year)} {int(month):02d}"


def neighbouring_months(year, month, before=1, after=1):
    """[(year, month)] from `before` months earlier to `after` months later, inclusive."""
    out = []
    for delta in range(-before, after + 1):
        m = int(month) - 1 + delta
        out.append((int(year) + m // 12, m % 12 + 1))
    return out


MISSING_CODES = {"42P01", "42703", "PGRST204", "PGRST205"}   # table / column not there (schema not applied)


class StoreError(RuntimeError):
    """A store failure. status and code are PostgREST's HTTP status and Postgres/PostgREST error code
    when the server answered (None for a network failure); the message keeps the response text, so
    `"23505" in str(exc)` still finds a unique violation."""

    def __init__(self, message="", status=None, code=None):
        super().__init__(message)
        self.status, self.code = status, code

    @property
    def missing(self):
        """The table or column does not exist: schema.sql has not been applied yet."""
        text = str(self)
        return self.code in MISSING_CODES or (self.status == 404 and ("does not exist" in text or "Could not find" in text))

    @property
    def conflict(self):
        """A unique or foreign-key violation (the routes answer 409)."""
        return self.code in ("23505", "23503") or self.status == 409


# ---------------------------------------------------------------------------
# Supabase (PostgREST)
# ---------------------------------------------------------------------------
class SupabaseStore:
    def __init__(self, url, service_key, timeout=60):
        if not url or not service_key:
            raise StoreError("SUPABASE_URL and SUPABASE_SERVICE_KEY are required (or STORE=memory)")
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}",
                        "Content-Type": "application/json"}
        self.timeout = timeout

    # -- low level ----------------------------------------------------------
    def _req(self, method, table, params=None, body=None, prefer=None, want_response=False):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        try:
            r = requests.request(method, f"{self.base}/{table}", params=params, json=body,
                                 headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            # A reset or a timeout is a store failure too, so the callers' clean-up (trash an
            # upload whose row could not be written) runs; a timed-out write may still have landed.
            raise StoreError(f"{method} {table} -> {type(exc).__name__}: {exc}") from exc
        if r.status_code >= 400:
            # PostgREST's error body is {"code", "message", ...}: the code says what failed (a unique
            # violation, a table or column the schema does not have yet) without parsing the text.
            code = None
            try:
                body = r.json()
                code = body.get("code") if isinstance(body, dict) else None
            except ValueError:
                pass
            raise StoreError(f"{method} {table} -> {r.status_code} {r.text[:400]}", status=r.status_code,
                             code=str(code) if code else None)
        if want_response:
            return r
        if r.status_code == 204 or not r.text:
            return []
        return r.json()

    def select(self, table, params=None):
        """Every matching row. Pages with offset/limit and stops on the exact count
        PostgREST reports in Content-Range, so the project's max-rows setting
        (1000 by default on Supabase, but adjustable) cannot truncate a result."""
        out, offset, total = [], 0, None
        while True:
            page = dict(params or {})
            page["offset"], page["limit"] = offset, PAGE
            r = self._req("GET", table, page, prefer="count=exact", want_response=True)
            rows = r.json() if r.text else []
            out.extend(rows)
            offset += len(rows)
            total = _content_range_total(r.headers.get("Content-Range"), total)
            if not rows or (total is not None and offset >= total):
                return out

    def insert(self, table, rows, returning=False):
        if not rows:
            return []
        out = []
        for start in range(0, len(rows), 500):
            res = self._req("POST", table, None, rows[start:start + 500],
                            prefer="return=representation" if returning else "return=minimal")
            out.extend(res)
        return out

    def upsert(self, table, rows, on_conflict):
        if not rows:
            return
        for start in range(0, len(rows), 500):
            self._req("POST", table, {"on_conflict": on_conflict}, rows[start:start + 500],
                      prefer="resolution=merge-duplicates,return=minimal")

    def patch(self, table, filters, patch):
        return self._req("PATCH", table, filters, patch, prefer="return=representation")

    def delete(self, table, filters):
        return self._req("DELETE", table, filters, prefer="return=representation")

    @staticmethod
    def _in(values):
        quoted = ",".join('"' + str(v).replace('"', '\\"') + '"' for v in values)
        return f"in.({quoted})"

    # -- cards --------------------------------------------------------------
    def list_cards(self):
        return self.select("cards", {"order": "last4"})

    def upsert_cards(self, rows):
        """New cards are inserted as full rows, known ones are patched with just the
        fields given: PostgREST wants every object of a bulk payload to carry the
        same keys, and a merge upsert would write nulls over the columns a partial
        row leaves out."""
        if not rows:
            return
        known = {c["last4"] for c in self.select("cards", {"select": "last4", "order": "last4"})}
        new, seen = [], set()
        # New rows carry the columns the batch names (all of them, for PostgREST): a statement bringing a card
        # never names a column it does not set, so it imports whatever columns the table has grown since.
        keys = [k for k in CARD_FIELDS if k in ("last4", "active") or any(k in r for r in rows)]
        for r in rows:
            if r["last4"] in known or r["last4"] in seen:
                continue
            seen.add(r["last4"])
            row = {k: r.get(k) for k in keys}
            if row["active"] is None:
                row["active"] = True
            new.append(row)
        self.insert("cards", new)
        for r in rows:
            if r["last4"] in known:
                patch = {k: v for k, v in r.items() if k != "last4" and k in CARD_FIELDS}
                if patch:
                    self.patch("cards", {"last4": f"eq.{r['last4']}"}, patch)

    # -- statements ---------------------------------------------------------
    def insert_statement(self, row):
        row = dict(row)
        for key in ("period_start", "period_end"):
            row[key] = row.get(key) or None        # '' is not a date for Postgres
        return self.insert("statements", [row], returning=True)[0]

    def delete_statement(self, statement_id):
        """Remove an upload and its transactions that nobody has touched (no cost
        centre, usage, note, person or manual invoice decision). Returns the counts."""
        removed = self.delete("transactions", {
            "statement_id": f"eq.{statement_id}", "cost_center": "is.null", "usage": "is.null",
            "note": "is.null", "person_id": "is.null", "or": "(match_method.is.null,match_method.neq.manual)"})
        kept = self.select("transactions", {"select": "id", "statement_id": f"eq.{statement_id}", "order": "id"})
        for t in kept:
            self.patch("transactions", {"id": f"eq.{t['id']}"}, {"statement_id": None})
        gone = self.delete("statements", {"id": f"eq.{statement_id}"})
        return {"deleted": bool(gone), "transactions_removed": len(removed), "transactions_kept": len(kept)}

    def get_statement(self, statement_id):
        rows = self.select("statements", {"id": f"eq.{statement_id}"})
        return rows[0] if rows else None

    def list_statements(self, year=None, month=None):
        params = {"order": "uploaded_at.desc"}
        if year and month:
            params["months"] = "cs.{" + f'"{month_folder_name(year, month)}"' + "}"
        return self.select("statements", params)

    # -- transactions -------------------------------------------------------
    def existing_transaction_ids(self, ids):
        found = set()
        ids = list(ids)
        for start in range(0, len(ids), 200):
            rows = self.select("transactions", {"select": "id", "order": "id", "id": self._in(ids[start:start + 200])})
            found.update(r["id"] for r in rows)
        return found

    def insert_transactions(self, rows):
        """Uniform rows (same keys everywhere, as PostgREST requires), empty dates as null."""
        shaped = []
        for r in rows:
            row = {k: r.get(k) for k in TRANSACTION_FIELDS}
            for key in ("post_date", "txn_date", "updated_at", "source_currency", "invoice_id"):
                row[key] = row.get(key) or None
            if row.get("rejected_invoice_ids") is None:
                row["rejected_invoice_ids"] = []
            if row.get("also_invoice_ids") is None:
                row["also_invoice_ids"] = []
            shaped.append(row)
        self.insert("transactions", shaped)

    def list_transactions(self, year, month, card=None, statuses=None):
        params = {"year": f"eq.{int(year)}", "month": f"eq.{int(month)}",
                  "order": "txn_date.asc,card_last4.asc,amount.desc"}
        if card:
            params["card_last4"] = f"eq.{card}"
        if statuses:
            params["invoice_status"] = self._in(statuses)
        return self.select("transactions", params)

    def get_transaction(self, txn_id):
        rows = self.select("transactions", {"id": f"eq.{txn_id}"})
        return rows[0] if rows else None

    def update_transaction(self, txn_id, patch, expect=None):
        """The updated row, or None when there is no such row or, with `expect` ({column: value}),
        when the row no longer holds those values (someone changed it meanwhile): compare and set.
        A patch that sets invoice_id without invoice_part links the whole invoice (the part is cleared)."""
        patch = _whole_unless_part(patch)
        filters = {"id": f"eq.{txn_id}"}
        for column, value in (expect or {}).items():
            if isinstance(value, (list, tuple, set)):      # an array column: its exact value, sorted
                filters[column] = "eq.{" + ",".join('"' + str(v).replace('"', '\\"') + '"' for v in sorted(value)) + "}"
            else:
                filters[column] = "is.null" if value is None else f"eq.{value}"
        rows = self.patch("transactions", filters, patch)
        return rows[0] if rows else None

    def used_invoice_ids(self):
        """The ids of the documents linked to a transaction: its invoice, and the documents it also pays."""
        return set(self.used_invoice_parts())

    def used_invoice_parts(self):
        """{invoice id: the parts linked}: the listed card charges' indexes, "whole" for a whole link and for
        a document a charge also pays (also_invoice_ids)."""
        rows = self.select("transactions", {"select": "invoice_id,invoice_part,also_invoice_ids", "order": "id",
                                            "invoice_id": "not.is.null"})
        return _parts(rows)

    def transactions_for_invoice(self, invoice_id):
        """The transactions linked to the document as their invoice."""
        return self.select("transactions", {"invoice_id": f"eq.{invoice_id}", "order": "id"})

    def transactions_with_also(self, ids):
        """The transactions that hold any of these documents in also_invoice_ids (paid or confirmed with
        their invoice)."""
        ids = list(ids)
        out = {}
        for start in range(0, len(ids), 50):
            quoted = ",".join('"' + str(i).replace('"', '\\"') + '"' for i in ids[start:start + 50])
            for r in self.select("transactions", {"also_invoice_ids": f"ov.{{{quoted}}}", "order": "id"}):
                out[r["id"]] = r
        return list(out.values())

    def last_txn_date(self, card):
        """The date of the card's last statement line, or None: how far its statements reach."""
        r = self._req("GET", "transactions", {"select": "txn_date", "card_last4": f"eq.{card}",
                                               "order": "txn_date.desc", "limit": 1})
        return str(r[0]["txn_date"])[:10] if r else None

    def purchases_of_amount(self, amount, start, end):
        """The purchases of this amount within a cent, as billed or as their foreign amount, dated from start
        to end (ISO dates, both included), whatever their card or month: whether a payment is the only
        one of its amount."""
        lo, hi = f"{float(amount) - 0.01:.2f}", f"{float(amount) + 0.01:.2f}"
        return self.select("transactions", {
            "type": "eq.purchase", "txn_date": f"gte.{start}", "and": f"(txn_date.lte.{end})",
            "or": f"(and(amount.gte.{lo},amount.lte.{hi}),and(source_amount.gte.{lo},source_amount.lte.{hi}))",
            "order": "txn_date.asc,id.asc"})

    def linked_folders(self, prefix):
        """{vendor folder (lower case): how many purchases whose description starts with `prefix` were linked
        to a document in it, by the rules or by hand}: which supplier a card descriptor has been before."""
        rows = self.select("transactions", {"select": "invoice_id", "description": f"ilike.{prefix}*",
                                            "invoice_status": "eq.matched", "match_method": "in.(rule,manual)",
                                            "invoice_id": "not.is.null", "order": "id"})
        out = {}
        for inv in self.list_invoices(ids=sorted({r["invoice_id"] for r in rows})) if rows else []:
            folder = (inv.get("vendor_folder") or "").strip().lower()
            if folder:
                out[folder] = out.get(folder, 0) + sum(1 for r in rows if r["invoice_id"] == inv["id"])
        return out

    def first_txn_date(self, card):
        """The date of the card's first statement line, or None."""
        r = self._req("GET", "transactions", {"select": "txn_date", "card_last4": f"eq.{card}",
                                               "order": "txn_date.asc", "limit": 1})
        return str(r[0]["txn_date"])[:10] if r else None

    def invoice_card_numbers(self):
        """The card numbers (last four) invoices not removed show."""
        rows = self.select("invoices", {"select": "card_last4", "order": "id", "removed_at": "is.null",
                                        "card_last4": "not.is.null"})
        return {r["card_last4"] for r in rows if r.get("card_last4")}

    def record_inferred_charges(self, invoice_id, charges, read_charges, read_extracted_at):
        """Set card_charges to charges the matcher inferred, only while the invoice still holds the
        reading they were worked out from (no re-read since). True when written."""
        filters = {"id": f"eq.{invoice_id}", "removed_at": "is.null",
                   "extracted_at": f"eq.{read_extracted_at}" if read_extracted_at else "is.null"}
        if read_charges is None:
            filters["card_charges"] = "is.null"
        else:
            filters["card_charges->0->>inferred"] = "is.null"     # never over charges inferred meanwhile
        return bool(self.patch("invoices", filters, {"card_charges": charges}))

    def split_invoice_charges(self):
        """[{id, month_folder, card_charges}] for the invoices not removed that list two or more card
        charges (refunds included: matching.shared decides): what the matcher needs to find the orders
        charged in a month, whatever month they are filed in."""
        return self.select("invoices", {"select": "id,month_folder,card_charges", "order": "id",
                                        "removed_at": "is.null", "card_charges->1": "not.is.null"})

    def month_summary(self):
        return self.select("month_summary", {"order": "year.desc,month.desc,card_last4.asc"})

    # -- invoices -----------------------------------------------------------
    def invoice_index(self):
        """{file id: {modified_time, extraction_error, month_folder, vendor_folder, removed_at}} for every
        invoice seen so far."""
        rows = self.select("invoices", {"select": "id,modified_time,extraction_error,month_folder,vendor_folder,removed_at",
                                        "order": "id"})
        return {r["id"]: r for r in rows}

    def list_invoices(self, month_folders=None, ids=None):
        params = {"order": "invoice_date.asc,file_name.asc,id.asc"}
        if month_folders is not None:
            if not month_folders:
                return []
            params["month_folder"] = self._in(month_folders)
        if ids is not None:
            ids = list(ids)
            if not ids:
                return []
            out = []
            for start in range(0, len(ids), 100):     # keep each URL short
                page = dict(params, id=self._in(ids[start:start + 100]))
                out.extend(self.select("invoices", page))
            return out
        return self.select("invoices", params)

    def get_invoice(self, invoice_id):
        rows = self.select("invoices", {"id": f"eq.{invoice_id}"})
        return rows[0] if rows else None

    def upsert_invoices(self, rows):
        """Full rows only. Postgres checks NOT NULL (file_name) on the row an upsert proposes to
        insert before it finds the conflict, so a partial row fails even when the id exists:
        stamp known rows with update_invoices instead."""
        self.upsert("invoices", rows, "id")

    def update_invoices(self, ids, patch):
        """Set the same columns on existing invoices (removed_at, extraction_error)."""
        ids = list(ids)
        for start in range(0, len(ids), 100):     # keep each URL short
            self._req("PATCH", "invoices", {"id": self._in(ids[start:start + 100])}, patch, prefer="return=minimal")

    # -- misc ---------------------------------------------------------------
    def log_notification(self, row):
        self.insert("notifications", [row])

    def list_notifications(self, year, month):
        return self.select("notifications", {"year": f"eq.{int(year)}", "month": f"eq.{int(month)}",
                                             "order": "sent_at.desc"})

    def list_cost_centers(self):
        """The active cost centres' names, the dashboard's picker. Filtered here, not in the query, so it
        also works before schema.sql gave the table its active column."""
        return [r["name"] for r in self.select("cost_centers", {"order": "sort.asc,name.asc"})
                if r.get("active", True) is not False]

    # -- people a transaction can be assigned to -----------------------------
    def list_people(self):
        return self.select("people", {"select": "id,name,email", "order": "name.asc"})

    def get_person(self, person_id):
        if not UUID_RE.match(str(person_id or "")):
            return None                     # not an id Postgres would even parse
        rows = self.select("people", {"select": "id,name,email", "id": f"eq.{person_id}"})
        return rows[0] if rows else None

    def create_person(self, name, email=None, created_by=None):
        """(person, created): a new person, or the one already named so (case aside). An e-mail another
        person has (case aside) raises StoreError 23505 from people_email_unique."""
        same = person_named(self.list_people(), name)
        if same:
            return same, False
        try:
            row = self.insert("people", [{"name": name, "email": _blank_to_none(email), "created_by": created_by}],
                              returning=True)[0]
        except StoreError:
            same = person_named(self.list_people(), name)   # added by someone else a moment ago
            if same:
                return same, False
            raise                                           # not the name: the e-mail is taken
        return {k: row.get(k) for k in PERSON_VIEW}, True

    # -- the Drive import: one row per file read, and its last check -------
    def list_statement_sources(self):
        return self.select("statement_sources", {"order": "file_id"})

    def save_statement_source(self, row):
        """A full row (every column, as the import writes it): insert or replace."""
        self.upsert("statement_sources", [dict(row)], "file_id")

    def get_state(self, key):
        rows = self.select("app_state", {"key": f"eq.{key}"})
        return rows[0]["value"] if rows else {}

    def set_state(self, key, value):
        self.upsert("app_state", [{"key": key, "value": value, "updated_at": now_iso()}], "key")

    # -- one lease for the long steps, shared by the service and the Cloud Run jobs ----------
    def take_lease(self, name, holder, job, ttl, take_over=""):
        """True when `holder` now holds the lease: it was free (expired), already this holder's,
        or held by a holder starting with `take_over`. A conditional PATCH, so two takers at the
        same moment cannot both win."""
        now = dt.datetime.now(dt.timezone.utc)
        when = [f"expires_at.lt.{stamp(now)}", f"holder.eq.{holder}"]
        if take_over:
            when.append(f"holder.like.{take_over}*")
        row = {"holder": holder, "job": job, "acquired_at": stamp(now),
               "expires_at": stamp(now + dt.timedelta(seconds=ttl))}
        if self.patch("job_lease", {"name": f"eq.{name}", "or": "(" + ",".join(when) + ")"}, row):
            return True
        if self.select("job_lease", {"name": f"eq.{name}", "select": "name"}):
            return False
        # No row yet: the first taker creates it; a second one at the same moment is ignored.
        made = self._req("POST", "job_lease", None, [dict(row, name=name)],
                         prefer="resolution=ignore-duplicates,return=representation")
        return bool(made)

    def renew_lease(self, name, holder, ttl):
        until = stamp(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl))
        return bool(self.patch("job_lease", {"name": f"eq.{name}", "holder": f"eq.{holder}"}, {"expires_at": until}))

    def release_lease(self, name, holder):
        self.patch("job_lease", {"name": f"eq.{name}", "holder": f"eq.{holder}"},
                   {"holder": None, "job": None, "expires_at": NEVER})

    def get_lease(self, name):
        rows = self.select("job_lease", {"name": f"eq.{name}"})
        return rows[0] if rows else None

    # -- the admin pages (/admin) -------------------------------------------
    def _count(self, table, params):
        """How many rows match, from Content-Range (count=exact) without reading them."""
        query = dict(params or {})
        query.setdefault("select", "id")
        query["limit"] = 1
        r = self._req("GET", table, query, prefer="count=exact", want_response=True)
        total = _content_range_total(r.headers.get("Content-Range"), None)
        if total is None:
            total = len(r.json() if r.text else [])
        return total

    def _one(self, rows, fields):
        return {k: rows[0].get(k) for k in fields} if rows else None

    # audit: append only
    def insert_audit(self, row):
        self.insert("admin_audit", [{k: row.get(k) for k in AUDIT_FIELDS}])

    def list_audit(self, entity=None, entity_id=None, actor=None, before_id=None, limit=50):
        """Newest first, one request (no paging: the caller asks for at most 200)."""
        params = {"select": ",".join(AUDIT_LIST_COLUMNS), "order": "id.desc", "limit": int(limit)}
        if entity:
            params["entity"] = f"eq.{entity}"
        if entity_id:
            params["entity_id"] = f"eq.{entity_id}"
        actor = re.sub(r"[*,()]", "", str(actor or "")).strip()    # PostgREST's wildcard and list syntax
        if actor:
            params["actor"] = f"ilike.*{actor}*"
        if before_id is not None:
            params["id"] = f"lt.{int(before_id)}"
        return self._req("GET", "admin_audit", params)

    # cost centres: places and entities, retired instead of deleted
    def list_cost_center_rows(self):
        rows = self.select("cost_centers", {"order": "sort.asc,name.asc"})
        # select=* so this works before schema.sql added the columns: a missing one reads as unset, and a
        # missing active column as active (every row was, then).
        return [dict({k: r.get(k) for k in COST_CENTER_FIELDS}, active=r.get("active", True) is not False)
                for r in rows]

    def add_cost_center(self, row):
        full = _row(COST_CENTER_FIELDS, dict(row, active=True), sort=100)
        return self._one(self.insert("cost_centers", [full], returning=True), COST_CENTER_FIELDS)

    def update_cost_center(self, name, patch):
        """PATCH by exact name; a rename cascades to cards.default_cost_center (on update cascade)."""
        patch = _only(patch, COST_CENTER_FIELDS, never=())
        if not patch:
            return next((r for r in self.list_cost_center_rows() if r["name"] == name), None)
        return self._one(self.patch("cost_centers", {"name": f"eq.{name}"}, patch), COST_CENTER_FIELDS)

    # use cases: the Use case box's list
    def list_use_cases(self, active_only=False):
        rows = self.select("use_cases", {"order": "sort.asc,name.asc"})
        rows = [{k: r.get(k) for k in USE_CASE_FIELDS} for r in rows if not active_only or r.get("active")]
        return sorted(rows, key=lambda r: (r.get("sort") or 0, str(r.get("name") or "").casefold()))

    def create_use_case(self, row):
        full = _row(USE_CASE_FIELDS, row, id=str(uuid.uuid4()), created_at=now_iso(), active=True, sort=100)
        return self._one(self.insert("use_cases", [full], returning=True), USE_CASE_FIELDS)

    def update_use_case(self, use_case_id, patch):
        if not _is_uuid(use_case_id):
            return None
        patch = _only(patch, USE_CASE_FIELDS)
        if not patch:
            return self._one(self.select("use_cases", {"id": f"eq.{use_case_id}"}), USE_CASE_FIELDS)
        return self._one(self.patch("use_cases", {"id": f"eq.{use_case_id}"}, patch), USE_CASE_FIELDS)

    def coding_values_in_use(self):
        rows = self.select("transactions", {"select": "cost_center,usage", "order": "id",
                                            "or": "(cost_center.not.is.null,usage.not.is.null)"})
        return _count_values(rows)

    # cards
    def card_usage(self, last4):
        """What keeps a card from being deleted: its transactions, the uploads that list it, and the
        supplementary cards linked under it."""
        return {"transactions": self._count("transactions", {"card_last4": f"eq.{last4}"}),
                "statements": self._count("statements", {"cards": 'cs.{"' + str(last4) + '"}'}),
                "supplementary": self._count("cards", {"select": "last4", "primary_last4": f"eq.{last4}"})}

    def delete_card(self, last4):
        """True when deleted. A card with transactions fails on their foreign key (409 23503)."""
        return bool(self.delete("cards", {"last4": f"eq.{last4}"}))

    def latest_notifications(self):
        rows = self.select("notifications", {"select": ",".join(NOTIFICATION_VIEW), "order": "sent_at.desc,id.desc"})
        return _latest_per_card(rows)

    # people
    def list_people_rows(self):
        rows = self.select("people", {"order": "name.asc"})
        return sorted(({k: r.get(k) for k in PERSON_FIELDS} for r in rows), key=lambda p: str(p["name"]).casefold())

    def update_person(self, person_id, patch):
        if not _is_uuid(person_id):
            return None
        patch = _only(patch, ("name", "email", "updated_by", "updated_at"))
        if "email" in patch:
            patch["email"] = _blank_to_none(patch["email"])
        if not patch:
            return self.get_person(person_id)
        return self._one(self.patch("people", {"id": f"eq.{person_id}"}, patch), PERSON_VIEW)

    def delete_person(self, person_id):
        """True when deleted; transactions and cards keep no person (on delete set null)."""
        if not _is_uuid(person_id):
            return False
        return bool(self.delete("people", {"id": f"eq.{person_id}"}))

    def people_usage(self):
        rows = self.select("transactions", {"select": "person_id", "person_id": "not.is.null", "order": "id"})
        out = {}
        for r in rows:
            out[r["person_id"]] = out.get(r["person_id"], 0) + 1
        return out

    def reassign_person_transactions(self, from_id, to_id):
        if not (_is_uuid(from_id) and _is_uuid(to_id)):
            return 0
        rows = self._req("PATCH", "transactions", {"person_id": f"eq.{from_id}", "select": "id"},
                         {"person_id": to_id}, prefer="return=representation")
        return len(rows)

    # vendor registry: reference data in Round A (matching does not read it)
    def list_vendor_registry(self):
        return {name: [{k: r.get(k) for k in fields} for r in self.select(table, {"order": "id.asc"})]
                for name, table, fields in _REGISTRY_TABLES}

    def create_vendor(self, row):
        full = _row(VENDOR_FIELDS, row, id=str(uuid.uuid4()), created_at=now_iso(), kind="supplier",
                    code_each=False, active=True)
        return self._one(self.insert("vendors", [full], returning=True), VENDOR_FIELDS)

    def update_vendor(self, vendor_id, patch):
        if not _is_uuid(vendor_id):
            return None
        patch = _only(patch, VENDOR_FIELDS)
        if not patch:
            return self._one(self.select("vendors", {"id": f"eq.{vendor_id}"}), VENDOR_FIELDS)
        return self._one(self.patch("vendors", {"id": f"eq.{vendor_id}"}, patch), VENDOR_FIELDS)

    def add_vendor_folder(self, row):
        full = _row(VENDOR_FOLDER_FIELDS, row, id=str(uuid.uuid4()), created_at=now_iso(),
                    folder_key=_key(row.get("folder")))
        return self._one(self.insert("vendor_folders", [full], returning=True), VENDOR_FOLDER_FIELDS)

    def delete_vendor_folder(self, folder_id):
        if not _is_uuid(folder_id):
            return False
        return bool(self.delete("vendor_folders", {"id": f"eq.{folder_id}"}))

    def add_vendor_pattern(self, row):
        full = _row(VENDOR_PATTERN_FIELDS, row, id=str(uuid.uuid4()), created_at=now_iso(), kind="descriptor",
                    match_type="prefix", status="probation", source="admin")
        return self._one(self.insert("vendor_patterns", [full], returning=True), VENDOR_PATTERN_FIELDS)

    def update_vendor_pattern(self, pattern_id, patch):
        if not _is_uuid(pattern_id):
            return None
        patch = _only(patch, VENDOR_PATTERN_FIELDS, never=("id", "vendor_id", "created_by", "created_at"))
        if not patch:
            return self._one(self.select("vendor_patterns", {"id": f"eq.{pattern_id}"}), VENDOR_PATTERN_FIELDS)
        return self._one(self.patch("vendor_patterns", {"id": f"eq.{pattern_id}"}, patch), VENDOR_PATTERN_FIELDS)

    def delete_vendor_pattern(self, pattern_id):
        if not _is_uuid(pattern_id):
            return False
        return bool(self.delete("vendor_patterns", {"id": f"eq.{pattern_id}"}))

    def _vendor_coding_row(self, vendor_id, group_name):
        # Compared in Python, not with ilike: % and _ in a group name would act as wildcards.
        rows = self.select("vendor_coding", {"vendor_id": f"eq.{vendor_id}", "order": "id.asc"})
        return next((r for r in rows if _key(r.get("group_name")) == _key(group_name)), None)

    def set_vendor_coding(self, vendor_id, group_name, use_case_id, actor):
        """The vendor's default use case for a card group ("" = any card): updated in place, or added."""
        if not (_is_uuid(vendor_id) and _is_uuid(use_case_id)):
            return None
        now = now_iso()
        row = self._vendor_coding_row(vendor_id, group_name)
        if row:
            return self._one(self.patch("vendor_coding", {"id": f"eq.{row['id']}"},
                                        {"use_case_id": use_case_id, "updated_by": actor, "updated_at": now}),
                             VENDOR_CODING_FIELDS)
        full = _row(VENDOR_CODING_FIELDS, {"vendor_id": vendor_id, "group_name": group_name or "",
                                           "use_case_id": use_case_id, "created_by": actor},
                    id=str(uuid.uuid4()), created_at=now, mode="suggest")
        return self._one(self.insert("vendor_coding", [full], returning=True), VENDOR_CODING_FIELDS)

    def delete_vendor_coding(self, vendor_id, group_name):
        if not _is_uuid(vendor_id):
            return False
        row = self._vendor_coding_row(vendor_id, group_name)
        return bool(row) and bool(self.delete("vendor_coding", {"id": f"eq.{row['id']}"}))

    # the catalogue's rows (admin.py caches them for 5 minutes)
    def admin_transaction_rows(self):
        return self.select("transactions", {"select": ",".join(ADMIN_TXN_COLUMNS), "order": "id"})

    def admin_invoice_rows(self):
        return self.select("invoices", {"select": ",".join(ADMIN_INVOICE_COLUMNS), "order": "id"})

    def schema_status(self):
        """{probe: bool}: whether schema.sql's admin-portal tables and columns exist. Any failure other
        than "not there" is raised (a store that is down is not a schema that is missing)."""
        out = {}
        for name, (table, column) in SCHEMA_PROBES:
            try:
                self._req("GET", table, {"select": column, "limit": 1})
                out[name] = True
            except StoreError as exc:
                if not exc.missing:
                    raise
                out[name] = False
        return out


_REGISTRY_TABLES = (("vendors", "vendors", VENDOR_FIELDS), ("folders", "vendor_folders", VENDOR_FOLDER_FIELDS),
                    ("patterns", "vendor_patterns", VENDOR_PATTERN_FIELDS),
                    ("coding", "vendor_coding", VENDOR_CODING_FIELDS))


def _count_values(rows):
    """{"cost_center": {value: n}, "usage": {value: n}}: values trimmed, empty ones skipped."""
    out = {"cost_center": {}, "usage": {}}
    for r in rows:
        for column in ("cost_center", "usage"):
            value = str(r.get(column) or "").strip()
            if value:
                out[column][value] = out[column].get(value, 0) + 1
    return out


def _latest_per_card(rows):
    """{last4: the first row per card} of rows ordered newest first."""
    out = {}
    for r in rows:
        last4 = r.get("card_last4")
        if last4 and last4 not in out:
            out[last4] = {k: r.get(k) for k in NOTIFICATION_VIEW}
    return out


def _content_range_total(header, previous):
    """'0-24/25' -> 25, '*/0' -> 0, '0-999/*' or missing -> previous."""
    if not header or "/" not in header:
        return previous
    total = header.rsplit("/", 1)[1].strip()
    return int(total) if total.isdigit() else previous


# ---------------------------------------------------------------------------
# In-memory (tests, STORE=memory)
# ---------------------------------------------------------------------------
# The places and entities a purchase belongs to (schema.sql's seed), and what purchases are for: the
# expense types that were cost centres until the coding split of Nov 2026.
DEFAULT_COST_CENTERS = [("Head office", 100), ("Salon - Dufferin", 110), ("Salon - Ridgeway", 111),
                        ("Salon - Consumer", 112), ("Salon - Eglinton", 113), ("Salon - STC", 114),
                        ("Salon - Rapistan", 115), ("Salon - Brampton", 116), ("US", 120), ("EU", 121)]
DEFAULT_USE_CASES = [("Marketing", 10), ("IT & Software", 20), ("Shipping & Logistics", 30), ("Travel", 40),
                     ("Meals & Entertainment", 50), ("Office & Supplies", 60), ("Inventory / COGS", 70),
                     ("Professional Services", 80), ("Utilities & Telecom", 90),
                     ("Insurance & Government", 100), ("Other", 999)]
SPLIT_ACTOR = "schema:coding_split"


def _cost_center_order(r):
    return (r.get("sort") or 0, str(r["name"]).casefold(), r["name"])


class MemoryStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.cards, self.transactions, self.invoices = {}, {}, {}
        self.statements, self.notifications = [], []
        self.statement_sources, self.state, self.leases = {}, {}, {}
        now = now_iso()
        # The state the live database has after the coding split: the places active, the 11 expense
        # types retired (never deleted), and the same 11 names as use cases.
        self.cost_centers = ([_row(COST_CENTER_FIELDS, {"name": n, "sort": o, "active": True})
                              for n, o in DEFAULT_COST_CENTERS]
                             + [_row(COST_CENTER_FIELDS, {"name": n, "sort": o, "active": False,
                                                          "updated_by": SPLIT_ACTOR, "updated_at": now})
                                for n, o in DEFAULT_USE_CASES])
        self.use_cases = {}
        for n, o in DEFAULT_USE_CASES:
            row = _row(USE_CASE_FIELDS, {"id": str(uuid.uuid4()), "name": n, "sort": o, "active": True,
                                         "created_by": SPLIT_ACTOR, "created_at": now})
            self.use_cases[row["id"]] = row
        self.state["coding_split"] = {"at": now, "retired": [n for n, _ in DEFAULT_USE_CASES], "kept_in_use": [],
                                      "memory": True}
        self.audit, self._audit_seq = [], 0
        self.vendors, self.vendor_folders, self.vendor_patterns, self.vendor_coding = {}, {}, {}, {}
        self.people = {}
        self._seq = 0

    def list_cards(self):
        return [dict(c) for _, c in sorted(self.cards.items())]

    def upsert_cards(self, rows):
        with self.lock:
            # cards.default_cost_center references cost_centers(name), and the face's check constraints:
            # checked before anything is written.
            names = {c["name"] for c in self.cost_centers}
            for r in rows:
                if r.get("default_cost_center") is not None and r["default_cost_center"] not in names:
                    raise _missing_fk(f"cards.default_cost_center {r['default_cost_center']!r} is not a cost centre")
                _card_checks(r)
            for r in rows:
                self.cards.setdefault(r["last4"], {}).update(r)

    def insert_statement(self, row):
        with self.lock:
            self._seq += 1
            row = dict(row, id=f"st_{self._seq}")
            self.statements.append(row)
            return dict(row)

    def delete_statement(self, statement_id):
        with self.lock:
            removable = [t["id"] for t in self.transactions.values() if t.get("statement_id") == statement_id
                         and not (t.get("cost_center") or t.get("usage") or t.get("note") or t.get("person_id"))
                         and t.get("match_method") != "manual"]
            for tid in removable:
                del self.transactions[tid]
            kept = [t for t in self.transactions.values() if t.get("statement_id") == statement_id]
            for t in kept:
                t["statement_id"] = None
            before = len(self.statements)
            self.statements = [s for s in self.statements if s["id"] != statement_id]
            return {"deleted": len(self.statements) < before, "transactions_removed": len(removable),
                    "transactions_kept": len(kept)}

    def get_statement(self, statement_id):
        return next((dict(r) for r in self.statements if r["id"] == statement_id), None)

    def list_statements(self, year=None, month=None):
        rows = self.statements
        if year and month:
            name = month_folder_name(year, month)
            rows = [s for s in rows if name in (s.get("months") or [])]
        return [dict(s) for s in sorted(rows, key=lambda s: s.get("uploaded_at", ""), reverse=True)]

    def existing_transaction_ids(self, ids):
        return {i for i in ids if i in self.transactions}

    def insert_transactions(self, rows):
        with self.lock:
            for r in rows:
                if r["id"] in self.transactions:
                    raise StoreError(f"duplicate transaction id {r['id']}")
                self.transactions[r["id"]] = dict(r, also_invoice_ids=sorted(r.get("also_invoice_ids") or ()))

    def list_transactions(self, year, month, card=None, statuses=None):
        rows = [t for t in self.transactions.values()
                if t["year"] == int(year) and t["month"] == int(month)
                and (not card or t["card_last4"] == card)
                and (not statuses or t["invoice_status"] in statuses)]
        rows.sort(key=lambda t: (t["txn_date"], t["card_last4"], -float(t["amount"])))
        return [dict(t) for t in rows]

    def get_transaction(self, txn_id):
        t = self.transactions.get(txn_id)
        return dict(t) if t else None

    def update_transaction(self, txn_id, patch, expect=None):
        with self.lock:
            t = self.transactions.get(txn_id)
            if not t:
                return None
            if expect and any((sorted(t.get(k) or ()) != sorted(v) if isinstance(v, (list, tuple, set)) else t.get(k) != v)
                              for k, v in expect.items()):
                return None                 # changed meanwhile: the caller's compare-and-set fails
            patch = _whole_unless_part(patch)
            after = dict(t, **patch)
            # Same rules as schema.sql: the unique indexes transactions_invoice_whole_unique and
            # transactions_invoice_part_unique, and the trigger that keeps an invoice from being linked
            # whole and by charge at once, or a document a charge also pays from being anyone else's.
            clash = _also_conflict(self.transactions.values(), txn_id, after)
            if clash:
                raise StoreError(f"invoice {clash} is already linked to another transaction")
            t.update(patch)
            return dict(t)

    def transactions_for_invoice(self, invoice_id):
        return [dict(t) for t in self.transactions.values() if t.get("invoice_id") == invoice_id]

    def transactions_with_also(self, ids):
        ids = set(ids)
        return [dict(t) for t in self.transactions.values() if ids & set(t.get("also_invoice_ids") or ())]

    def last_txn_date(self, card):
        dates = [str(t["txn_date"])[:10] for t in self.transactions.values() if t.get("card_last4") == card]
        return max(dates) if dates else None

    def used_invoice_ids(self):
        return set(self.used_invoice_parts())

    def used_invoice_parts(self):
        return _parts([t for t in self.transactions.values() if t.get("invoice_id")])

    def purchases_of_amount(self, amount, start, end):
        def near(v):
            return v is not None and abs(float(v) - float(amount)) <= 0.0101
        return [dict(t) for t in sorted(self.transactions.values(), key=lambda t: (str(t.get("txn_date")), t["id"]))
                if t.get("type") == "purchase" and (near(t["amount"]) or near(t.get("source_amount")))
                and start <= str(t.get("txn_date"))[:10] <= end]

    def linked_folders(self, prefix):
        out = {}
        for t in self.transactions.values():
            if (t.get("invoice_status") == "matched" and t.get("match_method") in ("rule", "manual") and t.get("invoice_id")
                    and str(t.get("description") or "").lower().startswith(prefix.lower())):
                folder = ((self.invoices.get(t["invoice_id"]) or {}).get("vendor_folder") or "").strip().lower()
                if folder:
                    out[folder] = out.get(folder, 0) + 1
        return out

    def first_txn_date(self, card):
        dates = [str(t["txn_date"])[:10] for t in self.transactions.values() if t.get("card_last4") == card]
        return min(dates) if dates else None

    def invoice_card_numbers(self):
        return {r["card_last4"] for r in self.invoices.values() if r.get("card_last4") and not r.get("removed_at")}

    def record_inferred_charges(self, invoice_id, charges, read_charges, read_extracted_at):
        with self.lock:
            r = self.invoices.get(invoice_id)
            held = r.get("card_charges") if r else None
            if (not r or r.get("removed_at") or r.get("extracted_at") != read_extracted_at
                    or (read_charges is None and held is not None)
                    or (held and isinstance(held[0], dict) and held[0].get("inferred"))):
                return False
            r["card_charges"] = [dict(c) for c in charges]
            return True

    def split_invoice_charges(self):
        return [{"id": r["id"], "month_folder": r.get("month_folder"), "card_charges": r.get("card_charges")}
                for r in sorted(self.invoices.values(), key=lambda r: r["id"])
                if not r.get("removed_at") and isinstance(r.get("card_charges"), list) and len(r["card_charges"]) > 1]

    def month_summary(self):
        groups = {}
        for t in self.transactions.values():
            g = groups.setdefault((t["year"], t["month"], t["card_last4"]), {
                "year": t["year"], "month": t["month"], "card_last4": t["card_last4"],
                "transactions": 0, "spend": 0.0, "matched": 0, "possible": 0, "missing": 0,
                "waived": 0, "uncoded": 0})
            g["transactions"] += 1
            if t["type"] == "purchase":
                g["spend"] = round(g["spend"] + float(t["amount"]), 2)
                if not str(t.get("usage") or "").strip():        # uncoded: no use case (schema.sql's view)
                    g["uncoded"] += 1
            if t["invoice_status"] in ("matched", "possible", "missing", "waived"):
                g[t["invoice_status"]] += 1
        return sorted(groups.values(), key=lambda g: (-g["year"], -g["month"], g["card_last4"]))

    def invoice_index(self):
        return {i: {"modified_time": v.get("modified_time"), "extraction_error": v.get("extraction_error"),
                    "month_folder": v.get("month_folder"), "vendor_folder": v.get("vendor_folder"),
                    "removed_at": v.get("removed_at")}
                for i, v in self.invoices.items()}

    def list_invoices(self, month_folders=None, ids=None):
        rows = list(self.invoices.values())
        if month_folders is not None:
            rows = [r for r in rows if r.get("month_folder") in set(month_folders)]
        if ids is not None:
            rows = [r for r in rows if r["id"] in set(ids)]
        rows.sort(key=lambda r: (r.get("invoice_date") or "", r.get("file_name") or ""))
        return [dict(r) for r in rows]

    def get_invoice(self, invoice_id):
        r = self.invoices.get(invoice_id)
        return dict(r) if r else None

    def upsert_invoices(self, rows):
        with self.lock:
            for r in rows:
                # created_at is set once, as the column's default does in Postgres.
                self.invoices.setdefault(r["id"], {"created_at": now_iso()}).update(r)

    def update_invoices(self, ids, patch):
        with self.lock:
            for i in ids:
                if i in self.invoices:
                    self.invoices[i].update(patch)

    def log_notification(self, row):
        self.notifications.append(dict(row))

    def list_notifications(self, year, month):
        return [dict(n) for n in self.notifications if n["year"] == int(year) and n["month"] == int(month)]

    def list_cost_centers(self):
        return [c["name"] for c in sorted(self.cost_centers, key=_cost_center_order) if c.get("active") is not False]

    @staticmethod
    def _person_view(p):
        return {k: p.get(k) for k in PERSON_VIEW}

    def list_people(self):
        return sorted((self._person_view(p) for p in self.people.values()), key=lambda p: p["name"].lower())

    def get_person(self, person_id):
        p = self.people.get(str(person_id or ""))
        return self._person_view(p) if p else None

    def _email_taken(self, email, but=None):
        """people_email_unique: lower(email), when not null."""
        return email is not None and any(p["id"] != but and p.get("email") is not None
                                         and p["email"].lower() == email.lower() for p in self.people.values())

    def create_person(self, name, email=None, created_by=None):
        with self.lock:
            same = person_named(self.people.values(), name)
            if same:
                return self._person_view(same), False
            email = _blank_to_none(email)
            if self._email_taken(email):
                raise _conflict(f"people_email_unique: {email}")
            person = _row(PERSON_FIELDS, {"id": str(uuid.uuid4()), "name": name, "email": email,
                                          "created_by": created_by, "created_at": now_iso()})
            self.people[person["id"]] = person
            return self._person_view(person), True

    def list_statement_sources(self):
        return [dict(r) for _, r in sorted(self.statement_sources.items())]

    def save_statement_source(self, row):
        with self.lock:
            self.statement_sources[row["file_id"]] = dict(row)

    def get_state(self, key):
        return dict(self.state.get(key) or {})

    def set_state(self, key, value):
        with self.lock:
            self.state[key] = json.loads(json.dumps(value))     # stored as JSON, like the jsonb column

    def take_lease(self, name, holder, job, ttl, take_over=""):
        now = dt.datetime.now(dt.timezone.utc)
        with self.lock:
            cur = self.leases.get(name)
            free = (not cur or cur["expires_at"] < stamp(now) or cur.get("holder") == holder
                    or (take_over and (cur.get("holder") or "").startswith(take_over)))
            if not free:
                return False
            self.leases[name] = {"name": name, "holder": holder, "job": job, "acquired_at": stamp(now),
                                 "expires_at": stamp(now + dt.timedelta(seconds=ttl))}
            return True

    def renew_lease(self, name, holder, ttl):
        with self.lock:
            cur = self.leases.get(name)
            if not cur or cur.get("holder") != holder:
                return False
            cur["expires_at"] = stamp(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl))
            return True

    def release_lease(self, name, holder):
        with self.lock:
            cur = self.leases.get(name)
            if cur and cur.get("holder") == holder:
                cur.update(holder=None, job=None, expires_at=NEVER)

    def get_lease(self, name):
        cur = self.leases.get(name)
        return dict(cur) if cur else None

    # -- the admin pages (/admin): the same rules as schema.sql's unique indexes, keys and cascades --
    def insert_audit(self, row):
        with self.lock:
            self._audit_seq += 1
            entry = {k: row.get(k) for k in AUDIT_FIELDS}
            for k in ("before", "after"):                    # stored as JSON, like the jsonb columns
                if entry[k] is not None:
                    entry[k] = json.loads(json.dumps(entry[k], default=str))
            self.audit.append(dict(entry, id=self._audit_seq, at=now_iso()))

    def list_audit(self, entity=None, entity_id=None, actor=None, before_id=None, limit=50):
        actor = re.sub(r"[*,()]", "", str(actor or "")).strip().lower()   # as SupabaseStore sends it
        rows = [r for r in reversed(self.audit)
                if (not entity or r["entity"] == entity) and (not entity_id or r["entity_id"] == entity_id)
                and (not actor or actor in str(r.get("actor") or "").lower())
                and (before_id is None or r["id"] < int(before_id))]
        return [{k: json.loads(json.dumps(r[k])) for k in AUDIT_LIST_COLUMNS} for r in rows[:int(limit)]]

    def list_cost_center_rows(self):
        return [dict(c) for c in sorted(self.cost_centers, key=_cost_center_order)]

    def _cost_center_clash(self, name, but=None):
        """cost_centers_name_lower_unique, leaving out the row being renamed (named `but`)."""
        return any(c["name"] != but and c["name"].lower() == str(name).lower() for c in self.cost_centers)

    def add_cost_center(self, row):
        with self.lock:
            full = _row(COST_CENTER_FIELDS, dict(row, active=True), sort=100)
            if self._cost_center_clash(full["name"]):
                raise _conflict(f"cost_centers_name_lower_unique: {full['name']}")
            self.cost_centers.append(full)
            return dict(full)

    def update_cost_center(self, name, patch):
        with self.lock:
            row = next((c for c in self.cost_centers if c["name"] == name), None)
            if row is None:
                return None
            patch = _only(patch, COST_CENTER_FIELDS, never=())
            new = patch.get("name", name)
            if new != name:
                if self._cost_center_clash(new, but=name):
                    raise _conflict(f"cost_centers_name_lower_unique: {new}")
                for card in self.cards.values():        # references cost_centers(name) on update cascade
                    if card.get("default_cost_center") == name:
                        card["default_cost_center"] = new
            row.update(patch)
            return dict(row)

    def list_use_cases(self, active_only=False):
        rows = sorted(self.use_cases.values(), key=lambda r: (r.get("sort") or 0, str(r["name"]).casefold()))
        return [dict(r) for r in rows if not active_only or r.get("active")]

    def _use_case_clash(self, name, but=None):
        return any(u["id"] != but and u["name"].lower() == str(name).lower() for u in self.use_cases.values())

    def create_use_case(self, row):
        with self.lock:
            full = _row(USE_CASE_FIELDS, row, id=str(uuid.uuid4()), created_at=now_iso(), active=True, sort=100)
            if self._use_case_clash(full["name"]) or full["id"] in self.use_cases:
                raise _conflict(f"use_cases_name_unique: {full['name']}")
            self.use_cases[full["id"]] = full
            return dict(full)

    def update_use_case(self, use_case_id, patch):
        if not _is_uuid(use_case_id):
            return None
        with self.lock:
            row = self.use_cases.get(use_case_id)
            if row is None:
                return None
            patch = _only(patch, USE_CASE_FIELDS)
            if "name" in patch and self._use_case_clash(patch["name"], but=use_case_id):
                raise _conflict(f"use_cases_name_unique: {patch['name']}")
            row.update(patch)
            return dict(row)

    def coding_values_in_use(self):
        return _count_values(self.transactions.values())

    def card_usage(self, last4):
        return {"transactions": sum(1 for t in self.transactions.values() if t.get("card_last4") == last4),
                "statements": sum(1 for s in self.statements if last4 in (s.get("cards") or ())),
                "supplementary": sum(1 for c in self.cards.values() if c.get("primary_last4") == last4)}

    def delete_card(self, last4):
        with self.lock:
            if last4 not in self.cards:
                return False
            if any(t.get("card_last4") == last4 for t in self.transactions.values()):
                raise _missing_fk(f"transactions reference card {last4}")    # transactions.card_last4
            del self.cards[last4]
            return True

    def latest_notifications(self):
        # Newest first, later entries first on a tie (Postgres: sent_at.desc,id.desc).
        order = sorted(enumerate(self.notifications), key=lambda p: (str(p[1].get("sent_at") or ""), p[0]), reverse=True)
        return _latest_per_card(n for _, n in order)

    def list_people_rows(self):
        return sorted((_row(PERSON_FIELDS, p) for p in self.people.values()), key=lambda p: str(p["name"]).casefold())

    def update_person(self, person_id, patch):
        if not _is_uuid(person_id):
            return None
        with self.lock:
            p = self.people.get(person_id)
            if p is None:
                return None
            patch = _only(patch, ("name", "email", "updated_by", "updated_at"))
            if "email" in patch:
                patch["email"] = _blank_to_none(patch["email"])
            if "name" in patch and any(o["id"] != person_id and o["name"].lower() == str(patch["name"]).lower()
                                       for o in self.people.values()):
                raise _conflict(f"people_name_unique: {patch['name']}")
            if "email" in patch and self._email_taken(patch["email"], but=person_id):
                raise _conflict(f"people_email_unique: {patch['email']}")
            p.update(patch)
            return self._person_view(p)

    def delete_person(self, person_id):
        if not _is_uuid(person_id):
            return False
        with self.lock:
            if self.people.pop(person_id, None) is None:
                return False
            for row in list(self.transactions.values()) + list(self.cards.values()):   # on delete set null
                if row.get("person_id") == person_id:
                    row["person_id"] = None
            return True

    def people_usage(self):
        out = {}
        for t in self.transactions.values():
            if t.get("person_id"):
                out[t["person_id"]] = out.get(t["person_id"], 0) + 1
        return out

    def reassign_person_transactions(self, from_id, to_id):
        if not (_is_uuid(from_id) and _is_uuid(to_id)):
            return 0
        with self.lock:
            moving = [t for t in self.transactions.values() if t.get("person_id") == from_id]
            if moving and to_id not in self.people:
                raise _missing_fk(f"transactions.person_id {to_id} is not a person")
            for t in moving:
                t["person_id"] = to_id
            return len(moving)

    def list_vendor_registry(self):
        return {name: [dict(r) for _, r in sorted(getattr(self, table).items())]
                for name, table, _ in _REGISTRY_TABLES}

    def create_vendor(self, row):
        with self.lock:
            full = _row(VENDOR_FIELDS, row, id=str(uuid.uuid4()), created_at=now_iso(), kind="supplier",
                        code_each=False, active=True)
            if full["id"] in self.vendors or any(v["name"].lower() == full["name"].lower() for v in self.vendors.values()):
                raise _conflict(f"vendors_name_unique: {full['name']}")
            self.vendors[full["id"]] = full
            return dict(full)

    def update_vendor(self, vendor_id, patch):
        if not _is_uuid(vendor_id):
            return None
        with self.lock:
            row = self.vendors.get(vendor_id)
            if row is None:
                return None
            patch = _only(patch, VENDOR_FIELDS)
            if "name" in patch and any(v["id"] != vendor_id and v["name"].lower() == str(patch["name"]).lower()
                                       for v in self.vendors.values()):
                raise _conflict(f"vendors_name_unique: {patch['name']}")
            row.update(patch)
            return dict(row)

    def _need_vendor(self, vendor_id):
        if vendor_id not in self.vendors:
            raise _missing_fk(f"vendor {vendor_id} does not exist")

    def add_vendor_folder(self, row):
        with self.lock:
            full = _row(VENDOR_FOLDER_FIELDS, row, id=str(uuid.uuid4()), created_at=now_iso(),
                        folder_key=_key(row.get("folder")))
            self._need_vendor(full["vendor_id"])
            if any(f["folder_key"] == full["folder_key"] for f in self.vendor_folders.values()):
                raise _conflict(f"vendor_folders_key_unique: {full['folder_key']}")
            self.vendor_folders[full["id"]] = full
            return dict(full)

    def delete_vendor_folder(self, folder_id):
        if not _is_uuid(folder_id):
            return False
        with self.lock:
            return self.vendor_folders.pop(folder_id, None) is not None

    def add_vendor_pattern(self, row):
        with self.lock:
            full = _row(VENDOR_PATTERN_FIELDS, row, id=str(uuid.uuid4()), created_at=now_iso(), kind="descriptor",
                        match_type="prefix", status="probation", source="admin")
            self._need_vendor(full["vendor_id"])
            if any((p["kind"], p["pattern"]) == (full["kind"], full["pattern"]) for p in self.vendor_patterns.values()):
                raise _conflict(f"vendor_patterns_unique: {full['kind']} {full['pattern']}")
            self.vendor_patterns[full["id"]] = full
            return dict(full)

    def update_vendor_pattern(self, pattern_id, patch):
        if not _is_uuid(pattern_id):
            return None
        with self.lock:
            row = self.vendor_patterns.get(pattern_id)
            if row is None:
                return None
            patch = _only(patch, VENDOR_PATTERN_FIELDS, never=("id", "vendor_id", "created_by", "created_at"))
            after = dict(row, **patch)
            if any(p["id"] != pattern_id and (p["kind"], p["pattern"]) == (after["kind"], after["pattern"])
                   for p in self.vendor_patterns.values()):
                raise _conflict(f"vendor_patterns_unique: {after['kind']} {after['pattern']}")
            row.update(patch)
            return dict(row)

    def delete_vendor_pattern(self, pattern_id):
        if not _is_uuid(pattern_id):
            return False
        with self.lock:
            return self.vendor_patterns.pop(pattern_id, None) is not None

    def _vendor_coding_row(self, vendor_id, group_name):
        return next((r for _, r in sorted(self.vendor_coding.items())
                     if r["vendor_id"] == vendor_id and _key(r.get("group_name")) == _key(group_name)), None)

    def set_vendor_coding(self, vendor_id, group_name, use_case_id, actor):
        if not (_is_uuid(vendor_id) and _is_uuid(use_case_id)):
            return None
        with self.lock:
            self._need_vendor(vendor_id)
            if use_case_id not in self.use_cases:
                raise _missing_fk(f"use case {use_case_id} does not exist")
            now = now_iso()
            row = self._vendor_coding_row(vendor_id, group_name)
            if row:
                row.update(use_case_id=use_case_id, updated_by=actor, updated_at=now)
                return dict(row)
            full = _row(VENDOR_CODING_FIELDS, {"vendor_id": vendor_id, "group_name": group_name or "",
                                               "use_case_id": use_case_id, "created_by": actor},
                        id=str(uuid.uuid4()), created_at=now, mode="suggest")
            self.vendor_coding[full["id"]] = full
            return dict(full)

    def delete_vendor_coding(self, vendor_id, group_name):
        if not _is_uuid(vendor_id):
            return False
        with self.lock:
            row = self._vendor_coding_row(vendor_id, group_name)
            return bool(row) and self.vendor_coding.pop(row["id"], None) is not None

    def admin_transaction_rows(self):
        return [dict({k: t.get(k) for k in ADMIN_TXN_COLUMNS}, also_invoice_ids=list(t.get("also_invoice_ids") or ()))
                for _, t in sorted(self.transactions.items())]

    def admin_invoice_rows(self):
        return [{k: r.get(k) for k in ADMIN_INVOICE_COLUMNS} for _, r in sorted(self.invoices.items())]

    def schema_status(self):
        return {name: True for name, _ in SCHEMA_PROBES}
