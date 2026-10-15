"""Where dashboard-entered rows live: the GenC Consumption Supabase project (table
public.consumption, ../schema.sql), reached through PostgREST with the service-role key from the
server only. MemoryStore is the same interface in-process, for tests and local demos.

Deleting is soft (deleted_at / deleted_by), so a removed line can still be found in Supabase.
"""

import copy
import datetime as dt
import threading
import uuid

import requests

COLUMNS = ("id,entry_date,sku,reason,qty,unit_cost_cad,cost_source,subtotal_cad,request_by,sent_to,"
           "process_date,picked_up,issued,created_by,created_at,updated_by,updated_at,"
           "skuvault_status,skuvault_removed,skuvault_error,skuvault_started_at,skuvault_done_at,skuvault_by")
# A SkuVault removal may start when nothing is running, or when a run was left "pending" this long
# ago (an instance that died mid-way); see claim_skuvault.
CLAIMABLE = ("none", "failed", "partial")


class StoreError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _numbers(row):
    """PostgREST sends numeric columns as JSON numbers; make sure they are floats either way."""
    for key in ("qty", "unit_cost_cad", "subtotal_cad"):
        if row.get(key) is not None:
            row[key] = float(row[key])
    return row


class SupabaseStore:
    TABLE = "consumption"

    def __init__(self, url, service_key, timeout=30):
        if not url or not service_key:
            raise StoreError("SUPABASE_URL and SUPABASE_SERVICE_KEY must both be set")
        self.base = url.rstrip("/") + "/rest/v1/"
        self.timeout = timeout
        self.headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}",
                        "Content-Type": "application/json"}
        self.http = requests.Session()

    def _req(self, method, params=None, body=None, prefer=None):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        try:
            response = self.http.request(method, self.base + self.TABLE, params=params, json=body,
                                         headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise StoreError(f"database unreachable: {exc}") from exc
        if response.status_code >= 300:
            raise StoreError(f"database error: {method} {self.TABLE} -> {response.status_code} {response.text[:200]}",
                             response.status_code)
        return response.json() if response.content else []

    def list_entries(self):
        rows = self._req("GET", params={"select": COLUMNS, "deleted_at": "is.null",
                                        "order": "entry_date.asc,created_at.asc", "limit": "20000"})
        return [_numbers(r) for r in rows]

    def get_entry(self, entry_id):
        rows = self._req("GET", params={"select": COLUMNS, "id": f"eq.{entry_id}", "deleted_at": "is.null"})
        return _numbers(rows[0]) if rows else None

    def insert_entries(self, rows):
        out = self._req("POST", params={"select": COLUMNS}, body=rows, prefer="return=representation")
        return [_numbers(r) for r in out]

    def update_entry(self, entry_id, patch):
        out = self._req("PATCH", params={"select": COLUMNS, "id": f"eq.{entry_id}", "deleted_at": "is.null"},
                        body=patch, prefer="return=representation")
        return _numbers(out[0]) if out else None

    def claim_skuvault(self, entry_id, user, stale_before):
        """Mark the line "pending" if no other removal holds it: one conditional PATCH, so two
        requests racing for the same line cannot both get it. Returns the claimed line or None."""
        claimable = f"skuvault_status.in.({','.join(CLAIMABLE)})"
        stale = f"and(skuvault_status.eq.pending,skuvault_started_at.lt.{stale_before})"
        out = self._req("PATCH", params={"select": COLUMNS, "id": f"eq.{entry_id}", "deleted_at": "is.null",
                                         "or": f"({claimable},{stale})"},
                        body={"skuvault_status": "pending", "skuvault_started_at": now_iso(), "skuvault_by": user,
                              "skuvault_error": None}, prefer="return=representation")
        return _numbers(out[0]) if out else None

    def delete_entry(self, entry_id, user):
        out = self._req("PATCH", params={"select": "id", "id": f"eq.{entry_id}", "deleted_at": "is.null"},
                        body={"deleted_at": now_iso(), "deleted_by": user}, prefer="return=representation")
        return bool(out)

    def ping(self):
        self._req("GET", params={"select": "id", "limit": "1"})
        return True


class MemoryStore:
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()

    @staticmethod
    def _public(row):
        out = {k: v for k, v in row.items() if k not in ("deleted_at", "deleted_by")}
        cost = out.get("unit_cost_cad")
        out["subtotal_cad"] = None if cost is None else out["qty"] * cost
        return copy.deepcopy(out)

    def list_entries(self):
        with self.lock:
            live = [r for r in self.rows.values() if not r.get("deleted_at")]
            live.sort(key=lambda r: (r["entry_date"], r["created_at"]))
            return [self._public(r) for r in live]

    def get_entry(self, entry_id):
        with self.lock:
            row = self.rows.get(entry_id)
            return self._public(row) if row and not row.get("deleted_at") else None

    def insert_entries(self, rows):
        out = []
        with self.lock:
            for row in rows:
                stamp = now_iso()
                full = {"id": str(uuid.uuid4()), "created_at": stamp, "updated_at": stamp, "updated_by": None,
                        "picked_up": False, "issued": False, "process_date": None, "cost_source": "none",
                        "unit_cost_cad": None, "request_by": "", "sent_to": "", "skuvault_status": "none",
                        "skuvault_removed": [], "skuvault_error": None, "skuvault_started_at": None,
                        "skuvault_done_at": None, "skuvault_by": None}
                full.update(copy.deepcopy(row))
                self.rows[full["id"]] = full
                out.append(self._public(full))
        return out

    def update_entry(self, entry_id, patch):
        with self.lock:
            row = self.rows.get(entry_id)
            if not row or row.get("deleted_at"):
                return None
            row.update(copy.deepcopy(patch))
            return self._public(row)

    def claim_skuvault(self, entry_id, user, stale_before):
        with self.lock:
            row = self.rows.get(entry_id)
            if not row or row.get("deleted_at"):
                return None
            stale = row["skuvault_status"] == "pending" and (row.get("skuvault_started_at") or "") < stale_before
            if row["skuvault_status"] not in CLAIMABLE and not stale:
                return None
            row.update({"skuvault_status": "pending", "skuvault_started_at": now_iso(), "skuvault_by": user,
                        "skuvault_error": None})
            return self._public(row)

    def delete_entry(self, entry_id, user):
        with self.lock:
            row = self.rows.get(entry_id)
            if not row or row.get("deleted_at"):
                return False
            row.update({"deleted_at": now_iso(), "deleted_by": user})
            return True

    def ping(self):
        return True
