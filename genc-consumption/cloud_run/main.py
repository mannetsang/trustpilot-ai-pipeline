"""GenC Consumption: what GenC Beauty takes out of its own stock for office use, and what it cost.

Two sources, one dashboard:
  * the rows from before the dashboard: the Google Sheet "office use", tab 明细, read live as the
    runtime service account (cached for a minute) and never written;
  * the rows entered on the dashboard: the GenC Consumption Supabase project, table consumption
    (../schema.sql), reached with the service-role key from this server only.
Costs for new lines come from the sheet's Cost Ref tab (the same lookup the 明细 tab does) and are
stored with the line, so a later price change does not rewrite past months. The reasons are the
Note Ref tab.

Access: behind Identity-Aware Proxy. Google signs people in, the app verifies the
X-Goog-IAP-JWT-Assertion header against IAP_AUDIENCE and allows ALLOWED_DOMAINS. Anyone allowed
may add lines and tick 拿货 / 出库 on any line; a line's content (date, SKU, qty, reason, cost,
requested by, sent to) is changed or deleted only by whoever entered it or an ADMIN_EMAILS address.
AUTH_DISABLED=1 (local runs only) lets every caller in as an admin named "local".

Endpoints
  GET    /                    dashboard
  GET    /health[?deep=1]     liveness; deep=1 also checks the sheet and the database (counts only)
  GET    /api/config          auth mode and links (no data)
  GET    /api/me              who am I
  GET    /api/data            every row from both sources, the reasons, the sheet's link
  GET    /api/skus?q=         Cost Ref search for the entry form
  POST   /api/entries         {entry_date, reason, request_by, sent_to, process_date, picked_up, issued,
                               items: [{sku, qty, unit_cost_cad?}]}
  PATCH  /api/entries/<id>    any of the fields above for one line
  DELETE /api/entries/<id>    soft delete

Env: SPREADSHEET_ID, DETAIL_TAB, DETAIL_GID, STORE (supabase | memory), SUPABASE_URL,
SUPABASE_SERVICE_KEY, IAP_AUDIENCE, ALLOWED_DOMAINS, ADMIN_EMAILS, AUTH_DISABLED, TIMEZONE.
"""

import datetime as dt
import os
import sys
import threading
import time
from zoneinfo import ZoneInfo

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Local runs: read the repo's gitignored .env. The container has no lib/, so this is a no-op there.
try:
    sys.path.insert(0, os.path.join(HERE, "..", ".."))
    from lib.secrets import load_dotenv  # noqa: E402
    load_dotenv(HERE)
except Exception:  # noqa: BLE001
    pass

from sheet import SheetClient, SheetError  # noqa: E402
from store import MemoryStore, StoreError, SupabaseStore, now_iso  # noqa: E402

SERVICE = "genc-consumption"


def _csv(name, default=""):
    return [x.strip().lower() for x in os.environ.get(name, default).split(",") if x.strip()]


SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1fc2OkX2f9jfMlYxHSPuvyFP2p13JygwrguWxJQkeNdE")
DETAIL_TAB = os.environ.get("DETAIL_TAB", "明细")
DETAIL_GID = os.environ.get("DETAIL_GID", "0")
STORE_KIND = os.environ.get("STORE", "supabase").lower()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
IAP_AUDIENCE = os.environ.get("IAP_AUDIENCE", "")
IAP_HEADER = "X-Goog-IAP-JWT-Assertion"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
ALLOWED_DOMAINS = set(_csv("ALLOWED_DOMAINS", "superhairpieces.com"))
ADMIN_EMAILS = set(_csv("ADMIN_EMAILS", "manne@superhairpieces.com,anais@superhairpieces.com"))
AUTH_DISABLED = os.environ.get("AUTH_DISABLED") == "1"
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "America/Toronto"))
EARLIEST_DATE = dt.date(2020, 1, 1)
MAX_ITEMS = 50

if os.environ.get("K_SERVICE") and not AUTH_DISABLED and not IAP_AUDIENCE:
    # Without the audience no assertion can be checked, so nobody could get in anyway.
    raise SystemExit("IAP_AUDIENCE is required on Cloud Run (Identity-Aware Proxy)")
