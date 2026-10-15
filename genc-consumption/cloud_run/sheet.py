"""Read the Google Sheet "office use": the 明细 rows from before the dashboard, the Note Ref
reasons and the Cost Ref costs.

The sheet stays the source of truth for its own rows: they are read live (cached for a minute),
never copied into Supabase and never written. Columns are found by their header text, so a column
inserted or moved in the sheet does not shift the data.

Dates come back as serial numbers (valueRenderOption UNFORMATTED_VALUE, dateTimeRenderOption
SERIAL_NUMBER): days since 1899-12-30, Google Sheets' epoch.
"""

import datetime as dt
import re
import threading
import time

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
EPOCH = dt.date(1899, 12, 30)

# Header text (normalised: lower case, whitespace collapsed) -> field.
HEADERS = {
    "date": "entry_date",
    "sku": "sku",
    "note": "reason",
    "qty": "qty",
    "cost cad": "unit_cost_cad",
    "subtotal amount cad": "subtotal_cad",
    "request by": "request_by",
    "sent to": "sent_to",
    "process date": "process_date",
    "拿货": "picked_up",
    "出库": "issued",
}
REQUIRED = ("entry_date", "sku", "qty")
DEFAULT_REASONS = ["视频需要", "样品需要", "客户需求", "跨部门"]


class SheetError(Exception):
    pass


def _norm(header):
    return re.sub(r"\s+", " ", str(header or "")).strip().lower()


def to_date(value):
    """A cell's date as an ISO string, or None. Serial numbers and typed YYYY-MM-DD both work."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not 20000 < value < 80000:          # 1954..2119: anything else is not a date
            return None
        return (EPOCH + dt.timedelta(days=int(value))).isoformat()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%Y.%m.%d"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def to_number(value):
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("$", "").replace(",", "").replace("CAD", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() in ("TRUE", "1", "YES", "Y", "✓", "✔")


def to_text(value, limit=300):
    if value is None:
        return ""
    return str(value).strip()[:limit]


def parse_detail(values):
    """The 明细 tab's values (header row first) -> list of row dicts. Blank rows are skipped."""
    if not values:
        raise SheetError("the 明细 tab is empty")
    columns = {}
    for index, header in enumerate(values[0]):
        field = HEADERS.get(_norm(header))
        if field and field not in columns:
            columns[field] = index
    missing = [HEADERS_BY_FIELD[f] for f in REQUIRED if f not in columns]
    if missing:
        raise SheetError(f"the 明细 tab has no column {', '.join(missing)} in its first row")

    rows = []
    for number, raw in enumerate(values[1:], start=2):
        def cell(field):
            index = columns.get(field)
            return raw[index] if index is not None and index < len(raw) else None

        sku = to_text(cell("sku"))
        if not sku:
            continue
        qty = to_number(cell("qty"))
        unit_cost = to_number(cell("unit_cost_cad"))
        subtotal = to_number(cell("subtotal_cad"))
        if subtotal is None and qty is not None and unit_cost is not None:
            subtotal = qty * unit_cost
        rows.append({
            "id": f"sheet-{number}",
            "source": "sheet",
            "sheet_row": number,
            "entry_date": to_date(cell("entry_date")),
            "sku": sku,
            "reason": to_text(cell("reason"), 50),
            "qty": qty,
            "unit_cost_cad": unit_cost,
            "subtotal_cad": subtotal,
            "request_by": to_text(cell("request_by"), 80),
            "sent_to": to_text(cell("sent_to"), 200),
            "process_date": to_date(cell("process_date")),
            "picked_up": to_bool(cell("picked_up")),
            "issued": to_bool(cell("issued")),
        })
    return rows


HEADERS_BY_FIELD = {field: header for header, field in HEADERS.items()}


def parse_reasons(values):
    reasons = []
    for row in values or []:
        text = to_text(row[0] if row else "", 50)
        if text and text not in reasons:
            reasons.append(text)
    return reasons or list(DEFAULT_REASONS)


def parse_costs(values):
    """Cost Ref (SKU, Cost USD, Cost CAD) -> {sku: cost CAD}. Like the sheet's XLOOKUP, the first
    row of a SKU wins."""
    costs = {}
    for row in (values or [])[1:]:
        if not row:
            continue
        sku = to_text(row[0])
        cost = to_number(row[2]) if len(row) > 2 else None
        if sku and cost is not None and sku not in costs:
            costs[sku] = cost
    return costs


