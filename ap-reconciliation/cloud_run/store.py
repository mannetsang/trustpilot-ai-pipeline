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
import threading

import requests

TRANSACTION_FIELDS = (
    "id", "statement_id", "card_last4", "holder_name", "txn_date", "post_date", "year", "month",
    "description", "supplier", "city", "country", "merchant_category", "type", "amount", "currency",
    "source_amount", "source_currency", "invoice_status", "invoice_id", "match_confidence",
    "match_method", "match_note", "cost_center", "usage", "note", "updated_by", "updated_at",
    "created_at", "raw",
)
INVOICE_FIELDS = (
    "id", "file_name", "mime_type", "web_view_link", "folder_path", "month_folder", "vendor_folder",
    "modified_time", "size", "vendor", "invoice_number", "invoice_date", "total", "currency",
    "card_last4", "payment_method", "summary", "is_invoice", "extracted_at", "extraction_error",
)

PAGE = 1000


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


class StoreError(RuntimeError):
    pass


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
    def _req(self, method, table, params=None, body=None, prefer=None):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        r = requests.request(method, f"{self.base}/{table}", params=params, json=body,
                             headers=headers, timeout=self.timeout)
        if r.status_code >= 400:
            raise StoreError(f"{method} {table} -> {r.status_code} {r.text[:400]}")
        if r.status_code == 204 or not r.text:
            return []
        return r.json()

    def select(self, table, params=None):
        """Every matching row, paging through PostgREST's row limit."""
        out, offset = [], 0
        while True:
            page = dict(params or {})
            page["offset"], page["limit"] = offset, PAGE
            rows = self._req("GET", table, page)
            out.extend(rows)
            if len(rows) < PAGE:
                return out
            offset += PAGE

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

    @staticmethod
    def _in(values):
        quoted = ",".join('"' + str(v).replace('"', '\\"') + '"' for v in values)
        return f"in.({quoted})"

    # -- cards --------------------------------------------------------------
    def list_cards(self):
        return self.select("cards", {"order": "last4"})

    def upsert_cards(self, rows):
        self.upsert("cards", rows, "last4")

    # -- statements ---------------------------------------------------------
    def insert_statement(self, row):
        return self.insert("statements", [row], returning=True)[0]

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
            rows = self.select("transactions", {"select": "id", "id": self._in(ids[start:start + 200])})
            found.update(r["id"] for r in rows)
        return found

    def insert_transactions(self, rows):
        self.insert("transactions", rows)

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

    def update_transaction(self, txn_id, patch):
        rows = self.patch("transactions", {"id": f"eq.{txn_id}"}, patch)
        return rows[0] if rows else None

    def used_invoice_ids(self):
        rows = self.select("transactions", {"select": "invoice_id", "invoice_id": "not.is.null"})
        return {r["invoice_id"] for r in rows}

    def month_summary(self):
        return self.select("month_summary", {"order": "year.desc,month.desc,card_last4.asc"})

    # -- invoices -----------------------------------------------------------
    def invoice_index(self):
        """{file id: {modified_time, extraction_error}} for every invoice seen so far."""
        rows = self.select("invoices", {"select": "id,modified_time,extraction_error"})
        return {r["id"]: r for r in rows}

    def list_invoices(self, month_folders=None, ids=None):
        params = {"order": "invoice_date.asc,file_name.asc"}
        if month_folders is not None:
            if not month_folders:
                return []
            params["month_folder"] = self._in(month_folders)
        if ids is not None:
            if not ids:
                return []
            params["id"] = self._in(ids)
        return self.select("invoices", params)

    def get_invoice(self, invoice_id):
        rows = self.select("invoices", {"id": f"eq.{invoice_id}"})
        return rows[0] if rows else None

    def upsert_invoices(self, rows):
        self.upsert("invoices", rows, "id")

    # -- misc ---------------------------------------------------------------
    def log_notification(self, row):
        self.insert("notifications", [row])

    def list_notifications(self, year, month):
        return self.select("notifications", {"year": f"eq.{int(year)}", "month": f"eq.{int(month)}",
                                             "order": "sent_at.desc"})

    def list_cost_centers(self):
        return [r["name"] for r in self.select("cost_centers", {"order": "sort.asc,name.asc"})]


# ---------------------------------------------------------------------------
# In-memory (tests, STORE=memory)
# ---------------------------------------------------------------------------
DEFAULT_COST_CENTERS = [
    "Marketing", "IT & Software", "Shipping & Logistics", "Travel", "Meals & Entertainment",
    "Office & Supplies", "Inventory / COGS", "Professional Services", "Utilities & Telecom",
    "Insurance & Government", "Salon - Dufferin", "Salon - Ridgeway", "Salon - Consumer",
    "Salon - Eglinton", "Salon - STC", "Salon - Rapistan", "Salon - Brampton", "US", "EU", "Other",
]


class MemoryStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.cards, self.transactions, self.invoices = {}, {}, {}
        self.statements, self.notifications = [], []
        self.cost_centers = list(DEFAULT_COST_CENTERS)
        self._seq = 0

    def list_cards(self):
        return [dict(c) for _, c in sorted(self.cards.items())]

    def upsert_cards(self, rows):
        with self.lock:
            for r in rows:
                self.cards.setdefault(r["last4"], {}).update(r)

    def insert_statement(self, row):
        with self.lock:
            self._seq += 1
            row = dict(row, id=f"st_{self._seq}")
            self.statements.append(row)
            return dict(row)

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
                self.transactions[r["id"]] = dict(r)

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

    def update_transaction(self, txn_id, patch):
        with self.lock:
            t = self.transactions.get(txn_id)
            if not t:
                return None
            t.update(patch)
            return dict(t)

    def used_invoice_ids(self):
        return {t["invoice_id"] for t in self.transactions.values() if t.get("invoice_id")}

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
                if not (t.get("cost_center") or ""):
                    g["uncoded"] += 1
            if t["invoice_status"] in ("matched", "possible", "missing", "waived"):
                g[t["invoice_status"]] += 1
        return sorted(groups.values(), key=lambda g: (-g["year"], -g["month"], g["card_last4"]))

    def invoice_index(self):
        return {i: {"modified_time": v.get("modified_time"), "extraction_error": v.get("extraction_error")}
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
                self.invoices.setdefault(r["id"], {}).update(r)

    def log_notification(self, row):
        self.notifications.append(dict(row))

    def list_notifications(self, year, month):
        return [dict(n) for n in self.notifications if n["year"] == int(year) and n["month"] == int(month)]

    def list_cost_centers(self):
        return list(self.cost_centers)