if AUTH_DISABLED:
    print("AUTH_DISABLED=1: every caller is let in as an admin; never deploy this way")

app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
app.json.ensure_ascii = False


# ---------------------------------------------------------------------------
# Clients (lazy, replaceable by tests)
# ---------------------------------------------------------------------------
_clients = {}
_client_lock = threading.Lock()


def get_store():
    with _client_lock:
        if "store" not in _clients:
            _clients["store"] = MemoryStore() if STORE_KIND == "memory" else SupabaseStore(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        return _clients["store"]


def get_sheet():
    with _client_lock:
        if "sheet" not in _clients:
            _clients["sheet"] = SheetClient(SPREADSHEET_ID, detail_tab=DETAIL_TAB)
        return _clients["sheet"]


def set_clients(store=None, sheet=None):
    with _client_lock:
        _clients.clear()
        if store is not None:
            _clients["store"] = store
        if sheet is not None:
            _clients["sheet"] = sheet


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------
class AuthError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


class BadRequest(Exception):
    pass


_token_lock = threading.Lock()
_token_cache = {}            # assertion -> (expires at, user)
_iap_keys = {"fetched": 0.0, "keys": {}}
IAP_KEYS_TTL = 3600
IAP_KEYS_COOLDOWN = 60


def fetch_iap_keys():
    """IAP's published public keys: a JSON map of key id to PEM. Tests patch this."""
    import requests as http
    resp = http.get(IAP_CERTS_URL, timeout=10)
    resp.raise_for_status()
    return resp.json()


def iap_keys(kid):
    now = time.time()
    with _token_lock:
        age = now - _iap_keys["fetched"]
        known = not kid or kid in _iap_keys["keys"]
        if (age < IAP_KEYS_TTL and known) or age < IAP_KEYS_COOLDOWN:
            return _iap_keys["keys"]
    keys = fetch_iap_keys()
    with _token_lock:
        _iap_keys.update({"fetched": time.time(), "keys": keys})
    return keys


def verify_iap_jwt(token):
    """The signed header IAP adds to every request: ES256, IAP's published keys, our audience."""
    from google.auth import jwt as google_jwt
    kid = google_jwt.decode_header(token).get("kid")
    info = google_jwt.decode(token, certs=iap_keys(kid), audience=IAP_AUDIENCE, clock_skew_in_seconds=30)
    if info.get("iss") != IAP_ISSUER:
        raise ValueError(f"unexpected issuer {info.get('iss')!r}")
    return info


def display_name(email):
    """anais@superhairpieces.com -> Anais, wen.yu@... -> Wen Yu: what the sheet's Request By holds."""
    local = email.split("@", 1)[0]
    return " ".join(part.capitalize() for part in local.replace("_", ".").split(".") if part)


def make_user(email):
    return {"email": email, "name": display_name(email), "admin": email in ADMIN_EMAILS}


def current_user():
    if AUTH_DISABLED:
        return {"email": "local", "name": "Local", "admin": True}
    token = request.headers.get(IAP_HEADER, "").strip()
    if not token or not IAP_AUDIENCE:
        return None
    now = time.time()
    with _token_lock:
        cached = _token_cache.get(token)
        if cached and cached[0] > now:
            return cached[1]
        if len(_token_cache) > 500:
            for key in [k for k, v in _token_cache.items() if v[0] <= now]:
                _token_cache.pop(key, None)
    try:
        info = verify_iap_jwt(token)
    except Exception as exc:  # noqa: BLE001
        print(f"auth: IAP assertion rejected: {exc}")
        return None
    email = (info.get("email") or "").lower()
    if not email or (email not in ADMIN_EMAILS and email.rsplit("@", 1)[-1] not in ALLOWED_DOMAINS):
        # IAP's own access list is the first gate; this keeps an accidental wider grant harmless.
        print(f"auth: {email or '(no email)'} is outside the allowed domains")
        return None
    user = make_user(email)
    with _token_lock:
        _token_cache[token] = (float(info.get("exp") or now + 300), user)
    return user


def require_user():
    user = current_user()
    if not user:
        raise AuthError(401, "sign in with your superhairpieces.com Google account")
    # Identity rides on IAP's browser cookie, so a form on another site could post here with it;
    # the page sends this header on every call and a cross-site form cannot.
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-Requested-With") != "XMLHttpRequest":
        raise AuthError(403, "changes must come from the dashboard page (X-Requested-With header)")
    g.user = user
    return user


def may_edit_content(user, row):
    return user["admin"] or (row.get("created_by") or "").lower() == user["email"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def today():
    return dt.datetime.now(TIMEZONE).date()


def parse_date(value, field, required=True):
    if value in (None, ""):
        if required:
            raise BadRequest(f"{field} is required")
        return None
    try:
        day = dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        raise BadRequest(f"{field} must be a date (YYYY-MM-DD)") from None
    if not EARLIEST_DATE <= day <= today() + dt.timedelta(days=31):
        raise BadRequest(f"{field} {day.isoformat()} is out of range")
    return day.isoformat()


def parse_text(value, field, limit, required=False):
    text = "".join(ch for ch in str(value or "") if ch.isprintable()).strip()
    if required and not text:
        raise BadRequest(f"{field} is required")
    if len(text) > limit:
        raise BadRequest(f"{field} is longer than {limit} characters")
    return text


def parse_number(value, field, required=True, positive=False, maximum=1_000_000):
    if value in (None, ""):
        if required:
            raise BadRequest(f"{field} is required")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{field} must be a number") from None
    if number != number or number < 0 or (positive and number <= 0) or number > maximum:
        raise BadRequest(f"{field} must be {'above' if positive else 'at least'} 0 and at most {maximum:,}")
    return round(number, 4)


def parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def reasons():
    try:
        _, found, _ = get_sheet().detail()
        return found
    except Exception:  # noqa: BLE001 - the defaults keep entry possible while the sheet is down
        from sheet import DEFAULT_REASONS
        return list(DEFAULT_REASONS)


def parse_reason(value):
    reason = parse_text(value, "reason", 50, required=True)
    allowed = reasons()
    if reason not in allowed:
        raise BadRequest(f"reason must be one of {', '.join(allowed)} (the Note Ref tab)")
    return reason


def cost_index():
    try:
        return get_sheet().costs()
    except Exception as exc:  # noqa: BLE001
        raise SheetError(f"the Cost Ref tab could not be read, so costs cannot be looked up: {exc}") from exc


def priced(sku, manual_cost, costs):
    """(sku, unit cost, cost source): Cost Ref first (and its spelling of the SKU), else a typed cost."""
    found = costs.lookup(sku)
    if found:
        return found[0], found[1], "cost_ref"
    if manual_cost is not None:
        return sku, manual_cost, "manual"
    return sku, None, "none"


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
def app_row(row, user):
    out = dict(row)
    out["source"] = "app"
    out["can_edit"] = may_edit_content(user, row)
    return out


def sheet_rows():
    sheet = get_sheet()
    rows, found_reasons, fetched = sheet.detail()
    for row in rows:
        row["sheet_url"] = sheet.row_url(DETAIL_GID, row["sheet_row"])
    return rows, found_reasons, fetched


@app.errorhandler(AuthError)
def _auth_error(exc):
    return jsonify({"error": exc.message}), exc.status


@app.errorhandler(BadRequest)
def _bad_request(exc):
    return jsonify({"error": str(exc)}), 400


@app.errorhandler(StoreError)
def _store_error(exc):
    print(f"store: {exc}")
    return jsonify({"error": f"database: {exc}"}), 502


@app.errorhandler(SheetError)
def _sheet_error(exc):
    print(f"sheet: {exc}")
    return jsonify({"error": str(exc)}), 503


@app.errorhandler(Exception)
def _unexpected(exc):
    if isinstance(exc, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": exc.description}), exc.code
        return exc
    print(f"unexpected error on {request.path}: {exc!r}")
    return jsonify({"error": "unexpected error; see the service log"}), 500


@app.after_request
def _headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.path in ("/", "/index.html") or request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/health")
def health():
    body = {"service": SERVICE, "ok": True, "store": STORE_KIND}
    if request.args.get("deep") == "1":
        checks = {"auth": {"mode": "open" if AUTH_DISABLED else "iap", "audience": IAP_AUDIENCE}}
        try:
            rows, found_reasons, _ = get_sheet().detail()
            checks["sheet"] = {"ok": True, "rows": len(rows), "reasons": len(found_reasons)}
        except Exception as exc:  # noqa: BLE001
            checks["sheet"] = {"ok": False, "error": str(exc)[:160]}
        try:
            checks["cost_ref"] = {"ok": True, "skus": len(get_sheet().costs())}
        except Exception as exc:  # noqa: BLE001
            checks["cost_ref"] = {"ok": False, "error": str(exc)[:160]}
        try:
            get_store().ping()
            checks["store"] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            checks["store"] = {"ok": False, "error": str(exc)[:160]}
        body["checks"] = checks
        body["ok"] = all(c.get("ok", True) for c in checks.values())
    return jsonify(body), 200 if body["ok"] else 503


@app.route("/api/config")
def api_config():
    return jsonify({"auth": "open" if AUTH_DISABLED else "iap", "sheet_url": get_sheet().url, "detail_tab": DETAIL_TAB})


@app.route("/api/me")
def api_me():
    return jsonify(require_user())


@app.route("/api/data")
def api_data():
    user = require_user()
    warnings = []
    sheet = get_sheet()
    try:
        old_rows, found_reasons, fetched = sheet_rows()
        sheet_info = {"ok": True, "fetched_at": dt.datetime.fromtimestamp(fetched, dt.timezone.utc).isoformat(timespec="seconds")}
    except Exception as exc:  # noqa: BLE001 - show the new rows even when the sheet is unreachable
        print(f"sheet: {exc}")
        old_rows, found_reasons = [], reasons()
        sheet_info = {"ok": False}
        warnings.append(f"The Google Sheet could not be read, so rows from before the dashboard are missing: {exc}")
    try:
        new_rows = [app_row(r, user) for r in get_store().list_entries()]
        store_info = {"ok": True}
    except StoreError as exc:
        print(f"store: {exc}")
        new_rows, store_info = [], {"ok": False}
        warnings.append(f"The database could not be read, so rows entered on the dashboard are missing: {exc}")
    undated = sum(1 for r in old_rows if not r["entry_date"])
    if undated:
        warnings.append(f"{undated} row(s) in the {DETAIL_TAB} tab have no readable date and are left out of the charts.")
    return jsonify({
        "rows": old_rows + new_rows,
        "reasons": found_reasons,
        "today": today().isoformat(),
        "user": user,
        "sheet": {**sheet_info, "url": sheet.url, "title": sheet.title, "tab": DETAIL_TAB},
        "store": store_info,
        "warnings": warnings,
    })


@app.route("/api/skus")
def api_skus():
    require_user()
    query = parse_text(request.args.get("q"), "q", 100)
    return jsonify({"skus": cost_index().search(query) if len(query) >= 2 else []})


@app.route("/api/entries", methods=["POST"])
def api_create():
    user = require_user()
    body = request.get_json(silent=True) or {}
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise BadRequest("add at least one item")
    if len(items) > MAX_ITEMS:
        raise BadRequest(f"at most {MAX_ITEMS} items at a time")
    common = {
        "entry_date": parse_date(body.get("entry_date"), "date"),
        "reason": parse_reason(body.get("reason")),
        "request_by": parse_text(body.get("request_by"), "requested by", 80, required=True),
        "sent_to": parse_text(body.get("sent_to"), "sent to", 200),
        "process_date": parse_date(body.get("process_date"), "process date", required=False),
        "picked_up": parse_bool(body.get("picked_up", False)),
        "issued": parse_bool(body.get("issued", False)),
    }
    if common["issued"] and not common["process_date"]:
        common["process_date"] = today().isoformat()
    costs = cost_index()
    rows = []
    for number, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise BadRequest(f"item {number} is not an object")
        sku = parse_text(item.get("sku"), f"item {number} SKU", 300, required=True)
        qty = parse_number(item.get("qty"), f"item {number} qty", positive=True, maximum=100_000)
        manual = parse_number(item.get("unit_cost_cad"), f"item {number} unit cost", required=False, maximum=100_000)
        sku, unit_cost, source = priced(sku, manual, costs)
        rows.append({**common, "sku": sku, "qty": qty, "unit_cost_cad": unit_cost, "cost_source": source,
                     "created_by": user["email"]})
    created = get_store().insert_entries(rows)
    return jsonify({"created": [app_row(r, user) for r in created]}), 201


CONTENT_FIELDS = ("entry_date", "sku", "reason", "qty", "unit_cost_cad", "request_by", "sent_to")
STATUS_FIELDS = ("picked_up", "issued", "process_date")


@app.route("/api/entries/<entry_id>", methods=["PATCH"])
def api_update(entry_id):
    user = require_user()
    store = get_store()
    row = store.get_entry(entry_id)
    if not row:
        return jsonify({"error": "no such line (deleted, or from the Google Sheet)"}), 404
    body = request.get_json(silent=True) or {}
    unknown = set(body) - set(CONTENT_FIELDS) - set(STATUS_FIELDS)
    if unknown:
        raise BadRequest(f"unknown field(s): {', '.join(sorted(unknown))}")
    if set(body) & set(CONTENT_FIELDS) and not may_edit_content(user, row):
        raise AuthError(403, f"only {row.get('created_by')} or an admin can change this line; "
                             "anyone can tick 拿货 / 出库")
    patch = {}
    if "entry_date" in body:
        patch["entry_date"] = parse_date(body["entry_date"], "date")
    if "reason" in body:
        patch["reason"] = parse_reason(body["reason"])
    if "qty" in body:
        patch["qty"] = parse_number(body["qty"], "qty", positive=True, maximum=100_000)
    if "request_by" in body:
        patch["request_by"] = parse_text(body["request_by"], "requested by", 80, required=True)
    if "sent_to" in body:
        patch["sent_to"] = parse_text(body["sent_to"], "sent to", 200)
    # The cost is looked up again only when the SKU or a typed cost actually changes: re-saving a
    # line must not re-price it from today's Cost Ref.
    sku_changed = "sku" in body and parse_text(body["sku"], "SKU", 300, required=True) != row["sku"]
    if sku_changed or "unit_cost_cad" in body:
        sku = parse_text(body.get("sku", row["sku"]), "SKU", 300, required=True)
        manual = parse_number(body["unit_cost_cad"], "unit cost", required=False, maximum=100_000) \
            if "unit_cost_cad" in body else (row["unit_cost_cad"] if row["cost_source"] == "manual" else None)
        sku, unit_cost, source = priced(sku, manual, cost_index())
        if "unit_cost_cad" in body and source == "cost_ref" and manual is not None and manual != unit_cost:
            raise BadRequest(f"{sku} is priced by the Cost Ref tab ({unit_cost:.2f}); change it there")
        patch.update({"sku": sku, "unit_cost_cad": unit_cost, "cost_source": source})
    for field in ("picked_up", "issued"):
        if field in body:
            patch[field] = parse_bool(body[field])
    if "process_date" in body:
        patch["process_date"] = parse_date(body["process_date"], "process date", required=False)
    elif patch.get("issued") and not row.get("process_date"):
        patch["process_date"] = today().isoformat()
    if not patch:
        raise BadRequest("nothing to change")
    patch.update({"updated_by": user["email"], "updated_at": now_iso()})
    updated = store.update_entry(entry_id, patch)
    if not updated:
        return jsonify({"error": "no such line"}), 404
    return jsonify({"entry": app_row(updated, user)})


@app.route("/api/entries/<entry_id>", methods=["DELETE"])
def api_delete(entry_id):
    user = require_user()
    store = get_store()
    row = store.get_entry(entry_id)
    if not row:
        return jsonify({"error": "no such line (deleted, or from the Google Sheet)"}), 404
    if not may_edit_content(user, row):
        raise AuthError(403, f"only {row.get('created_by')} or an admin can delete this line")
    store.delete_entry(entry_id, user["email"])
    return jsonify({"deleted": entry_id})


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else int(os.environ.get("PORT", "8080"))
    app.run(host="127.0.0.1", port=port, debug=False)