class CostIndex:
    """SKU -> cost lookups and the search behind the SKU box of the entry form."""

    def __init__(self, costs):
        self.costs = costs
        self.lower = {}
        for sku, cost in costs.items():
            self.lower.setdefault(sku.lower(), (sku, cost))
        self.keys = sorted(self.lower)

    def __len__(self):
        return len(self.costs)

    def lookup(self, sku):
        """(the SKU as Cost Ref spells it, cost) or None. Case-insensitive, like XLOOKUP."""
        sku = (sku or "").strip()
        if sku in self.costs:
            return sku, self.costs[sku]
        return self.lower.get(sku.lower())

    def search(self, query, limit=20):
        words = [w for w in (query or "").lower().split() if w]
        if not words:
            return []
        hits = [k for k in self.keys if all(w in k for w in words)]
        first = words[0]
        hits.sort(key=lambda k: (not k.startswith(first), len(k), k))
        return [{"sku": self.lower[k][0], "unit_cost_cad": self.lower[k][1]} for k in hits[:limit]]


class SheetClient:
    """Reads the sheet as the runtime service account (Application Default Credentials)."""

    def __init__(self, spreadsheet_id, detail_tab="明细", reasons_tab="Note Ref", costs_tab="Cost Ref",
                 detail_ttl=60, costs_ttl=600, session=None):
        self.spreadsheet_id = spreadsheet_id
        self.detail_tab, self.reasons_tab, self.costs_tab = detail_tab, reasons_tab, costs_tab
        self.detail_ttl, self.costs_ttl = detail_ttl, costs_ttl
        self._session = session
        self._lock = threading.Lock()
        self._detail = None        # (fetched_at, rows, reasons)
        self._costs = None         # (fetched_at, CostIndex)
        self.title = ""

    @property
    def url(self):
        return f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/edit"

    def session(self):
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
            creds, _ = google.auth.default(scopes=SCOPES)
            self._session = AuthorizedSession(creds)
        return self._session

    def _batch_get(self, ranges):
        url = f"{SHEETS_API}/{self.spreadsheet_id}/values:batchGet"
        params = [("ranges", r) for r in ranges] + [("valueRenderOption", "UNFORMATTED_VALUE"),
                                                    ("dateTimeRenderOption", "SERIAL_NUMBER")]
        response = self.session().get(url, params=params, timeout=60)
        if response.status_code != 200:
            raise SheetError(f"Google Sheets answered {response.status_code}: {response.text[:200]}")
        return [vr.get("values", []) for vr in response.json().get("valueRanges", [])]

    @staticmethod
    def _range(tab, cols):
        return f"'{tab}'!{cols}"

    def fetch_title(self):
        url = f"{SHEETS_API}/{self.spreadsheet_id}"
        response = self.session().get(url, params={"fields": "properties.title"}, timeout=30)
        if response.status_code == 200:
            self.title = response.json().get("properties", {}).get("title", "")
        return self.title

    def detail(self, force=False):
        """(rows, reasons, fetched_at). Serves the last good read when a refresh fails."""
        with self._lock:
            cached = self._detail
            if cached and not force and time.time() - cached[0] < self.detail_ttl:
                return cached[1], cached[2], cached[0]
        try:
            detail, reasons = self._batch_get([self._range(self.detail_tab, "A1:Z"),
                                               self._range(self.reasons_tab, "A1:A")])
            rows, reasons = parse_detail(detail), parse_reasons(reasons)
        except Exception:
            if cached:
                return cached[1], cached[2], cached[0]
            raise
        if not self.title:
            try:
                self.fetch_title()
            except Exception:  # noqa: BLE001 - the title is cosmetic
                pass
        with self._lock:
            self._detail = (time.time(), rows, reasons)
        return rows, reasons, self._detail[0]

    def costs(self, force=False):
        with self._lock:
            cached = self._costs
            if cached and not force and time.time() - cached[0] < self.costs_ttl:
                return cached[1]
        try:
            (values,) = self._batch_get([self._range(self.costs_tab, "A1:C")])
            index = CostIndex(parse_costs(values))
        except Exception:
            if cached:
                return cached[1]
            raise
        with self._lock:
            self._costs = (time.time(), index)
        return index

    def row_url(self, sheet_gid, row):
        return f"{self.url}#gid={sheet_gid}&range=A{row}:L{row}"

