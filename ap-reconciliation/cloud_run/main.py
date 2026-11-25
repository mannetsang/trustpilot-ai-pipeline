"""AP credit-card reconciliation: statement upload, invoice matching, Chat reminders, dashboard.

Accounts payable uploads a credit-card statement or card-transaction export
(.xlsx/.csv parsed directly, .pdf/images read by Gemini). The service stores
each line once (deterministic ids), walks the AP mailbox's Drive folder
Invoice/YYYY MM/<vendor>/ to read every new invoice with Gemini, pairs
purchases with invoices (rules first, Gemini judge for the rest), posts one
"invoices needed" card to each credit card's Google Chat space, and serves the
dashboard where card owners add the use case (and cost centre) of their charges,
link or waive invoices, month by month. On cards in RECEIPT_UPLOAD_CARDS the
holder can also photograph or upload a receipt from the dashboard: it is saved
to Invoice/YYYY MM/<vendor>/ and linked to the purchase.

Storage is a dedicated Supabase project (schema in ../schema.sql), reached
with the service-role key from this server only. STORE=memory runs everything
in-process for tests and demos.

Access. The live deployment (decided 2026-10-13) sits behind Identity-Aware Proxy: Google signs
people in, the app verifies the X-Goog-IAP-JWT-Assertion header against IAP_AUDIENCE, allows the
ALLOWED_DOMAINS and makes AP_EMAILS "ap" and everyone else "member" (see README). With
AUTH_DISABLED=1 instead, every caller is "ap" and the name typed into the page (X-Dashboard-User
header) is what gets recorded: with DASHBOARD_PASSWORD also set the page first asks for that shared
password and keeps a signed cookie (POST /api/login), without it the dashboard is open to anyone
who can reach it.

Endpoints
  GET  /                         dashboard
  GET  /health[?deep=1]          liveness; deep=1 also checks Supabase and the Invoice folder
  GET  /api/config               auth mode, dashboard and sign-in page URLs (public, no secrets)
  POST /api/login  POST /api/logout  password mode: {"password"} sets the session cookie / clears it
  GET  /api/me                   who am I, role (ap | member), cards I own
  GET  /api/months               year/month navigation with per-card counts, the cost centres and use cases
  GET  /api/transactions?year&month[&card]   the month's rows (with a use-case suggestion each when switched on)
  GET  /api/transactions/<id>    one row (the page polls it while an uploaded receipt is being read)
  PATCH /api/transactions/<id>   cost_center, usage (the use case), note, person_id ("" clears it),
                                 action=waive|unwaive|confirm|link|unlink; setting the use case fills an
                                 empty cost centre from the card's default (filled_cost_center)
  GET  /api/people               the people a transaction can be assigned to
  POST /api/people               {name[, email]}: add one (201), or the one already named so (200); 409 for
                                 an e-mail someone else has
  POST /api/transactions/<id>/rematch  read new invoices, then look for this purchase's invoice again (202, a job)
  GET  /api/transactions/<id>/receipt-target  where Add receipt… would save: month and vendor folder, folders
  POST /api/transactions/<id>/receipt   multipart file (1-10 pages), vendor_folder, upload_id: save the
                                 receipt in Drive and link it (201; 200 for a retried upload_id)
  DELETE /api/transactions/<id>/receipt  Remove…: unlink a receipt added from the dashboard, trash its file
  GET  /receipts/<last4>/<YYYY>/<MM>  the Chat card's Add receipts: redirects to that card's receipts list
  POST /api/ask?year&month[&card]  voice mode: {"question"} -> a short answer to read aloud (read only)
  GET  /api/invoices?year&month[&all=1]  invoices of the surrounding month folders (link picker)
  GET  /api/cards  POST /api/cards (AP)     card labels, owners, Chat space names, default cost centre,
                                 receipt upload, bank, network and product; POST answers 404 for an unknown
                                 card unless create: true
  GET  /api/export/missing?group&card&year&month  the purchases still missing a receipt, as .xlsx
  GET  /api/statements?year&month           uploads
  POST /api/statements (AP, multipart file; ?sync=1 also indexes, matches and notifies)
  DELETE /api/statements/<id> (AP)          remove an upload and its untouched lines
  GET  /api/bank-imports (AP)               the Drive import: last check of the Bank Transactions folder, files read
  POST /api/index?year&month&limit (AP)     read new invoices for month-1..month+1, one batch
  POST /api/match?year&month[&card] (AP)    pair purchases with invoices
  POST /api/notify?year&month[&card] (AP)   post the "invoices needed" Chat cards
  POST /api/jobs?kind=index|match|notify&year&month[&card] (AP)  the same steps in the background (202)
  GET  /api/jobs/current                    the running or last background job: progress, then result
  GET  /admin                                the accounts-payable admin pages (every /api/admin call is AP only;
                                             its writes are refused in open mode on a real database)
  GET  /static/banks/<file>.svg              the bank logos the Cards gallery draws (BANKS), cacheable
  GET  /api/admin/settings  PUT /api/admin/settings/coding {suggestions}
  GET  /api/admin/cards  DELETE /api/admin/cards/<last4>[?confirm=1]  POST /api/admin/cards/<last4>/fill-default
  GET  /api/admin/card-numbers/<n>  POST|DELETE /api/admin/card-numbers/<n>/ignore
  GET  /api/admin/coding  POST /api/admin/cost-centers  PATCH /api/admin/cost-centers?name=
  POST /api/admin/cost-centers/order  POST /api/admin/use-cases  PATCH /api/admin/use-cases/<id>
  POST /api/admin/use-cases/order
  GET  /api/admin/vendors?q&sort&registered&page&per[&fresh=1]  GET /api/admin/vendors/folder?name=
  GET  /api/admin/registry  POST /api/admin/vendors  PATCH /api/admin/vendors/<id>
  POST /api/admin/vendors/<id>/folders  DELETE /api/admin/vendor-folders/<id>
  POST /api/admin/vendors/<id>/patterns  PATCH|DELETE /api/admin/vendor-patterns/<id>
  PUT  /api/admin/vendors/<id>/coding  GET /api/admin/proposals  POST /api/admin/proposals/dismiss
  GET  /api/admin/people  PATCH|DELETE /api/admin/people/<id>  POST /api/admin/people/<id>/merge
  POST /api/admin/people/link-holders  GET /api/admin/audit?entity&entity_id&actor&before_id&limit
The weekly reminder is a Cloud Run job running `python main.py remind`, and the Drive import one
running `python main.py sync` every 15 minutes (see the deploy workflow).

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, INVOICE_FOLDER_ID, STATEMENTS_FOLDER_ID (optional),
BANK_FOLDER_ID and BANK_SETTLE_MINUTES (the Drive import; off when unset), INVOICE_WATCH (0: the import
run does not read and match newly filed invoices),
GOOGLE_WORKSPACE_AP_CLIENT_ID/_CLIENT_SECRET/_REFRESH_TOKEN (Drive as ap@; ADC when unset),
CARD_WEBHOOKS_JSON, GCHAT_WEBHOOK_URL, IAP_AUDIENCE, ALLOWED_DOMAINS,
AP_EMAILS, DASHBOARD_URL, LOGIN_URL, GEMINI_EXTRACT_MODEL, GEMINI_MATCH_MODEL, AI_DISABLED, AUTH_DISABLED,
DASHBOARD_PASSWORD, DASHBOARD_SESSION_DAYS, RECEIPT_UPLOAD_CARDS (default 4421; "all" for every card), STORE.
"""

import calendar
import concurrent.futures
import datetime as dt
import hashlib
import hmac
import html
import io
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict

from flask import Flask, abort, g, jsonify, redirect, request, send_file, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Local runs: read the repo's gitignored .env. The container has no lib/, so this is a no-op there.
try:
    sys.path.insert(0, os.path.join(HERE, "..", ".."))
    from lib.secrets import load_dotenv  # noqa: E402
    load_dotenv(HERE)
except Exception:  # noqa: BLE001
    pass

import admin  # noqa: E402
import assistant  # noqa: E402
import chat  # noqa: E402
import invoices as invoice_index  # noqa: E402
import matching  # noqa: E402
import receipts  # noqa: E402
import statements  # noqa: E402
from drive import FOLDER, Drive, DriveError  # noqa: E402
from gemini import Gemini, GeminiError  # noqa: E402
from store import (CARD_FIELDS, CARD_NETWORKS, CARD_PRODUCT_MAX, UUID_RE, VENDOR_FIELDS, MemoryStore,  # noqa: E402
                   StoreError, SupabaseStore, month_folder_name, neighbouring_months, now_iso, person_named)

app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _csv(name, default=""):
    return [x.strip().lower() for x in os.environ.get(name, default).split(",") if x.strip()]


STORE_KIND = os.environ.get("STORE", "supabase").lower()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
INVOICE_FOLDER_ID = os.environ.get("INVOICE_FOLDER_ID", "18YkGTNDIrxdQgHRgquvQizI8fLsFOpPk")
STATEMENTS_FOLDER_ID = os.environ.get("STATEMENTS_FOLDER_ID", "")
# The Drive folder whose statements are imported on their own (`python main.py sync`, every 15
# minutes): the "Bank Transactions" folder, shared with ap@ as a viewer. Unset: no import.
BANK_FOLDER_ID = os.environ.get("BANK_FOLDER_ID", "")
BANK_SETTLE = int(os.environ.get("BANK_SETTLE_MINUTES", "10")) * 60   # a file edited this recently waits
# The same run reads and matches invoices filed under the Invoice folder since the last run (by the
# e-mail filer, by hand, or a receipt uploaded here). INVOICE_WATCH=0 turns that off.
INVOICE_WATCH_ON = os.environ.get("INVOICE_WATCH", "1") != "0"
# Stripped: a secret version saved from a text file often ends in a line break, which Google's token
# endpoint does not forgive.
AP_CLIENT_ID = os.environ.get("GOOGLE_WORKSPACE_AP_CLIENT_ID", "").strip()
AP_CLIENT_SECRET = os.environ.get("GOOGLE_WORKSPACE_AP_CLIENT_SECRET", "").strip()
AP_REFRESH_TOKEN = os.environ.get("GOOGLE_WORKSPACE_AP_REFRESH_TOKEN", "").strip()
CARD_WEBHOOKS = chat.load_webhooks(os.environ.get("CARD_WEBHOOKS_JSON", ""), os.environ.get("GCHAT_WEBHOOK_URL", ""))
# Identity-Aware Proxy signs every request it lets through; the JWT's audience names this
# service: /projects/<project number>/locations/<region>/services/<service>.
IAP_AUDIENCE = os.environ.get("IAP_AUDIENCE", "")
IAP_HEADER = "X-Goog-IAP-JWT-Assertion"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
DASHBOARD_USER_HEADER = "X-Dashboard-User"     # open mode: the name typed into the page
ALLOWED_DOMAINS = set(_csv("ALLOWED_DOMAINS", "superhairpieces.com"))
AP_EMAILS = set(_csv("AP_EMAILS", "ap@superhairpieces.com,manne@superhairpieces.com,snow@superhairpieces.com"))
AUTH_DISABLED = os.environ.get("AUTH_DISABLED") == "1"
# Shared password (Secret Manager ap-reconciliation-dashboard-password). With AUTH_DISABLED=1 and
# this set, the page asks for it once and keeps a signed cookie; whoever knows it is AP. Stripped
# because a secret version made with `echo` carries the line break.
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "").strip()
PASSWORD_MODE = AUTH_DISABLED and bool(DASHBOARD_PASSWORD)
SESSION_COOKIE = "ap_recon_session"


def _session_days():
    raw = os.environ.get("DASHBOARD_SESSION_DAYS", "30")
    try:
        days = int(raw)
    except ValueError:
        raise SystemExit(f"DASHBOARD_SESSION_DAYS must be a whole number of days, not {raw!r}") from None
    if days < 1:
        # 0 would sign the cookie and reject it on the next call: the page would loop on the gate.
        raise SystemExit("DASHBOARD_SESSION_DAYS must be at least 1")
    return days


SESSION_DAYS = _session_days()
LOGIN_WINDOW = 15 * 60          # wrong passwords are counted over this many seconds...
LOGIN_MAX_FAILURES = 10         # ...per address, and
LOGIN_MAX_FAILURES_TOTAL = 50   # ...across every address (one instance, so in-process counters hold)
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")
LOGIN_URL = os.environ.get("LOGIN_URL", "").rstrip("/")      # the public sign-in page (IAP mode)
GCP_PROJECT = os.environ.get("GCP_PROJECT", "shp-ai-bot-2026")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
GEMINI_EXTRACT_MODEL = os.environ.get("GEMINI_EXTRACT_MODEL", "gemini-2.5-flash")
GEMINI_MATCH_MODEL = os.environ.get("GEMINI_MATCH_MODEL", "gemini-2.5-pro")
AI_DISABLED = os.environ.get("AI_DISABLED") == "1"
INDEX_BATCH = int(os.environ.get("INDEX_BATCH", "30"))
INDEX_WORKERS = int(os.environ.get("INDEX_WORKERS", "4"))
MAX_INDEX_ROUNDS = 100
SERVICE = "ap-reconciliation"
# Cards whose holders add receipts from the dashboard (Add receipt…, the receipts list, the Chat
# card's Add receipts button). A code default, so the service and the weekly job agree without a
# deploy change; "all" (or "*") switches it on for every card.
RECEIPT_UPLOAD_CARDS = set(_csv("RECEIPT_UPLOAD_CARDS", "4421"))
RECEIPT_WINDOW = 600            # receipt saves are counted over this many seconds...
RECEIPT_MAX = 40                # ...per person
RECEIPT_MAX_BODY = receipts.MAX_PDF_BYTES + 1024 * 1024     # a receipt's request: the PDF limit plus form overhead


def receipt_upload_env(last4):
    """What RECEIPT_UPLOAD_CARDS says for the card, whatever the card's own setting."""
    return bool(RECEIPT_UPLOAD_CARDS & {"all", "*"}) or str(last4) in RECEIPT_UPLOAD_CARDS


def receipt_upload_enabled(card):
    """card: a cards row or a last4. The row's own receipt_upload (true/false) wins, even over
    RECEIPT_UPLOAD_CARDS=all; null, or no row, follows RECEIPT_UPLOAD_CARDS."""
    if isinstance(card, dict):
        row, last4 = card, str(card.get("last4") or "")
    else:
        last4 = str(card)
        try:
            row = next((c for c in get_store().list_cards() if c["last4"] == last4), None)
        except Exception:  # noqa: BLE001  (store down: the env decides)
            row = None
    own = (row or {}).get("receipt_upload")
    return bool(own) if own is not None else receipt_upload_env(last4)


def auth_mode():
    """open: anyone who reaches the URL; password: the shared password first; iap: Google sign-in."""
    return ("password" if PASSWORD_MODE else "open") if AUTH_DISABLED else "iap"


def access_banner():
    """The start-up line that says who gets in, or SystemExit for a configuration that must not run."""
    if PASSWORD_MODE:
        # Password mode: the page asks for the shared password and everyone who knows it is AP.
        return ("AUTH_DISABLED=1 with DASHBOARD_PASSWORD: the dashboard asks for the shared password; "
                "every caller who knows it is AP")
    if AUTH_DISABLED and "DASHBOARD_PASSWORD" in os.environ:
        # The secret is mounted but its version is blank (an `echo.`, an empty file): refusing to
        # start beats quietly opening the dashboard to everyone.
        raise SystemExit("DASHBOARD_PASSWORD is set but blank: add a non-empty secret version, "
                         "or remove it from the deploy to run open on purpose")
    if AUTH_DISABLED:
        # Open mode (the live setting from 2026-10-12 to 2026-10-13): anyone who can reach the URL
        # sees every card and may change anything, as AP.
        return ("AUTH_DISABLED=1 without DASHBOARD_PASSWORD: the dashboard is open to anyone who can "
                "reach it; every caller is AP")
    if os.environ.get("K_SERVICE") and not IAP_AUDIENCE:
        # Without the audience no assertion can be checked, so nobody could get in anyway.
        raise SystemExit("IAP_AUDIENCE is required on Cloud Run when AUTH_DISABLED is not set (Identity-Aware Proxy)")
    return ""


_banner = access_banner()
if _banner:
    print(_banner)

STATUS_LABELS = {"matched": "Invoice found", "possible": "Possible match", "missing": "Invoice missing",
                 "waived": "No invoice (waived)", "not_required": "No invoice needed"}
EDITABLE_TEXT = {"cost_center": 100, "usage": 500, "note": 1000}


# ---------------------------------------------------------------------------
# Lazy clients
# ---------------------------------------------------------------------------
_clients = {}
_client_lock = threading.Lock()


def get_store():
    with _client_lock:
        if "store" not in _clients:
            _clients["store"] = MemoryStore() if STORE_KIND == "memory" else SupabaseStore(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        return _clients["store"]


def get_drive():
    with _client_lock:
        if "drive" not in _clients:
            _clients["drive"] = Drive(AP_CLIENT_ID, AP_CLIENT_SECRET, AP_REFRESH_TOKEN)
        return _clients["drive"]


def get_gemini():
    if AI_DISABLED:
        return None
    with _client_lock:
        if "gemini" not in _clients:
            _clients["gemini"] = Gemini(GCP_PROJECT, VERTEX_LOCATION, GEMINI_EXTRACT_MODEL, GEMINI_MATCH_MODEL)
        return _clients["gemini"]


def set_clients(store=None, drive=None, gemini=None):
    """Tests inject fakes here."""
    with _client_lock:
        if store is not None:
            _clients["store"] = store
            admin.invalidate()          # the admin pages' cached reads belong to the store they came from
        if drive is not None:
            _clients["drive"] = drive
        if gemini is not None:
            _clients["gemini"] = gemini


# ---------------------------------------------------------------------------
# Auth: the signed header Identity-Aware Proxy adds to every request; roles from env + cards table
# ---------------------------------------------------------------------------
_token_cache = {}
_token_lock = threading.Lock()


class AuthError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


_iap_keys = {"fetched": 0.0, "keys": {}}
IAP_KEYS_TTL = 3600          # IAP rotates keys rarely; an unknown key id refreshes early
IAP_KEYS_COOLDOWN = 60       # but garbage key ids cannot make us hammer gstatic


def fetch_iap_keys():
    """IAP's published public keys: a JSON map of key id to PEM. Tests patch this."""
    import requests as http
    resp = http.get(IAP_CERTS_URL, timeout=10)
    resp.raise_for_status()
    return resp.json()


def iap_keys(kid):
    """The key map, cached in-process so verifying an assertion normally costs no network call."""
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
    """Check the signed header IAP adds to every request: ES256, IAP's published keys, our audience."""
    from google.auth import jwt as google_jwt
    kid = google_jwt.decode_header(token).get("kid")
    info = google_jwt.decode(token, certs=iap_keys(kid), audience=IAP_AUDIENCE, clock_skew_in_seconds=30)
    if info.get("iss") != IAP_ISSUER:
        raise ValueError(f"unexpected issuer {info.get('iss')!r}")
    return info


def declared_user():
    """Open mode: the name the page says is typing (remembered in the browser), else "anonymous"."""
    try:
        raw = request.headers.get(DASHBOARD_USER_HEADER, "")
    except RuntimeError:            # no request: the CLI or the weekly job
        raw = ""
    name = "".join(ch for ch in raw if ch.isprintable()).strip()[:80]
    return name or "anonymous"


# Password mode. The cookie holds "<issued>.<hmac>", signed with a key derived from the password
# and the Supabase service key: a stolen cookie gives away neither, and changing either secret
# signs everyone out. Nothing is stored server side, so sessions survive instance restarts.
def _session_key():
    return hashlib.sha256(f"{DASHBOARD_PASSWORD}\x00{SUPABASE_SERVICE_KEY}".encode()).digest()


def session_token(issued=None):
    issued = int(time.time() if issued is None else issued)
    sig = hmac.new(_session_key(), str(issued).encode(), "sha256").hexdigest()
    return f"{issued}.{sig}"


def session_ok(token):
    issued, _, sig = (token or "").partition(".")
    # Shape first: str.isdigit accepts digits int() rejects, and compare_digest refuses non-ASCII,
    # so a crafted cookie must answer 401, never raise.
    if not (issued.isascii() and issued.isdigit() and len(issued) <= 12
            and len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)):
        return False
    age = time.time() - int(issued)
    if age > SESSION_DAYS * 86400 or age < -300:
        return False
    expected = hmac.new(_session_key(), issued.encode(), "sha256").hexdigest()
    return hmac.compare_digest(sig, expected)


def password_matches(given):
    return bool(DASHBOARD_PASSWORD) and hmac.compare_digest((given or "").strip().encode(), DASHBOARD_PASSWORD.encode())


_login_failures = {}        # client address -> [time of each wrong password in the window]
_login_lock = threading.Lock()


def client_ip():
    """The address Cloud Run's front end appends to X-Forwarded-For (the last entry: anything
    before it came from the client and could be made up), else the socket's."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.rsplit(",", 1)[-1].strip() or request.remote_addr or "?")[:64]


def login_throttled(ip):
    """A short shared password has few guesses; counting wrong ones per address and overall keeps a
    script from walking through them (the counters live in this process; one instance runs)."""
    now = time.time()
    with _login_lock:
        for addr in [a for a, times in _login_failures.items() if not any(t > now - LOGIN_WINDOW for t in times)]:
            _login_failures.pop(addr, None)
        mine = [t for t in _login_failures.get(ip, []) if t > now - LOGIN_WINDOW]
        total = sum(1 for times in _login_failures.values() for t in times if t > now - LOGIN_WINDOW)
        return len(mine) >= LOGIN_MAX_FAILURES or total >= LOGIN_MAX_FAILURES_TOTAL


def note_login(ip, ok):
    with _login_lock:
        if ok:
            _login_failures.pop(ip, None)
        else:
            _login_failures.setdefault(ip, []).append(time.time())


def current_user():
    """Open mode: whoever says they are typing, as AP, after the shared password when one is set.
    Behind IAP: who its assertion header names, or None when the request did not come through IAP."""
    if AUTH_DISABLED:
        if PASSWORD_MODE and not session_ok(request.cookies.get(SESSION_COOKIE)):
            return None
        who = declared_user()
        return {"email": who, "name": who, "role": "ap", "picture": ""}
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
    if not email:
        return None
    domain = email.rsplit("@", 1)[-1]
    if email not in AP_EMAILS and domain not in ALLOWED_DOMAINS:
        # IAP's own access list is the first gate; this keeps an accidental wider grant harmless.
        print(f"auth: {email} is outside the allowed domains")
        return None
    user = {"email": email, "name": "", "picture": "", "role": "ap" if email in AP_EMAILS else "member"}
    with _token_lock:
        _token_cache[token] = (float(info.get("exp") or now + 300), user)
    return user


def require_page_header():
    """Behind IAP, and in password mode, identity rides on a browser cookie, so a form on another
    site could post here with it; the page sends this header on every call and a cross-site form
    cannot. (The cookie is also SameSite=Lax, which modern browsers enforce on their own.)"""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-Requested-With") != "XMLHttpRequest":
        raise AuthError(403, "changes must come from the dashboard page (X-Requested-With header)")


def require_user(ap_only=False):
    user = current_user()
    if not user:
        raise AuthError(401, "enter the dashboard password" if PASSWORD_MODE
                        else "sign in with your superhairpieces.com Google account")
    if not AUTH_DISABLED or PASSWORD_MODE:
        require_page_header()
    if ap_only and user["role"] != "ap":
        raise AuthError(403, "only the accounts-payable team can do this")
    g.user = user
    return user


EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def owner_emails(value):
    """A card's owners: one address or several, separated by commas, semicolons or spaces."""
    return [e for e in re.split(r"[\s,;]+", (value or "").lower()) if e]


def cards_owned_by(email, cards=None):
    cards = cards if cards is not None else get_store().list_cards()
    email = (email or "").lower()
    return {c["last4"] for c in cards if email and email in owner_emails(c.get("owner_email"))}


def may_edit(user, card_last4, cards=None):
    return user["role"] == "ap" or card_last4 in cards_owned_by(user["email"], cards)


def require_admin(write=False):
    """The /api/admin routes: accounts payable only. Their changes are refused while a real database
    is open to anyone (AUTH_DISABLED=1 without a password): the audit could not say who made them."""
    user = require_user(ap_only=True)
    if write and STORE_KIND != "memory" and auth_mode() == "open":
        raise AuthError(403, "admin changes are refused while the dashboard is open to anyone "
                             "(AUTH_DISABLED=1 without DASHBOARD_PASSWORD)")
    return user


# ---------------------------------------------------------------------------
# Jobs (one at a time per instance, run inside the request like the review services)
# ---------------------------------------------------------------------------
_job_lock = threading.Lock()
_last_job = {"name": None, "started": None, "finished": None, "result": None, "error": None, "progress": {}}

# The lock above keeps the dashboard's own long steps apart. The Cloud Run jobs (the weekly
# reminder, the Drive import) run in other containers, so the store holds a lease as well: whoever
# runs a long step takes it, renews it every LEASE_BEAT seconds, and a holder that died loses it
# LEASE_TTL seconds after its last renewal. The service runs on one instance, so a lease that
# another service instance holds was left by an earlier one (a deploy, a restart) and is taken over.
LEASE_NAME = "jobs"
LEASE_TTL = 300
LEASE_BEAT = 60
LEASE_POLL = 5             # a waiter asks this often: shorter than the import's LEASE_GAP
_RUN_ID = os.environ.get("CLOUD_RUN_EXECUTION") or os.environ.get("K_REVISION") or "local"
LEASE_HOLDER = f"service:{_RUN_ID}:{uuid.uuid4().hex[:8]}"      # the CLI's jobs switch it to job:...


class Lease:
    """The store's job lease while a long step runs; renewed in the background until release()."""

    def __init__(self, job, holder, held=True):
        self.job, self.holder, self.held = job, holder, held
        self._done = threading.Event()
        if held:
            threading.Thread(target=self._beat, name=f"lease-{job}", daemon=True).start()

    def _beat(self):
        while not self._done.wait(LEASE_BEAT):
            try:
                if not get_store().renew_lease(LEASE_NAME, self.holder, LEASE_TTL):
                    print(f"lease: {self.job} lost the job lease (expired and taken by someone else)")
                    return
            except Exception as exc:  # noqa: BLE001
                print(f"lease: could not renew for {self.job}: {exc}")

    def release(self):
        if self._done.is_set():
            return
        self._done.set()
        if self.held:
            try:
                get_store().release_lease(LEASE_NAME, self.holder)
            except Exception as exc:  # noqa: BLE001
                print(f"lease: could not release after {self.job} (it expires in {LEASE_TTL} s): {exc}")


def take_lease(job, wait=0, fail_open=True):
    """A Lease once the store's job lease is ours; None while another container holds it after
    `wait` seconds. fail_open: when the store cannot be reached, run anyway (the step itself will
    say what is wrong) rather than refuse; the jobs pass False and try again on their next run."""
    holder = LEASE_HOLDER
    take_over = "service:" if holder.startswith("service:") else ""
    deadline = time.time() + wait
    while True:
        try:
            if get_store().take_lease(LEASE_NAME, holder, job, LEASE_TTL, take_over):
                return Lease(job, holder)
        except Exception as exc:  # noqa: BLE001
            if not fail_open:
                raise
            print(f"lease: could not reach the store ({exc}); {job} runs without the job lease")
            return Lease(job, holder, held=False)
        if time.time() >= deadline:
            return None
        time.sleep(max(1, min(LEASE_POLL, deadline - time.time())))


def lease_live(lease, job=None, holder_prefix=""):
    """A job_lease row someone holds now (not expired), optionally for `job` or by a holder
    starting with `holder_prefix`."""
    lease = lease or {}
    until = _when(lease.get("expires_at"))
    return bool(lease.get("holder")) and (lease.get("holder") or "").startswith(holder_prefix) and \
        (job is None or lease.get("job") == job) and until is not None and until > dt.datetime.now(dt.timezone.utc)


def lease_view():
    """Who holds the job lease elsewhere, for a 409: {"job", "since"} or {}."""
    try:
        lease = get_store().get_lease(LEASE_NAME) or {}
    except Exception:  # noqa: BLE001
        return {}
    return {"job": lease.get("job"), "since": lease.get("acquired_at")} if lease.get("holder") else {}


def run_locked(name, fn, *args, **kwargs):
    if not _job_lock.acquire(blocking=False):
        return 409, {"status": "busy", "last_job": _last_job}
    lease = take_lease(name)
    if lease is None:
        _job_lock.release()
        return 409, {"status": "busy", "last_job": _last_job, "elsewhere": lease_view()}
    _last_job.update({"name": name, "started": now_iso(), "finished": None, "error": None, "progress": {}})
    try:
        result = fn(*args, **kwargs)
        _last_job["result"] = result
        return 200, {"status": "ok", **result}
    except Exception as exc:  # noqa: BLE001
        _last_job["result"] = {"error": str(exc)}
        _last_job["error"] = str(exc)
        print(f"{name} error: {exc}")
        return 500, {"status": "error", "message": str(exc)}
    finally:
        lease.release()
        _last_job["finished"] = now_iso()
        _job_lock.release()


# -- background jobs for the dashboard ------------------------------------------------------
# An invoice read or a match with the Gemini judge can run for minutes, and office networks drop
# a silent HTTP response after a minute or so (that cut the first real run). So the page starts
# the step here, in a thread under the same lock, and polls /api/jobs/current every two seconds.
# Every job has an id and the last few stay readable by id, so a tab polling a finished job still
# gets its result after someone else has started the next one. Cloud Run keeps the CPU allocated
# between requests for this (--no-cpu-throttling).
JOB_INDEX_LIMIT = 8        # files per batch, so the progress moves every ten seconds or so
JOB_HISTORY = 10
JOB_LOG_LINES = 100        # the tail of a job's log that travels with each poll
_jobs = OrderedDict()      # id -> job, newest last


def _job_log(line):
    """Where a background job's log lines go: the page shows the last one under the step and copies
    the tail into its details box (`log`, with `lines` counting everything so far); 'indexed' lines
    are counted for the progress bar."""
    line = str(line)
    progress = _last_job["progress"]
    progress["note"] = line[:200]
    if line.startswith("indexed "):
        progress["live"] = progress.get("live", 0) + 1
    tail = progress.setdefault("log", [])
    tail.append(line[:300])
    if len(tail) > JOB_LOG_LINES:
        del tail[:len(tail) - JOB_LOG_LINES]
    progress["lines"] = progress.get("lines", 0) + 1
    print(line)


def start_job(name, fn, *args, **kwargs):
    """Run fn in the background; 409 while another job (background or in-request) holds the lock."""
    if not _job_lock.acquire(blocking=False):
        return 409, {"status": "busy", "job": job_view()}
    lease = take_lease(name)
    if lease is None:              # the Drive import or the weekly reminder is running in its own container
        _job_lock.release()
        return 409, {"status": "busy", "job": job_view(), "elsewhere": lease_view()}
    job = {"id": uuid.uuid4().hex, "name": name, "started": now_iso(), "finished": None, "result": None,
           "error": None, "progress": {}}
    _jobs[job["id"]] = job
    while len(_jobs) > JOB_HISTORY:
        _jobs.popitem(last=False)
    # _last_job mirrors the running job (same progress dict) for /health and the log hook.
    _last_job.update({k: job[k] for k in ("name", "started", "finished", "result", "error", "progress")})

    def run():
        try:
            job["result"] = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            job["error"] = str(exc)
            print(f"{name} error: {exc}")
        finally:
            lease.release()
            job["finished"] = now_iso()
            _last_job.update({k: job[k] for k in ("finished", "result", "error")})
            _job_lock.release()

    threading.Thread(target=run, name=f"job-{name}", daemon=True).start()
    return 202, {"status": "started", "job": job_view(job)}


def job_view(job=None):
    """A job as the page sees it: the given one, else the newest; an empty view before any job."""
    if job is None:
        job = next(reversed(_jobs.values()), None)
    if job is None:
        return {"id": None, "name": None, "started": None, "finished": None, "error": None, "progress": {}, "running": False}
    view = {k: job.get(k) for k in ("id", "name", "started", "finished", "error")}
    view["progress"] = dict(job.get("progress") or {})
    if "log" in view["progress"]:
        view["progress"]["log"] = list(view["progress"]["log"])    # the job thread keeps appending
    view["running"] = not job["finished"]
    if job["finished"]:
        view["result"] = job.get("result")
    return view


def index_until_done(year, month, limit=JOB_INDEX_LIMIT, on_read=None):
    """The read step: batch after batch until the month's folders hold nothing new (or nothing readable).

    on_read(done, total), when given, follows the whole pass: files finished so far (read or failed)
    against the files the pass will read, known once the first round has listed the folders."""
    progress = _last_job["progress"]
    read, errors, remaining, last_remaining, stalls = 0, [], 0, None, 0
    stalled = True                      # until a round ends with nothing remaining
    tally = {"before": 0, "total": None}  # files finished in earlier rounds; the pass's total

    def round_read(done, batch, new):
        retries = max(batch - min(new, limit), 0)        # earlier failures retried in this round
        total = tally["before"] + new + retries
        tally["total"] = total if tally["total"] is None else max(tally["total"], total)
        on_read(tally["before"] + done, tally["total"])
        if done == batch:
            tally["before"] += batch

    for rounds in range(1, MAX_INDEX_ROUNDS + 1):
        # The month-wide read step passes no on_read and calls index_month exactly as before.
        extra = {"on_read": round_read} if on_read else {}
        result = index_month(year, month, limit=limit, log=_job_log, **extra)
        if result.get("skipped"):
            progress["skipped"] = result["skipped"]
            return {"indexed": 0, "errors": [], "remaining": 0, "skipped": result["skipped"]}
        read += int(result.get("indexed") or 0)
        errors.extend(result.get("errors") or [])
        remaining = int(result.get("remaining") or 0)
        progress.update({"read": read, "live": 0, "remaining": remaining, "errors": len(errors), "rounds": rounds,
                         "files_seen": result.get("files_seen"), "month_folders": result.get("month_folders")})
        if not remaining:
            stalled = False
            break
        stalls = stalls + 1 if last_remaining is not None and remaining >= last_remaining else 0
        if stalls >= 2:                 # the same files keep failing; their errors are in the list
            break
        last_remaining = remaining
    return {"indexed": read, "errors": errors, "remaining": remaining, "stalled": stalled}


def dashboard_url():
    if DASHBOARD_URL:
        return DASHBOARD_URL
    try:
        return request.url_root.rstrip("/")
    except RuntimeError:
        return ""


def folder_url(folder_id):
    return f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else ""


_month_folder_ids = {}         # "YYYY MM" -> id of that month's folder under Invoice
_month_folder_names = {}       # folder id -> its name in Drive, when a person named it "2026-09" or "2026_9"


def resolve_month_folder(drive, year, month, create):
    """(id of the month's folder under Invoice, or None; created). The cache first (shared with
    month_folder_url), then the root's folders: the exact "2026 09" wins, else a folder a person named
    another way ("2026-09", "2026_9") that the index reads as the same month, else, when create is
    set, a new "2026 09"."""
    name = month_folder_name(year, month)
    if name in _month_folder_ids:
        folder_id = _month_folder_ids[name]
        if not create or _folder_alive(drive, folder_id):
            return folder_id, False
        print(f"receipt: month folder {name} ({folder_id}) was deleted or trashed in Drive; looking it up again")
        _month_folder_ids.pop(name, None)
    folders = [f for f in drive.list_children(INVOICE_FOLDER_ID) if f.get("mimeType") == FOLDER]
    found = next((f for f in folders if f.get("name") == name), None) or next(
        (f for f in folders if invoice_index.parse_month_folder(f.get("name") or "") == name), None)
    if found:
        folder_id, created = found["id"], False
        _month_folder_names[folder_id] = found.get("name") or name
    elif create:
        folder_id, created = drive.ensure_folder(INVOICE_FOLDER_ID, name), True
    else:
        return None, False
    _month_folder_ids[name] = folder_id
    return folder_id, created


def _folder_alive(drive, folder_id):
    """A cached folder before a file is saved into it: still there and not in the Drive trash (a
    trashed folder still answers, and a file saved into it would be lost with it)."""
    try:
        return not drive.get(folder_id, fields="id,trashed").get("trashed")
    except DriveError as exc:
        if _drive_404(exc):
            return False
        raise


def month_folder_label(folder_id, year, month):
    """The month folder's name as it is in Drive (what the person sees there)."""
    return _month_folder_names.get(folder_id) or month_folder_name(year, month)


def _drive_404(exc):
    return isinstance(exc, DriveError) and "-> 404" in str(exc)


def in_month_folder(drive, year, month, create, work):
    """work(month folder id or None, created), under the month's folder. A cached id that Drive no
    longer knows (404: someone deleted the folder since) is forgotten and looked up once more."""
    name = month_folder_name(year, month)
    cached = name in _month_folder_ids
    folder_id, created = resolve_month_folder(drive, year, month, create)
    try:
        return work(folder_id, created)
    except DriveError as exc:
        if not (cached and _drive_404(exc)):
            raise
        print(f"receipt: month folder {name} ({folder_id}) is gone from Drive; looking it up again")
        _month_folder_ids.pop(name, None)
        folder_id, created = resolve_month_folder(drive, year, month, create)
        return work(folder_id, created)


def month_folder_url(year, month):
    """Link to Invoice/YYYY MM, falling back to the root folder."""
    name = month_folder_name(year, month)
    if name not in _month_folder_ids:
        try:
            found = get_drive().find_child_folder(INVOICE_FOLDER_ID, name)
        except Exception as exc:  # noqa: BLE001
            print(f"month folder lookup failed: {exc}")
            return folder_url(INVOICE_FOLDER_ID)
        if not found:                       # the folder may be created later; look again next time
            return folder_url(INVOICE_FOLDER_ID)
        _month_folder_ids[name] = found
    return folder_url(_month_folder_ids[name])


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------
def _words(text):
    return re.findall(r"[a-z0-9]+", html.unescape(str(text or "")).lower())


def description_tie(a, b):
    """How closely two readings of a line's text agree: 3 the same words, 2 one cut short of the
    other (a bank's 20-character text), 1 most words in common, 0 not the same line."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0
    ja, jb = " ".join(wa), " ".join(wb)
    if ja == jb:
        return 3
    if ja.startswith(jb) or jb.startswith(ja):
        return 2
    sa, sb = set(wa), set(wb)
    return 1 if len(sa & sb) / len(sa | sb) >= 0.5 else 0


def stored_under_other_ids(store, candidates, parsed_ids):
    """{parsed id: stored row} for lines of a statement that are stored, but under another id: an
    earlier reading of the same lines (Gemini's, or this parser's before a fix changed a currency,
    a sign or a description, all of which the id is made of). Same card, same date, the same amount
    up to its sign, and a description that says the same; each stored row stands for one line.

    Pairs are made best first over each card's month: the same words and type, then the same words,
    then a cut-short text (same type first), then most words in common; so a loose match never takes
    a row that a closer line needs ("AMAZON MKTPL*AAA111" vs "*BBB222", a same-day sale and refund).
    The type, not the sign, is what earlier readings got right (Gemini kept purchases negative)."""
    found, groups = {}, {}
    for t in candidates:
        groups.setdefault((t["card_last4"], t["year"], t["month"]), []).append(t)
    for (card, year, month), lines in groups.items():
        free = {}
        for r in store.list_transactions(year, month, card):
            if r["id"] not in parsed_ids:           # a stored line this reading has under its own id is taken
                key = (str(r["txn_date"])[:10], round(abs(float(r["amount"])) * 100))
                free.setdefault(key, []).append(r)
        for tie, same_type in ((3, True), (3, False), (2, True), (2, False), (1, True), (1, False)):
            for t in lines:
                if t["id"] in found:
                    continue
                bucket = free.get((t["txn_date"], round(abs(float(t["amount"])) * 100)), [])
                hit = next((r for r in bucket if (not same_type or r.get("type") == t["type"])
                            and description_tie(t["description"], r.get("description")) >= tie), None)
                if hit:
                    bucket.remove(hit)
                    found[t["id"]] = hit
    return found


def ingest_statement(file_name, data, mime_type, uploaded_by, source=None):
    """Parse the upload and store what is new. Returns the summary shown to AP.

    source: the Drive file a statement was imported from (the Bank Transactions folder). It stays
    where it is, so no copy is saved and the upload links to it; a version that adds no new line
    is not listed as an upload, and the summary also carries every line's id."""
    store = get_store()
    parsed = statements.parse_statement(file_name, data, mime_type, gemini=get_gemini())
    txns = parsed["transactions"]
    existing_cards = {c["last4"]: c for c in store.list_cards()}
    card_rows = []
    for last4, info in parsed["cards"].items():
        if last4 not in existing_cards:
            label = f"Card ending {last4}" + (f" - {info['holder_name']}" if info.get("holder_name") else "")
            card_rows.append({"last4": last4, "label": label, "holder_name": info.get("holder_name") or "",
                              "active": True})
        elif info.get("holder_name") and not existing_cards[last4].get("holder_name"):
            card_rows.append({"last4": last4, "holder_name": info["holder_name"]})
    store.upsert_cards(card_rows)
    matching.forget_card_aliases()          # a new card is no longer a reissued number of another

    existing_ids = store.existing_transaction_ids([t["id"] for t in txns])
    elsewhere = stored_under_other_ids(store, [t for t in txns if t["id"] not in existing_ids], {t["id"] for t in txns})
    new = [t for t in txns if t["id"] not in existing_ids and t["id"] not in elsewhere]
    if elsewhere:
        parsed_by_id = {t["id"]: t for t in txns}
        differ = sum(1 for tid, r in elsewhere.items()
                     if r.get("currency") != parsed_by_id[tid]["currency"] or r.get("type") != parsed_by_id[tid]["type"]
                     or round(float(r["amount"]), 2) != parsed_by_id[tid]["amount"])
        n = len(elsewhere)
        parsed["warnings"].append(
            f"{n} line{'s were' if n != 1 else ' was'} already in the dashboard from an earlier reading of this "
            "statement and not added again" + (f"; {differ} read differently there (currency, sign or type)." if differ else "."))
    months = sorted({month_folder_name(t["year"], t["month"]) for t in txns})
    stamp = now_iso()
    drive_file = {}
    if source:
        drive_file = {"id": source.get("id"), "webViewLink": source.get("webViewLink")}
    elif STATEMENTS_FOLDER_ID and new:
        try:
            drive = get_drive()
            folder = drive.ensure_folder(STATEMENTS_FOLDER_ID, months[-1])
            drive_file = drive.upload(folder, file_name, data, mime_type)
        except Exception as exc:  # noqa: BLE001
            print(f"statement copy to Drive failed: {exc}")
    statement = {"id": None}
    if new or not source:
        statement = store.insert_statement({
            "file_name": file_name, "file_type": mime_type or "", "format": parsed["format"],
            "uploaded_by": uploaded_by, "uploaded_at": stamp,
            "period_start": parsed["period_start"], "period_end": parsed["period_end"],
            "cards": sorted(parsed["cards"]), "months": months,
            "transaction_count": len(txns), "new_count": len(new),
            "drive_file_id": drive_file.get("id"), "drive_link": drive_file.get("webViewLink"),
            "warnings": parsed["warnings"],
        })
    rows = []
    for t in new:
        row = dict(t)
        row.update({"statement_id": statement["id"], "created_at": stamp,
                    "invoice_status": "missing" if t["type"] == "purchase" else "not_required",
                    "invoice_id": None, "cost_center": None, "usage": None, "note": None})
        rows.append(row)
    try:
        store.insert_transactions(rows)
    except Exception:
        # No upload left behind without its lines (a retry would add another): take the row back
        # out, with any lines of it that did land (the insert goes 500 at a time).
        if statement.get("id"):
            try:
                store.delete_statement(statement["id"])
            except Exception as exc:  # noqa: BLE001
                print(f"statement {statement['id']}: could not take back the upload after a failed insert: {exc}")
        raise
    summary = {
        "statement_id": statement["id"], "file_name": file_name, "format": parsed["format"],
        "period_start": parsed["period_start"], "period_end": parsed["period_end"],
        "transactions": len(txns), "new": len(new), "already_stored": len(txns) - len(new),
        "cards": {l4: {"holder_name": c.get("holder_name", ""), "count": c["count"]} for l4, c in parsed["cards"].items()},
        "months": [[int(m[:4]), int(m[5:])] for m in months],
        "new_months": sorted({(t["year"], t["month"]) for t in new}, reverse=True),
        "warnings": parsed["warnings"], "drive_link": drive_file.get("webViewLink"),
    }
    if source:
        summary["transaction_ids"] = [t["id"] for t in txns]
        summary["line_months"] = {t["id"]: [t["year"], t["month"]] for t in txns}
    return summary


def index_month(year, month, limit=None, log=print, on_read=None):
    folders = [month_folder_name(y, m) for y, m in neighbouring_months(year, month)]
    gemini = get_gemini()
    if gemini is None:
        return {"files_seen": 0, "indexed": 0, "remaining": 0, "errors": [], "month_folders": folders,
                "skipped": "AI_DISABLED=1: invoices are not read"}
    return invoice_index.index_invoices(get_store(), get_drive(), gemini, INVOICE_FOLDER_ID,
                                        month_folders=folders, limit=limit or INDEX_BATCH, workers=INDEX_WORKERS, log=log,
                                        on_read=on_read)


def index_all(month_folders=None, limit=None):
    gemini = get_gemini()
    if gemini is None:
        return {"files_seen": 0, "indexed": 0, "remaining": 0, "errors": [], "skipped": "AI_DISABLED=1"}
    return invoice_index.index_invoices(get_store(), get_drive(), gemini, INVOICE_FOLDER_ID,
                                        month_folders=month_folders, limit=limit or INDEX_BATCH, workers=INDEX_WORKERS)


def match_month(year, month, card=None, log=print):
    return matching.match_month(get_store(), get_gemini(), year, month, card, use_ai=not AI_DISABLED, log=log)


def rematch_transaction(txn_id, user_email=""):
    """The dashboard's per-row Rematch, as a background job: read the invoices newly filed in Drive
    for the purchase's month and the months either side (the usual reason to rematch is an invoice
    just added after a reminder), then look for this purchase's invoice again."""
    store = get_store()
    txn = store.get_transaction(txn_id)
    if not txn:
        raise matching.RematchError("transaction not found")
    year, month = int(txn["year"]), int(txn["month"])
    progress = _last_job["progress"]
    # percent: 0-5 listing the Drive folders, 5-75 reading new invoices (files finished of files to
    # read), 78 the rules, 85 waiting for Gemini's judgement, 100 done.
    progress.update({"year": year, "month": month, "card": txn.get("card_last4"), "transaction_id": txn_id,
                     "stage": "read", "percent": 2})
    _job_log(f"Rematch: reading new invoices for {month_folder_name(year, month)} and the months either side")

    def on_read(done, total):
        progress.update({"files_done": done, "files_total": total,
                         "percent": 5 + int(70 * min(done, total) / total) if total else 75})

    read = index_until_done(year, month, on_read=on_read)

    def on_stage(stage):
        progress.update({"stage": "ai" if stage == "ai" else "match", "percent": 85 if stage == "ai" else 78})

    on_stage("rules")
    result = matching.match_transaction(store, get_gemini(), txn_id, use_ai=not AI_DISABLED, log=_job_log,
                                        updated_by=user_email or None, on_stage=on_stage)
    result["invoices_read"] = int(read.get("indexed") or 0)
    errors = read.get("errors") or []
    result["read_errors"] = len(errors)
    result["read_error_files"] = [str(e.get("file") or "")[:120] for e in errors[:5]]
    if read.get("skipped"):
        result["read_skipped"] = read["skipped"]
    updated = store.get_transaction(txn_id)
    if updated is None:
        raise matching.RematchError("the transaction was removed while Rematch was running")
    row = with_invoices(store, [updated])[0]
    row["editable"] = True              # the job only starts for someone allowed to edit the row
    result["transaction"] = row
    progress.update({"stage": "done", "percent": 100})
    return result


def notify_month(year, month, card=None, sent_by="", dry_run=False, dashboard=None):
    """One Chat card per credit card that still has purchases without an invoice.

    `dashboard` is the absolute dashboard URL for the card buttons; a background job passes the
    one resolved in its request, because there is no request to derive it from in the thread."""
    base = dashboard if dashboard is not None else dashboard_url()
    store = get_store()
    year, month = int(year), int(month)
    cards = {c["last4"]: c for c in store.list_cards()}
    txns = store.list_transactions(year, month, card)
    by_card = {}
    for t in txns:
        if t.get("type") != "purchase":
            continue
        bucket = by_card.setdefault(t["card_last4"], {"missing": [], "possible": [], "uncoded": 0})
        if t.get("invoice_status") == "missing":
            bucket["missing"].append(t)
        elif t.get("invoice_status") == "possible":
            bucket["possible"].append(t)
        if not str(t.get("usage") or "").strip():       # uncoded: no use case (Nov 2026: was no cost centre)
            bucket["uncoded"] += 1
    results, folder = [], month_folder_url(year, month) if not dry_run else ""
    for last4, bucket in sorted(by_card.items()):
        # Reminders are about receipts: a card whose receipts are all in gets no card, even when some
        # purchases have no use case yet (the dashboard's "No use case" KPI shows that).
        if not bucket["missing"] and not bucket["possible"]:
            results.append({"card": last4, "sent": False, "detail": "nothing missing"})
            continue
        card_info = cards.get(last4, {"last4": last4})
        if card_info.get("active") is False:
            results.append({"card": last4, "sent": False, "detail": "card inactive"})
            continue
        # Cards with receipt upload get "Add receipts" (a server path: it survives the Google sign-in,
        # a #fragment would not) instead of the Invoice folder button.
        receipts_url = (f"{base.rstrip('/')}/receipts/{last4}/{year}/{month:02d}"
                        if base and receipt_upload_enabled(card_info) else "")
        payload = chat.missing_invoices_card(card_info, year, month, bucket["missing"], bucket["possible"],
                                             base, "" if receipts_url else folder, bucket["uncoded"],
                                             receipts_url=receipts_url)
        if dry_run:
            results.append({"card": last4, "sent": False, "detail": "dry run", "missing": len(bucket["missing"]),
                            "possible": len(bucket["possible"]), "payload": payload})
            continue
        ok, detail = chat.post(chat.webhook_for(CARD_WEBHOOKS, last4), payload)
        store.log_notification({"card_last4": last4, "year": year, "month": month, "sent_at": now_iso(),
                                "sent_by": sent_by, "missing_count": len(bucket["missing"]),
                                "possible_count": len(bucket["possible"]),
                                "transaction_ids": [t["id"] for t in bucket["missing"] + bucket["possible"]],
                                "ok": ok, "detail": detail})
        results.append({"card": last4, "sent": ok, "detail": detail, "missing": len(bucket["missing"]),
                        "possible": len(bucket["possible"])})
        print(f"notify {last4} {year}-{month:02d}: {detail} ({len(bucket['missing'])} missing)")
    return {"year": year, "month": month, "cards": results}


def process_statement_sync(file_name, data, mime_type, uploaded_by, notify=True):
    """Upload + index + match + notify in one go (CLI, curl, tests)."""
    summary = ingest_statement(file_name, data, mime_type, uploaded_by)
    summary["index"], summary["match"], summary["notify"] = [], [], []
    for year, month in summary["months"]:
        for _ in range(MAX_INDEX_ROUNDS):
            result = index_month(year, month, limit=INDEX_BATCH)
            summary["index"].append({"year": year, "month": month, **result})
            if not result.get("remaining"):
                break
        summary["match"].append({"year": year, "month": month, **match_month(year, month)})
        if notify:
            summary["notify"].append(notify_month(year, month, sent_by=uploaded_by))
    return summary


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------
def linked_by_hand(t):
    """A receipt added from the dashboard, or an invoice picked with Link…: no matcher vouched for it."""
    note = t.get("match_note") or ""
    return t.get("match_method") == "manual" and note.startswith(("receipt uploaded by ", "linked by "))


def link_part(store, txn, inv):
    """(part, note) for a Link of txn to inv by hand; (False, why) when the invoice has no room. An
    invoice that lists several card charges (an Amazon order shipped in parts) takes one transaction
    per charge: the free charge of the same amount, else the free one closest in amount, and the note
    says which. Any other invoice is linked whole, and only when nothing else is linked to it."""
    taken = matching.taken_parts(store, [txn])
    held = taken.get(inv["id"]) or set()
    if not matching.shared(inv):
        if held:
            return False, "that invoice is already linked to another transaction"
        return None, ""
    if matching.WHOLE in held:
        return False, "that invoice is already linked to another transaction"
    free = [(n, c) for n, c in matching.listed_charges(inv) if n not in held]
    if not free:
        return False, "every card charge that invoice lists is already linked to a transaction"
    matching.refresh_card_aliases(store)
    mine = [(n, c) for n, c in free if not matching.cards_differ(c.get("card_last4"), txn.get("card_last4"))]
    if not mine:
        return False, "the charges of that invoice still free are on another card"
    amounts = matching.charge_amounts(txn, inv) or [abs(float(txn["amount"]))]
    n, c = min(mine, key=lambda nc: (round(min(abs(a - float(nc[1]["amount"])) for a in amounts), 2),
                                     matching.charge_days(txn, nc[1]) if matching.charge_days(txn, nc[1]) is not None else 999,
                                     nc[0]))
    _c, number, count = matching.charge_of(inv, n)
    return n, f"charge {number} of {count} ({c.get('date') or 'undated'}, {float(c['amount']):.2f})"


ALSO_FIELDS = ("id", "file_name", "web_view_link", "vendor", "invoice_number", "invoice_date", "total", "currency",
               "document_type", "month_folder", "vendor_folder", "removed_at")


def with_invoices(store, txns):
    ids = sorted({t["invoice_id"] for t in txns if t.get("invoice_id")}
                 | {d for t in txns for d in t.get("also_invoice_ids") or ()})
    invoices = {i["id"]: i for i in store.list_invoices(ids=ids)} if ids else {}
    if invoices:
        matching.refresh_card_aliases(store)     # a reissued card's number is no "other card"
    with _reading_lock:
        reading = set(_reading)
    out = []
    for t in txns:
        row = {k: v for k, v in t.items() if k != "raw"}
        inv = invoices.get(t.get("invoice_id"))
        row["invoice"] = ({k: inv.get(k) for k in ("id", "file_name", "web_view_link", "vendor", "invoice_number",
                                                   "invoice_date", "total", "currency", "vendor_folder", "month_folder",
                                                   "card_last4", "is_invoice", "extraction_error", "extracted_at",
                                                   "summary", "created_at")}
                          if inv else None)
        if inv:
            row["invoice"]["reading"] = inv["id"] in reading
            charge = matching._charge_view(inv, t.get("invoice_part"))
            if charge:
                row["invoice"]["charge"] = charge
        # The other documents the charge pays (invoices paid together) or is confirmed by.
        row["also_invoices"] = [{k: invoices[d].get(k) for k in ALSO_FIELDS}
                                for d in t.get("also_invoice_ids") or () if d in invoices]
        # What the receipt says against the charge, worked out each time from the stored facts: a
        # disagreement is shown, never turned into a status (the link stays as the person made it).
        row["checks"] = (matching.invoice_checks(t, matching.link_view(t, inv), reading=inv["id"] in reading,
                                                 also=[invoices.get(d) for d in t.get("also_invoice_ids") or ()])
                         if inv and linked_by_hand(t) else [])
        row["status_label"] = STATUS_LABELS.get(t.get("invoice_status"), t.get("invoice_status"))
        out.append(row)
    return out


def month_list(store):
    cards = {c["last4"]: c for c in store.list_cards()}
    months = {}
    for row in store.month_summary():
        key = (int(row["year"]), int(row["month"]))
        m = months.setdefault(key, {"year": key[0], "month": key[1], "label": f"{calendar.month_name[key[1]]} {key[0]}",
                                    "transactions": 0, "spend": 0.0, "matched": 0, "possible": 0, "missing": 0,
                                    "waived": 0, "uncoded": 0, "cards": {}})
        entry = {k: (float(row[k]) if k == "spend" else int(row[k])) for k in
                 ("transactions", "spend", "matched", "possible", "missing", "waived", "uncoded")}
        m["cards"][row["card_last4"]] = {**entry, "label": cards.get(row["card_last4"], {}).get("label") or f"Card ending {row['card_last4']}"}
        for k, v in entry.items():
            m[k] = round(m[k] + v, 2) if k == "spend" else m[k] + v
    return [months[k] for k in sorted(months, reverse=True)]


def card_view(c, aliases=None, by_last4=None):
    """aliases: {number receipts show: statement card} (matching.all_card_aliases): the numbers merchants
    may show for this card (the number printed on it, or one it was reissued to). by_last4: every card,
    for a supplementary card's primary (who holds it) and a primary's supplementary cards."""
    by_last4 = by_last4 or {}
    primary = by_last4.get(c.get("primary_last4") or "")
    return {"last4": c["last4"], "label": c.get("label") or f"Card ending {c['last4']}",
            "receipts_show": sorted(n for n, card in (aliases or {}).items() if card == c["last4"]),
            "holder_name": c.get("holder_name") or "", "owner_email": c.get("owner_email") or "",
            "chat_space": c.get("chat_space") or "", "active": c.get("active", True) is not False,
            "group_name": c.get("group_name") or "", "card_number": c.get("card_number") or "",
            "name_on_card": c.get("name_on_card") or "", "person_id": c.get("person_id") or "",
            "primary_last4": c.get("primary_last4") or "",
            "primary_holder": (primary or {}).get("holder_name") or "",
            "primary_card_number": (primary or {}).get("card_number") or "",
            "supplementary": sorted(o["last4"] for o in by_last4.values() if o.get("primary_last4") == c["last4"]),
            "webhook_configured": bool(CARD_WEBHOOKS.get(c["last4"])),
            "webhook_default": bool(chat.webhook_for(CARD_WEBHOOKS, c["last4"])),
            "default_cost_center": c.get("default_cost_center") or "",
            # The card's own switch (True, False, or None: RECEIPT_UPLOAD_CARDS decides); receipt_upload is
            # what applies.
            "receipt_upload_setting": c.get("receipt_upload"),
            "receipt_upload": receipt_upload_enabled(c),
            # The card as issued ("" when not set): a BANKS key, a CARD_NETWORKS name, the bank's product.
            "bank": c.get("bank") or "", "network": c.get("network") or "", "product": c.get("product") or ""}


def card_views(cards, seen=None):
    """The cards for the dashboard; with seen (the card numbers invoices show), each card's other numbers
    that receipts actually show (the Cards dialog); the number entered as printed on the card always."""
    by_last4 = {c["last4"]: c for c in cards}
    aliases = {n: c for n, c in matching.all_card_aliases(cards).items()
               if n in (seen or ()) or n == (by_last4.get(c) or {}).get("card_number")}
    return [card_view(c, aliases, by_last4) for c in cards]


GROUP_MAX = 40
CARD_LABEL_RE = re.compile(r"^Card ending (\d{4})(?: - (.*))?$")

# The banks a card can be from (cards.bank holds the key). The Cards tab takes its Bank list and each
# card's logo from this registry (GET /api/admin/cards "banks"): a bank added here is offered and drawn
# with no change to the page. short is its name where room is tight (the table, when the logo cannot
# load). Its logos go in static/banks/ (logo for a light background, logo_on_dark for the card face); its
# face colours are admin.html's .cc-face.bank-<key>, without which it gets a plain graphite face.
BANKS = {
    "rbc": {"name": "RBC Royal Bank", "short": "RBC", "logo": "/static/banks/rbc.svg", "logo_on_dark": "/static/banks/rbc.svg"},
    "chase": {"name": "Chase", "short": "Chase", "logo": "/static/banks/chase.svg", "logo_on_dark": "/static/banks/chase-white.svg"},
}
BANK_LOGO_PATH_RE = re.compile(r"/static/banks/[a-z0-9-]+\.svg")


def bank_list():
    """[{key, name, short, logo, logo_on_dark}] in BANKS order, for the page's pickers, card faces and table."""
    return [dict(key=key, **bank) for key, bank in BANKS.items()]


def default_card_label(last4, holder):
    return f"Card ending {last4}" + (f" - {holder}" if holder else "")


# ---------------------------------------------------------------------------
# Receipts added from the dashboard (cards in RECEIPT_UPLOAD_CARDS)
#
# The holder photographs or uploads the receipt of one purchase; it is saved to
# Invoice/<YYYY MM of the purchase>/<vendor>/ and linked to it as a manual match,
# which no matcher touches. Write order: Drive upload, a placeholder invoice row
# (no total, so no match job can pair it; it also satisfies the foreign key), the
# link (compare and set), then Gemini reads the bytes in the background. A link
# that loses the race, or a placeholder that cannot be stored, trashes its own
# file, so no orphan joins the matching pool.
# ---------------------------------------------------------------------------
_folder_lock = threading.Lock()        # month and vendor folders: find or create, one request at a time
_reading = set()                       # invoice ids being read in the background
_reading_lock = threading.Lock()
_receipt_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="receipt-read")
_receipt_times = {}                    # email -> [time of each receipt save in the window]
_receipt_lock = threading.Lock()
WITHDRAWN = "upload withdrawn: the purchase changed while it was saving"
# Same prefix: index_invoices skips the file and candidate_invoices leaves it out, even if its trash failed.
REMOVED = "upload withdrawn: removed with Remove…"


def receipt_access(user, txn, need_enabled=True):
    """AuthError(403) unless the card has receipt upload (when need_enabled) and the user may edit it."""
    last4 = txn["card_last4"]
    if need_enabled and not receipt_upload_enabled(last4):     # the card's own switch, else the env
        raise AuthError(403, f"Receipt upload isn't switched on for card {last4}.")
    if not may_edit(user, last4):
        raise AuthError(403, f"Only the card's owner or accounts payable can add receipts to card {last4}.")


def receipt_throttled(email):
    """Counts this save; True when the person has saved RECEIPT_MAX in the last RECEIPT_WINDOW seconds
    (a stuck retry loop on a phone, or a script). In-process, like the password counters: one instance."""
    now = time.time()
    with _receipt_lock:
        for who in [w for w, times in _receipt_times.items() if not any(t > now - RECEIPT_WINDOW for t in times)]:
            _receipt_times.pop(who, None)
        mine = [t for t in _receipt_times.get(email, []) if t > now - RECEIPT_WINDOW]
        throttled = len(mine) >= RECEIPT_MAX
        if not throttled:
            mine.append(now)
        _receipt_times[email] = mine
        return throttled


def vendor_choice(drive, store, txn, month_id, requested, children=None):
    """(vendor folder, its id when it already exists in the month folder, source) for txn's receipt.

    requested (what the person typed under Change) is made safe and snapped to an existing spelling;
    when nothing usable is left, or nothing was typed, the folder is suggested from the descriptor.
    children: the month folder's listing when the caller has it already."""
    year, month = int(txn["year"]), int(txn["month"])
    if children is None:
        children = drive.list_children(month_id) if month_id else []
    this_month = {}
    for f in children:
        if f.get("mimeType") == FOLDER and f.get("name"):
            this_month.setdefault(f["name"], f["id"])
    # Other months' folders, from the index: the live listing above already covers this month.
    current, earlier = month_folder_name(year, month), {}
    months = [month_folder_name(y, m) for y, m in neighbouring_months(year, month, 12, 1)]
    for inv in store.list_invoices(month_folders=months):
        name = inv.get("vendor_folder")
        if inv.get("removed_at") or not name or inv.get("month_folder") == current:
            continue
        files, latest = earlier.get(name, (0, ""))
        earlier[name] = (files + 1, max(latest, inv.get("month_folder") or ""))
    safe = receipts.safe_folder_name(requested) if requested else None
    if safe:
        name, in_month = receipts.snap_folder_name(safe, list(this_month), list(earlier))
        source = "this_month" if in_month else ("earlier_month" if name in earlier else "new")
    else:
        name, source = receipts.suggest_vendor_folder(txn, list(this_month), earlier)
    return name, (this_month.get(name) if source == "this_month" else None), source


def receipt_target(txn):
    """Where Add receipt… would save txn's receipt, for the dialog to show. Never creates anything."""
    drive, store = get_drive(), get_store()
    year, month = int(txn["year"]), int(txn["month"])

    def look(month_id, _created):
        children = drive.list_children(month_id) if month_id else []
        name, existing, source = vendor_choice(drive, store, txn, month_id, None, children=children)
        folders = {f["name"] for f in children if f.get("mimeType") == FOLDER and f.get("name")}
        return {"month_folder": month_folder_label(month_id, year, month), "month_exists": bool(month_id),
                "vendor_folder": name, "vendor_exists": bool(existing), "source": source,
                "folders": sorted(folders, key=str.casefold),
                "limits": {"max_pages": receipts.MAX_PAGES, "max_image_mb": receipts.MAX_IMAGE_BYTES // (1024 * 1024),
                           "max_pdf_mb": receipts.MAX_PDF_BYTES // (1024 * 1024)}}

    return in_month_folder(drive, year, month, False, look)


def transaction_row(store, txn, editable=True):
    row = with_invoices(store, [txn])[0]
    row["editable"] = editable
    return row


def _trash_quietly(drive, file_id, why):
    """Best effort: (moved to the Drive trash, error text)."""
    try:
        drive.trash(file_id)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        print(f"receipt: could not trash {file_id} ({why}): {exc}")
        return False, str(exc)[:300]


def _not_saved(store, txn, error):
    return {"status": "not_saved", "error": error, "transaction": transaction_row(store, txn)}


def _invoice_label(inv, txn):
    """' (Uber · CAD 23.45)' for the invoice a purchase already has, '' when there is nothing to name."""
    if not inv:
        return ""
    bits = [b for b in [inv.get("vendor") or inv.get("vendor_folder") or inv.get("file_name")] if b]
    if inv.get("total") is not None:
        bits.append(chat.money(inv["total"], inv.get("currency") or txn.get("currency") or "CAD"))
    return f" ({' · '.join(bits)})" if bits else ""


def _uploaded_with(drive, file_id):
    """The upload_id a dashboard upload stored with the Drive file ('' when none or unreadable)."""
    try:
        meta = drive.get(file_id, fields="id,appProperties")
    except DriveError as exc:
        print(f"receipt: could not read {file_id} for a replay check: {exc}")
        return ""
    return (meta.get("appProperties") or {}).get("apUpload") or ""


def _saved_body(store, txn, file_info, reading, replayed):
    return {"status": "saved", "replayed": replayed, "reading": reading, "file": file_info,
            "transaction": transaction_row(store, txn)}


def _replay(store, drive, txn, email, upload_id, pages, data, mime):
    """(200, the saved body) when txn is linked to this person's receipt sent with upload_id: a
    retry whose first save went through (its answer lost, or still on its way). Else None."""
    if not (upload_id and txn.get("invoice_status") == "matched" and txn.get("invoice_id")
            and txn.get("match_note") == f"receipt uploaded by {email}"
            and _uploaded_with(drive, txn["invoice_id"]) == upload_id):
        return None
    inv = store.get_invoice(txn["invoice_id"]) or {}
    with _reading_lock:
        reading = txn["invoice_id"] in _reading
    file_info = {"id": txn["invoice_id"], "name": inv.get("file_name"),
                 "folder": str(inv.get("folder_path") or "").strip("/"), "folder_created": False,
                 "web_view_link": inv.get("web_view_link"), "pages": pages, "size": len(data),
                 "mime_type": inv.get("mime_type") or mime}
    return 200, _saved_body(store, txn, file_info, reading, True)


def save_receipt(user, txn_id, parts, vendor_text, upload_id):
    """Save the receipt of one purchase from the dashboard. -> (HTTP status, body).

    parts: [(client file name, bytes)] in page order. vendor_text: the folder the person typed (or the
    suggestion the page showed); upload_id: one per receipt, so a retry after a lost response is
    recognised instead of saved twice. Everything that can refuse the request does so before any
    Drive call."""
    store = get_store()
    txn = store.get_transaction(txn_id)
    if not txn:
        return 404, {"error": "transaction not found"}
    receipt_access(user, txn)
    email = user["email"]
    if receipt_throttled(email):
        raise AuthError(429, "Too many receipts in ten minutes; wait a few minutes and try again.")
    if upload_id and not receipts.UPLOAD_ID_RE.fullmatch(upload_id):
        raise receipts.ReceiptError(400, "upload_id is malformed.")
    data, mime, ext, pages = receipts.prepare_upload(parts)
    drive = get_drive()

    status = txn.get("invoice_status")
    if txn.get("type") != "purchase":
        return 409, _not_saved(store, txn, "Only purchases need a receipt.")
    if status == "waived":
        return 409, _not_saved(store, txn, "This purchase is marked as having no invoice; undo that first.")
    if status == "matched":
        replay = _replay(store, drive, txn, email, upload_id, pages, data, mime)
        if replay:
            return replay
        inv = store.get_invoice(txn["invoice_id"]) if txn.get("invoice_id") else None
        return 409, _not_saved(store, txn, f"This purchase already has its invoice{_invoice_label(inv, txn)}; "
                                           "nothing was saved.")
    if status not in ("missing", "possible"):
        return 409, _not_saved(store, txn, "This purchase needs no invoice; nothing was saved.")

    year, month = int(txn["year"]), int(txn["month"])

    def place(month_id, month_created):
        name, vendor_id, _source = vendor_choice(drive, store, txn, month_id, vendor_text,
                                                 children=[] if month_created else None)
        created = vendor_id is None
        if created:
            vendor_id = drive.ensure_folder(month_id, name)
        taken = [] if created else [f.get("name") for f in drive.list_children(vendor_id)]
        file_name = receipts.receipt_file_name(txn.get("txn_date"), name, ext, taken)
        return month_id, name, vendor_id, created, file_name

    with _folder_lock:     # two uploads never make two "2026 11" or "Uber" folders
        month_id, vendor, vendor_id, folder_created, file_name = in_month_folder(drive, year, month, True, place)
    month_label = month_folder_label(month_id, year, month)

    # appProperties hold at most 124 bytes per key and value; an open-mode name can be long.
    by = email.encode("utf-8")[:110].decode("utf-8", "ignore")
    meta = drive.upload(vendor_id, file_name, data, mime,
                        description=receipts.drive_description(txn, email, now_iso()),
                        app_properties={"apTxn": txn_id, "apBy": by, "apUpload": upload_id or ""})
    record = {"id": meta["id"], "file_name": meta.get("name") or file_name, "mime_type": meta.get("mimeType") or mime,
              "web_view_link": meta.get("webViewLink") or "", "folder_path": f"/{month_label}/{vendor}",
              "month_folder": month_folder_name(year, month), "vendor_folder": vendor, "size": len(data)}
    try:
        store.upsert_invoices([invoice_index.upload_placeholder(record)])
    except StoreError:
        _trash_quietly(drive, meta["id"], "its invoice row could not be stored")
        raise

    rejected = list(txn.get("rejected_invoice_ids") or [])
    if status == "possible" and txn.get("invoice_id") and txn["invoice_id"] not in rejected:
        rejected.append(txn["invoice_id"])      # the person's receipt replaces the proposal
    patch = {"invoice_status": "matched", "invoice_id": meta["id"], "invoice_part": None, "match_confidence": 1.0,
             "match_method": "manual", "match_note": f"receipt uploaded by {email}",
             "rejected_invoice_ids": rejected, "updated_by": email, "updated_at": now_iso()}
    expect = {"invoice_status": status, "invoice_id": txn.get("invoice_id"), "invoice_part": txn.get("invoice_part"),
              "match_method": txn.get("match_method")}
    try:
        linked = store.update_transaction(txn_id, patch, expect=expect)
    except StoreError as exc:                   # the unique index (that id is linked elsewhere), or a timeout
        print(f"receipt: link of {meta['id']} to {txn_id} refused: {exc}")
        linked = None
    current = None
    if linked is None:
        # A timed-out PATCH may have landed: a link whose answer was lost counts as linked. If this
        # read fails too, the error goes out and nothing is trashed (the file may be linked).
        current = store.get_transaction(txn_id)
        if current and current.get("invoice_id") == meta["id"]:
            linked = current
    if linked is None:
        # The purchase changed while the file went up (a match job, a Link, a waive, or this same
        # receipt's first try): take the upload back so no stray copy joins the matching pool.
        _trash_quietly(drive, meta["id"], "the purchase changed while it was saving")
        store.update_invoices([meta["id"]], {"removed_at": now_iso(), "extraction_error": WITHDRAWN})
        replay = current and _replay(store, drive, current, email, upload_id, pages, data, mime)
        if replay:
            print(f"receipt: {email} {txn_id} retry of {upload_id} arrived mid-save; kept {current['invoice_id']}, "
                  f"trashed {meta['id']}")
            return replay
        status_now = (current or {}).get("invoice_status")
        label = STATUS_LABELS.get(status_now, status_now) if current else "removed"
        print(f"receipt: {email} {txn_id} withdrawn ({meta['id']}): the purchase is now {label}")
        return 409, {"status": "not_saved", "withdrawn": True,
                     "error": f"This purchase changed while your receipt was saving (now: {label}), so the receipt "
                              "was not kept. Reload to see it.",
                     "transaction": transaction_row(store, current) if current else None}

    reading = False
    if get_gemini() is not None:
        record.update({"modified_time": meta.get("modifiedTime"), "size": len(data), "_file": meta})
        with _reading_lock:
            _reading.add(meta["id"])
        try:
            _receipt_pool.submit(read_receipt, record, data, mime)
            reading = True
        except RuntimeError as exc:             # the pool is closing with the instance; the index reads it
            with _reading_lock:
                _reading.discard(meta["id"])
            print(f"receipt: background read of {meta['id']} not started: {exc}")
    print(f"receipt: {email} {txn_id} card {txn['card_last4']} -> {month_label}/{vendor}/{record['file_name']} "
          f"({pages} page(s), {len(data)} bytes{', new folder' if folder_created else ''})")
    file_info = {"id": meta["id"], "name": record["file_name"], "folder": f"{month_label}/{vendor}",
                 "folder_created": folder_created, "web_view_link": record["web_view_link"], "pages": pages,
                 "size": len(data), "mime_type": record["mime_type"]}
    return 201, _saved_body(store, linked, file_info, reading, False)


def read_receipt(record, data, mime):
    """The receipt pool: Gemini reads the receipt just uploaded, from the bytes in hand (no download).

    Writes only the invoice row, never the transaction, so it races with nothing. removed_at, and
    extraction_error when the read worked, are left out of the row, so a Remove made during the read
    stands with its marker. On any failure the index retries the file (modified_time unset, or the
    stored extraction_error)."""
    try:
        store = get_store()
        row = invoice_index.extract_invoice(get_gemini(), get_drive(), record, content=(data, mime))
        row.pop("removed_at", None)
        error = row.get("extraction_error")
        if error is None:
            row.pop("extraction_error", None)   # the placeholder's is None already
        elif str((store.get_invoice(record["id"]) or {}).get("extraction_error") or "").startswith("upload withdrawn"):
            row.pop("extraction_error")         # removed meanwhile: a read error must not clear the marker
        store.upsert_invoices([row])
        if matching.shared(row):
            matching.split_whole_links(store, row)      # the upload linked it whole; it is one of its charges
        print(f"receipt read {record['id']}: " + (f"error {error}" if error
              else f"{row.get('vendor')} {row.get('total')} {row.get('currency')} {row.get('invoice_date')}"))
    except Exception as exc:  # noqa: BLE001
        print(f"receipt read {record.get('id')} failed: {exc}")
    finally:
        with _reading_lock:
            _reading.discard(record.get("id"))


def remove_receipt(user, txn_id):
    """Remove…: unlink a receipt added from the dashboard and move its file to the Drive trash.
    -> (HTTP status, body). Not gated on RECEIPT_UPLOAD_CARDS, so an upload can still be taken back
    after a card is switched off. The id is not added to rejected_invoice_ids: the file is gone, and its
    row is marked (REMOVED) so neither the index nor the matcher takes it again, even if the trash fails."""
    store = get_store()
    txn = store.get_transaction(txn_id)
    if not txn:
        return 404, {"error": "transaction not found"}
    receipt_access(user, txn, need_enabled=False)
    invoice_id = txn.get("invoice_id")
    not_uploaded = "This invoice wasn't added with Add receipt…; use Unlink."
    if not (invoice_id and txn.get("match_method") == "manual"
            and str(txn.get("match_note") or "").startswith("receipt uploaded by ")):
        return 409, {"error": not_uploaded}
    drive = get_drive()
    try:
        meta = drive.get(invoice_id, fields="id,appProperties")
    except DriveError as exc:
        if not _drive_404(exc):
            raise
        meta = None                             # already gone from Drive
    if meta is not None and (meta.get("appProperties") or {}).get("apTxn") != txn_id:
        return 409, {"error": not_uploaded}
    # Out of the matching pool before the purchase lets go of it, so a match run in between cannot
    # link it again; the marker keeps the index from bringing it back if the trash fails. If the
    # unlink fails with an error, the marker stays: the file is still linked, and Remove… can be retried.
    prior = store.get_invoice(invoice_id) or {}
    store.update_invoices([invoice_id], {"removed_at": now_iso(), "extraction_error": REMOVED})
    patch = {"invoice_status": "missing", "invoice_id": None, "invoice_part": None, "match_confidence": None,
             "match_method": None, "match_note": None, "updated_by": user["email"], "updated_at": now_iso()}
    updated = store.update_transaction(txn_id, patch, expect={"invoice_id": invoice_id, "match_method": "manual"})
    if updated is None:
        store.update_invoices([invoice_id], {"removed_at": prior.get("removed_at"),
                                             "extraction_error": prior.get("extraction_error")})
        return 409, {"error": "This purchase changed meanwhile; reload."}
    trashed, error = _trash_quietly(drive, invoice_id, "Remove") if meta is not None else (True, "")
    print(f"receipt: {user['email']} removed {invoice_id} from {txn_id}"
          + ("" if trashed else f" (not trashed: {error})"))
    body = {"transaction": transaction_row(store, updated), "trashed": trashed}
    if not trashed:
        where = f"Invoice{prior['folder_path']}" if prior.get("folder_path") else "the Invoice folder"
        body["warning"] = (f"Unlinked, but the file could not be moved to the Drive trash: {error.rstrip('.')}. "
                           f"It won't be matched again; delete it from {where} by hand.")
    return 200, body


# ---------------------------------------------------------------------------
# Admin pages (/admin): shared helpers. The pure work (catalogue, proposals, flags) is in admin.py.
# ---------------------------------------------------------------------------
class AdminError(Exception):
    """A refusal from an admin route: its status and JSON body ({"error": ..., plus extra fields the
    page reads, such as "existing" or "clash"). A field error starts with the field's name and a colon,
    so the page shows it next to that field."""

    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.body = status, {"error": message, **extra}


SCHEMA_MISSING = ("the database is missing a table or column this needs: apply schema.sql with setup_db.py "
                  "(see /admin#settings)")
AUDIT_ENTITIES = ["card", "card_number", "cost_center", "use_case", "vendor", "vendor_folder", "vendor_pattern",
                  "vendor_coding", "person", "setting", "proposal"]                       # schema.sql's vocabulary
VENDOR_KINDS = ["supplier", "processor", "agent", "not_vendor"]
KIND_WORDS = {"processor": "a payment processor", "agent": "an agent", "not_vendor": "not a vendor"}
NO_ACTIVITY = {"transactions": 0, "recent": 0, "missing": 0, "first_month": None, "last_month": None,
               "missing_last_month": None, "missing_newest": 0, "missing_months": 0}


def json_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise AuthError(400, "the body must be a JSON object")
    return body


def actor():
    """Who is making this change, as the audit records it: the IAP e-mail, or the typed name."""
    return str((getattr(g, "user", None) or {}).get("email") or "unknown")[:200]


def jsonable(value):
    return None if value is None else json.loads(json.dumps(value, default=str))


def audit(entity, entity_id, action, before=None, after=None, note=None):
    """Best effort: a change whose audit row cannot be written is still made (and said in the log)."""
    try:
        get_store().insert_audit({"actor": actor(), "auth_mode": auth_mode(), "entity": entity,
                                  "entity_id": str(entity_id)[:200], "action": action,
                                  "before": jsonable(before), "after": jsonable(after), "note": note})
    except Exception as exc:  # noqa: BLE001
        print(f"audit: not recorded ({entity} {str(entity_id)[:80]} {action}): {exc}")


def plural(n, word, many=None):
    return f"{n} {word if n == 1 else (many or word + 's')}"


def text_field(body, field, limit, required=False):
    """A name or note typed by a person, spaces tidied; None when left empty (a 400 when required)."""
    value = admin.norm_space(body.get(field))
    if required and not 1 <= len(value) <= limit:
        raise AdminError(400, f"{field}: 1 to {limit} characters")
    if len(value) > limit:
        raise AdminError(400, f"{field}: at most {limit} characters")
    return value or None


def int_field(body, field, default=None):
    value = body.get(field)
    if value is None:
        return default
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,7}", value.strip()):
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > 1_000_000:
        raise AdminError(400, f"{field}: a whole number")
    return value


def bool_field(body, field):
    value = body.get(field)
    if not isinstance(value, bool):
        raise AdminError(400, f"{field}: true or false")
    return value


def uuid_or_404(value, what):
    if not UUID_RE.match(str(value or "")):
        raise AdminError(404, f"no such {what}")
    return str(value)


def fresh_requested():
    """?fresh=1: read the whole-table reads again (the page's Refresh)."""
    if request.args.get("fresh") == "1":
        admin.invalidate("transactions", "invoices", "registry")


def admin_registry(store, fresh=False):
    """The vendor registry (admin.RegistryIndex), or None while its tables are missing or unreadable."""
    try:
        return admin.registry(store, fresh)
    except StoreError as exc:
        print(f"vendor registry: not read ({exc})")
        return None


def need_registry(store):
    """The registry, read fresh, for a write that needs it; 502 while schema.sql is not applied. Read
    without admin_registry's catch-all: a timeout or a 5xx is not a missing table, so it reaches
    _store_error as a plain "database error" that a retry may clear, not "apply schema.sql"."""
    reg = admin.registry(store, fresh=True)     # None only when the tables are missing
    if reg is None:
        raise AdminError(502, SCHEMA_MISSING, schema_missing=True)
    return reg


def all_use_cases(store):
    """Every use case row (retired too, with "active"), or [] while the table is missing or unreadable."""
    try:
        return store.list_use_cases()
    except StoreError as exc:
        print(f"use cases: not read ({exc})")
        return []


def use_case_list(store, rows=None):
    """The active use cases as the pages show them, in the store's order (sort, then name): the dashboard
    keeps that order when it filters by group. Server code that checks "active" uses the full rows."""
    rows = all_use_cases(store) if rows is None else rows
    return [{k: u.get(k) for k in ("id", "name", "group_name", "description")} for u in rows if u.get("active")]


def coding_settings(store):
    try:
        return store.get_state("coding_settings") or {}
    except StoreError as exc:
        print(f"coding settings: not read ({exc})")
        return {}


def bump_registry(store):
    """Best effort: count registry changes in app_state (Settings shows the version), and drop the
    cached registry so the next read sees the change."""
    try:
        state = store.get_state("vendor_registry") or {}
        store.set_state("vendor_registry", {"version": int(state.get("version") or 0) + 1, "at": now_iso(),
                                            "by": actor()})
    except Exception as exc:  # noqa: BLE001
        print(f"vendor registry: version not counted ({exc})")
    admin.invalidate("registry")


def vendor_entries(store, reg):
    """Every vendor as GET /api/admin/registry lists it: the vendor row with its folders, patterns and
    coding rows (each naming its use case and group) and the purchases it resolves."""
    if reg is None:
        return []
    names = {u["id"]: u["name"] for u in all_use_cases(store)}
    try:
        resolves = admin.vendor_resolves(reg, admin.invoice_rows(store), admin.transaction_rows(store))
    except StoreError as exc:
        print(f"vendor registry: purchases not counted ({exc})")
        resolves = {}
    out = []
    for v in reg.nested():
        v["coding"] = [dict(c, use_case=names.get(c.get("use_case_id")),
                            group_label=admin.norm_space(c.get("group_name")) or "Any group") for c in v["coding"]]
        v["resolves"] = dict(resolves.get(v["id"]) or {"purchases": 0, "uncoded": 0, "missing": 0})
        out.append(v)
    return out


def vendor_payload(store, reg, vendor_id):
    """One vendor in the shape GET /api/admin/registry lists it; every vendor write answers this."""
    return next((v for v in vendor_entries(store, reg) if v["id"] == vendor_id), None)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.errorhandler(AuthError)
def _auth_error(exc):
    return jsonify({"error": exc.message}), exc.status


@app.errorhandler(AdminError)
def _admin_error(exc):
    return jsonify(exc.body), exc.status


@app.errorhandler(statements.StatementError)
def _statement_error(exc):
    return jsonify({"error": str(exc)}), 400


@app.errorhandler(receipts.ReceiptError)
def _receipt_error(exc):
    return jsonify({"error": exc.message}), exc.status


@app.errorhandler(StoreError)
def _store_error(exc):
    print(f"store error: {exc}")
    if exc.missing:             # schema.sql not applied yet: say what to do rather than the raw error
        return jsonify({"error": SCHEMA_MISSING, "schema_missing": True}), 502
    if exc.code == "23505":     # a unique index refused it: someone else saved the same thing first
        return jsonify({"error": "that already exists (someone may have just added it); reload"}), 409
    return jsonify({"error": f"database error: {exc}"}), 502


@app.errorhandler(DriveError)
def _drive_error(exc):
    print(f"drive error: {exc}")
    return jsonify({"error": f"Google Drive error: {exc}"}), 502


@app.errorhandler(GeminiError)
def _gemini_error(exc):
    print(f"gemini error: {exc}")
    return jsonify({"error": f"Gemini error: {exc}"}), 502


@app.errorhandler(413)
def _too_large(exc):
    """A body over the limit; the receipt route says it in the dialog's words, the rest as before."""
    if request.endpoint == "api_receipt_save":
        return jsonify({"error": f"The receipt is larger than {receipts.MAX_PDF_BYTES // (1024 * 1024)} MB."}), 413
    return _unexpected(exc)


@app.errorhandler(Exception)
def _unexpected(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": exc.description}), exc.code
        return exc
    print(f"unexpected error on {request.path}: {exc!r}")
    return jsonify({"error": f"unexpected error: {exc}"}), 500


@app.after_request
def _headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.path in ("/", "/index.html", "/admin", "/static/index.html", "/static/admin.html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    elif BANK_LOGO_PATH_RE.fullmatch(request.path) and response.status_code in (200, 304):
        # The bank logos change only with a deploy: the browser keeps them a day (private: behind IAP,
        # no shared cache). Opened on their own they are inert drawings, never a document that runs.
        response.headers["Cache-Control"] = "private, max-age=86400"
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return response


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/admin")
def admin_page():
    """The accounts-payable admin pages. No require_user: IAP guards the path like "/", the page holds
    no data, and every /api/admin call checks the AP role itself."""
    return send_from_directory(app.static_folder, "admin.html")


@app.route("/health")
@app.route("/healthz")
def healthz():
    """/health is the public one: Cloud Run's front end answers the exact path /healthz itself (404)."""
    public = {k: _last_job.get(k) for k in ("name", "started", "finished")}   # no job output: it holds card data
    body = {"service": SERVICE, "ok": True, "store": STORE_KIND, "last_job": public}
    if request.args.get("deep") == "1":
        # Reachability only (no data): can the service read its database and the Invoice folder?
        checks = {}
        try:
            checks["store"] = {"ok": True, "cost_centers": len(get_store().list_cost_centers())}
        except Exception as exc:  # noqa: BLE001
            checks["store"] = {"ok": False, "error": str(exc)[:160]}
        try:
            folder = get_drive().get(INVOICE_FOLDER_ID)
            checks["drive"] = {"ok": True, "folder": folder.get("name"), "identity": get_drive().identity}
        except Exception as exc:  # noqa: BLE001
            checks["drive"] = {"ok": False, "error": str(exc)[:160]}
        # Counts only: this answers without the password, and which cards exist is card data.
        checks["chat_webhooks"] = {"cards": sum(1 for k in CARD_WEBHOOKS if k != "default"),
                                   "default": "default" in CARD_WEBHOOKS}
        checks["auth"] = {"mode": auth_mode(), "audience": IAP_AUDIENCE}
        body["checks"] = checks
        body["ok"] = checks["store"]["ok"] and checks["drive"]["ok"]
    return jsonify(body)


@app.route("/api/config")
def api_config():
    return jsonify({"auth": auth_mode(), "auth_disabled": AUTH_DISABLED,
                    "dashboard_url": dashboard_url(), "login_url": LOGIN_URL,
                    "invoice_folder_url": folder_url(INVOICE_FOLDER_ID), "ai_enabled": not AI_DISABLED})


@app.route("/api/login", methods=["POST"])
def api_login():
    """Password mode: {"password": ...} sets the session cookie. Wrong passwords are counted per
    address and overall (429 once over the limit); nothing in the answer says which part failed."""
    if not PASSWORD_MODE:
        return jsonify({"ok": True, "auth": auth_mode()})
    require_page_header()
    ip = client_ip()
    if login_throttled(ip):
        print(f"auth: password attempts throttled for {ip}")
        raise AuthError(429, "too many wrong passwords; wait 15 minutes and try again")
    body = request.get_json(silent=True)
    given = body.get("password") if isinstance(body, dict) else None
    ok = isinstance(given, str) and password_matches(given)
    note_login(ip, ok)
    if not ok:
        print(f"auth: wrong password from {ip}")
        raise AuthError(401, "wrong password")
    response = jsonify({"ok": True, "auth": auth_mode()})
    response.set_cookie(SESSION_COOKIE, session_token(), max_age=SESSION_DAYS * 86400, httponly=True,
                        secure=bool(os.environ.get("K_SERVICE")), samesite="Lax", path="/")
    return response


@app.route("/api/logout", methods=["POST"])
def api_logout():
    require_page_header()
    response = jsonify({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.route("/api/me")
def api_me():
    user = require_user()
    cards = get_store().list_cards()
    return jsonify({**user, "cards_owned": sorted(cards_owned_by(user["email"], cards))})


@app.route("/api/months")
def api_months():
    require_user()
    store = get_store()
    # cost_centers: the active ones (retired ones stay on the purchases that carry them); use_cases: the
    # Use case box's list, [] while schema.sql is not applied.
    return jsonify({"months": month_list(store), "cards": card_views(store.list_cards()),
                    "cost_centers": store.list_cost_centers(), "people": store.list_people(),
                    "use_cases": use_case_list(store)})


def _year_month():
    try:
        year, month = int(request.args.get("year", "")), int(request.args.get("month", ""))
    except ValueError:
        raise AuthError(400, "year and month are required")  # reuse the JSON error path
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        raise AuthError(400, "year/month out of range")
    return year, month


@app.route("/api/transactions")
def api_transactions():
    user = require_user()
    year, month = _year_month()
    store = get_store()
    card = request.args.get("card") or None
    cards = store.list_cards()
    txns = store.list_transactions(year, month, card)
    owned = cards_owned_by(user["email"], cards)
    rows = with_invoices(store, txns)
    for row in rows:
        row["editable"] = user["role"] == "ap" or row["card_last4"] in owned
    # Each list read once: the suggestions below use the same reads (on Supabase every call is a request,
    # and a month holds hundreds of purchases).
    cost_centers = store.list_cost_centers()
    try:
        use_case_rows = store.list_use_cases()
    except StoreError as exc:
        print(f"use cases: not read ({exc})")
        use_case_rows = []
    suggestions = bool(coding_settings(store).get("suggestions"))
    if suggestions and use_case_rows:
        # A use case to offer on each purchase the person may code and nobody has, from the vendor
        # registry (AP's switch in /admin#settings). A suggestion only: nothing is written.
        try:
            registry = admin_registry(store)
            if registry is not None:
                active_cc = set(cost_centers)
                cards_by_last4 = {c["last4"]: c for c in cards}
                for row in rows:
                    if row["editable"] and row.get("type") == "purchase" and not str(row.get("usage") or "").strip():
                        s = admin.suggest_coding(row, cards_by_last4.get(row["card_last4"]), registry, use_case_rows,
                                                 active_cc)
                        if s:
                            row["suggestion"] = s
        except StoreError as exc:
            print(f"coding suggestions: none this time ({exc})")
    return jsonify({"year": year, "month": month, "transactions": rows,
                    "cards": card_views(cards), "cost_centers": cost_centers,
                    "use_cases": use_case_list(store, use_case_rows), "suggestions": suggestions,
                    "people": store.list_people(), "statements": store.list_statements(year, month),
                    "notifications": store.list_notifications(year, month)[:20]})


PERSON_NAME_MAX = 100


@app.route("/api/people", methods=["GET"])
def api_people():
    require_user()
    return jsonify({"people": get_store().list_people()})


@app.route("/api/people", methods=["POST"])
def api_people_create():
    """Add a person transactions can be assigned to. Anyone who can open the dashboard may; a name
    already there (case and extra spaces aside) gives that person back instead of a second one."""
    user = require_user()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "the body must be a JSON object"}), 400
    name = " ".join(str(body.get("name") or "").split())
    if not name:
        return jsonify({"error": "a name is required"}), 400
    if len(name) > PERSON_NAME_MAX:
        return jsonify({"error": f"the name is longer than {PERSON_NAME_MAX} characters"}), 400
    email = str(body.get("email") or "").strip().lower() or None
    if email and user["role"] != "ap":
        # The admin portal trusts a person's e-mail (People offers "Make owner" of the cards they hold),
        # so only accounts payable sets it. The dashboard's "New person…" sends a name alone.
        return jsonify({"error": "email: only accounts payable can set a person's e-mail"}), 403
    if email and not EMAIL_RE.match(email):
        return jsonify({"error": f"'{email[:80]}' is not an e-mail address"}), 400
    try:
        person, created = get_store().create_person(name, email, user["email"])
    except StoreError as exc:
        if exc.conflict:            # people_email_unique: an e-mail belongs to one person
            return jsonify({"error": "email: that e-mail already belongs to someone"}), 409
        raise
    if created:
        print(f"person {name!r} added by {user['email']}")
        audit("person", person["id"], "create", None, person)
    return jsonify({"person": person, "created": created}), 201 if created else 200


@app.route("/api/transactions/<txn_id>", methods=["GET"])
def api_transaction_get(txn_id):
    """One row as the table shows it (checks, invoice.reading): the page polls it while an uploaded
    receipt is being read."""
    user = require_user()
    store = get_store()
    txn = store.get_transaction(txn_id)
    if not txn:
        return jsonify({"error": "transaction not found"}), 404
    return jsonify({"transaction": transaction_row(store, txn, may_edit(user, txn["card_last4"]))})


@app.route("/api/transactions/<txn_id>", methods=["PATCH", "POST"])
def api_transaction_update(txn_id):
    user = require_user()
    store = get_store()
    txn = store.get_transaction(txn_id)
    if not txn:
        return jsonify({"error": "transaction not found"}), 404
    cards = store.list_cards()
    if not may_edit(user, txn["card_last4"], cards):
        return jsonify({"error": "you can only edit transactions of cards you own"}), 403
    card = next((c for c in cards if c["last4"] == txn["card_last4"]), None) or {}
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "the body must be a JSON object"}), 400
    patch = {}
    for field, limit in EDITABLE_TEXT.items():
        if field in body:
            value = str(body.get(field) or "").strip()
            patch[field] = value[:limit] or None
    # Coding: a value typed as a list entry spelled another way ("marketing") is saved in the list's
    # spelling; anything else is kept as typed (free text stays allowed).
    active_cc, listed, filled, hint = None, None, None, None
    if "cost_center" in body or "usage" in body:
        active_cc = store.list_cost_centers()
        if patch.get("cost_center"):
            patch["cost_center"] = admin.snap_name(patch["cost_center"], active_cc) or patch["cost_center"]
        if patch.get("usage"):
            listed = all_use_cases(store)
            snapped = admin.snap_use_case(patch["usage"], listed, card.get("group_name") or "")
            if snapped:
                patch["usage"] = snapped
        # The card's default cost centre (a salon's card is that salon) fills an empty one when the use
        # case is set. Only the purchase's own card counts, and a retired default fills nothing.
        default = card.get("default_cost_center")
        if (patch.get("usage") and "cost_center" not in body and txn.get("type") == "purchase"
                and not str(txn.get("cost_center") or "").strip() and default and default in active_cc):
            patch["cost_center"] = default
            filled = default
        # "Marketing" typed in the Cost centre box: a use case since Nov 2026, so the purchase still has
        # none. Nothing is moved; the person is told.
        value = patch.get("cost_center") if "cost_center" in body else None
        usage_after = patch["usage"] if "usage" in patch else txn.get("usage")
        if value and not str(usage_after or "").strip():
            listed = all_use_cases(store) if listed is None else listed
            if any(admin.name_key(u["name"]) == admin.name_key(value) for u in listed):
                hint = f"{value} is a use case now: put it in the Use case box"
    if "person_id" in body:
        person_id = str(body.get("person_id") or "").strip()
        if person_id and not store.get_person(person_id):
            return jsonify({"error": "that person does not exist; add them first"}), 400
        patch["person_id"] = person_id or None
    action = str(body.get("action") or "").strip().lower()
    status = txn.get("invoice_status")
    rejected = list(txn.get("rejected_invoice_ids") or [])
    expect = None
    if action == "waive":
        if status in ("missing", "possible"):
            patch.update({"invoice_status": "waived", "invoice_id": None, "invoice_part": None, "match_confidence": None,
                          "match_method": "manual", "match_note": "waived: " + (body.get("reason") or "no invoice available")[:200]})
    elif action == "unwaive":
        if status == "waived":
            patch.update({"invoice_status": "missing", "match_method": None, "match_note": None})
    elif action == "confirm":
        if status == "possible" and txn.get("invoice_id"):
            patch.update({"invoice_status": "matched", "match_confidence": 1.0, "match_method": "manual",
                          "match_note": "confirmed by " + user["email"]})
            # Only the proposal this request read: a match run may have released or moved it meanwhile,
            # and confirming then would leave the row matched without an invoice.
            expect = {"invoice_status": "possible", "invoice_id": txn["invoice_id"],
                      "invoice_part": txn.get("invoice_part")}
            proposed = store.get_invoice(txn["invoice_id"]) or {}
            if txn.get("invoice_part") is None and matching.shared(proposed):
                # Proposed whole before the order's charges were read: confirm the charge it is.
                part, note = link_part(store, txn, proposed)
                if part is False:
                    return jsonify({"error": note + "; use Rematch"}), 409
                patch.update({"invoice_part": part, "match_note": patch["match_note"] + f"; {note}"})
    elif action == "link":
        invoice_id = str(body.get("invoice_id") or "").strip()
        inv = store.get_invoice(invoice_id) if invoice_id else None
        if not inv:
            return jsonify({"error": "invoice not found"}), 404
        matching.refresh_card_aliases(store)
        if user["role"] != "ap" and matching.cards_differ(inv.get("card_last4"), txn["card_last4"]) \
                and not matching.shared(inv):
            return jsonify({"error": f"that invoice shows card ending {inv['card_last4']}; ask accounts payable to link it"}), 403
        part, note = link_part(store, txn, inv)
        if part is False:
            return jsonify({"error": note}), 409
        patch.update({"invoice_status": "matched", "invoice_id": invoice_id, "invoice_part": part,
                      "match_confidence": 1.0, "match_method": "manual",
                      "match_note": "linked by " + user["email"] + (f"; {note}" if note else ""),
                      "rejected_invoice_ids": [i for i in rejected if i != invoice_id]})
    elif action == "unlink":
        if txn.get("invoice_id") or status == "matched":
            # Remember the refusal so the matcher does not propose the same pair again: the invoice and
            # every document it came with (invoices paid together, its payment confirmation).
            for refused in [txn.get("invoice_id")] + list(txn.get("also_invoice_ids") or ()):
                if refused and refused not in rejected:
                    rejected.append(refused)
            patch.update({"invoice_status": "missing", "invoice_id": None, "invoice_part": None, "match_confidence": None,
                          "match_method": None, "match_note": None, "rejected_invoice_ids": rejected})
    elif action:
        return jsonify({"error": f"unknown action '{action}'"}), 400
    if not patch:
        return jsonify({"error": "nothing to update"}), 400
    patch.update({"updated_by": user["email"], "updated_at": now_iso()})
    try:
        if filled:
            # Fill only the empty cost centre this request read: one someone set meanwhile is kept.
            updated = store.update_transaction(txn_id, patch, expect=dict(expect or {}, cost_center=txn.get("cost_center")))
            if updated is None:
                # Usually the person's own Cost centre save landed first (two boxes, two requests).
                # The fill was never asked for, so it must not cost them the use case: save the rest
                # without it, under the request's own conditions, and keep the cost centre now there.
                filled = None
                patch.pop("cost_center", None)
                updated = store.update_transaction(txn_id, patch, expect=expect)
        else:
            updated = store.update_transaction(txn_id, patch, expect=expect)
    except StoreError as exc:
        if "already linked" in str(exc) or "23505" in str(exc) or "unique" in str(exc).lower():
            return jsonify({"error": "that invoice was linked to another transaction meanwhile; reload."}), 409
        raise
    if updated is None:
        return jsonify({"error": "This purchase changed meanwhile; reload."}), 409
    row = with_invoices(store, [updated])[0]
    row["editable"] = True  # may_edit passed above; the dashboard re-renders the row from this
    out = {"transaction": row}
    if filled:
        out["filled_cost_center"] = filled
    if hint:
        out["hint"] = hint
    return jsonify(out)


@app.route("/api/transactions/<txn_id>/rematch", methods=["POST"])
def api_transaction_rematch(txn_id):
    """Per-row Rematch: read new invoices for the purchase's months, then match it again, as a
    background job (202 with the job; 409 while another job runs). AP or the card's owner."""
    user = require_user()
    txn = get_store().get_transaction(txn_id)
    if not txn:
        return jsonify({"error": "transaction not found"}), 404
    if not may_edit(user, txn["card_last4"]):
        return jsonify({"error": "you can only rematch transactions of cards you own"}), 403
    why_not = matching.rematchable(txn)
    if why_not:
        return jsonify({"error": why_not}), 400
    status, payload = start_job("rematch", rematch_transaction, txn_id, user["email"])
    if status == 202:
        payload["job"] = job_view(_jobs[payload["job"]["id"]])
    return jsonify(payload), status


@app.route("/api/transactions/<txn_id>/receipt-target")
def api_receipt_target(txn_id):
    """Where Add receipt… would save this purchase's receipt: the month folder, the suggested vendor
    folder and the month's folders to pick from. Creates nothing."""
    user = require_user()
    txn = get_store().get_transaction(txn_id)
    if not txn:
        return jsonify({"error": "transaction not found"}), 404
    receipt_access(user, txn)
    return jsonify(receipt_target(txn))


@app.route("/api/transactions/<txn_id>/receipt", methods=["POST"])
def api_receipt_save(txn_id):
    """Add receipt…: multipart 'file' parts (one photo or PDF, or up to 10 photos of one receipt, in page
    order), optional vendor_folder and upload_id. 201 saved, 200 the same upload_id again, 409 not kept."""
    user = require_user()
    # A receipt is at most a 15 MB PDF (or photos that make one): a bigger body is refused from its
    # Content-Length, before it is spooled to /tmp (RAM on Cloud Run) or read into memory.
    request.max_content_length = RECEIPT_MAX_BODY
    parts = [(f.filename or "", f.read()) for f in request.files.getlist("file")]
    status, body = save_receipt(user, txn_id, parts, request.form.get("vendor_folder") or "",
                                request.form.get("upload_id") or "")
    return jsonify(body), status


@app.route("/api/transactions/<txn_id>/receipt", methods=["DELETE"])
def api_receipt_remove(txn_id):
    """Remove…: unlink a receipt added from the dashboard and move its file to the Drive trash."""
    user = require_user()
    status, body = remove_receipt(user, txn_id)
    return jsonify(body), status


@app.route("/receipts/<last4>/<year>/<month>")
def receipts_link(last4, year, month):
    """The Chat card's Add receipts button: on to that card's receipts list. A server path because a
    #fragment does not survive IAP's interactive sign-in. No require_user: IAP already guards the path
    and nothing but the redirect is returned."""
    if not (re.fullmatch(r"[0-9]{4}", last4) and re.fullmatch(r"[0-9]{4}", year) and 2000 <= int(year) <= 2100
            and re.fullmatch(r"[0-9]{1,2}", month) and 1 <= int(month) <= 12):
        abort(404)
    return redirect(f"/#/{year}/{int(month):02d}?card={last4}&view=receipts", 302)


ASK_SLOTS = 2
_ask_slots = threading.BoundedSemaphore(ASK_SLOTS)


@app.route("/api/ask", methods=["POST"])
def api_ask():
    """Voice mode: {"question": "..."} about the month the page shows (?year&month[&card]); answers in two
    or three sentences for the browser to read aloud. Read only, for anyone signed in (everyone may read
    every card). Gemini answers when it is on; otherwise, or when it fails, a summary of the month does."""
    user = require_user()
    year, month = _year_month()
    card = (request.args.get("card") or "").strip() or None
    if card and not (len(card) == 4 and card.isdigit()):
        return jsonify({"error": "card must be the card's last four digits"}), 400
    body = request.get_json(silent=True)
    question = body.get("question") if isinstance(body, dict) else None
    if not isinstance(question, str) or not question.strip():
        return jsonify({"error": "ask a question"}), 400
    question = question.strip()[:assistant.MAX_QUESTION]
    data = assistant.build_context(get_store(), year, month, card, job=job_view(), user=user)
    # At most ASK_SLOTS questions wait on Gemini at once, so voice never ties up the server's threads; the
    # rest get the plain summary straight away.
    if _ask_slots.acquire(blocking=False):
        try:
            result = assistant.answer(get_gemini(), question, data, model=GEMINI_EXTRACT_MODEL)
        finally:
            _ask_slots.release()
    else:
        result = {"answer": assistant.summary_answer(data), "source": "summary"}
    print(f"voice: {user['email']} asked about {year}-{month:02d}: {question[:80]!r} ({result['source']})")
    return jsonify(result)


@app.route("/api/invoices")
def api_invoices():
    require_user()
    year, month = _year_month()
    store = get_store()
    folders = [month_folder_name(y, m) for y, m in neighbouring_months(year, month)]
    everything = request.args.get("all") == "1"
    taken = store.used_invoice_parts()
    out = []
    for inv in store.list_invoices(month_folders=None if everything else folders):
        if inv.get("removed_at") and inv["id"] not in taken:
            continue
        row = {k: inv.get(k) for k in ("id", "file_name", "web_view_link", "month_folder", "vendor_folder", "vendor",
                                       "invoice_number", "invoice_date", "total", "currency", "card_last4",
                                       "summary", "is_invoice", "extraction_error", "document_type", "removed_at")}
        # An invoice that lists several card charges is linked once every charge has its purchase.
        row["linked"] = inv["id"] in taken and not matching.has_room(inv, taken)
        if matching.shared(inv):
            row["charges"] = len(matching.listed_charges(inv))
            row["charges_taken"] = sorted(p for p in taken.get(inv["id"], ()) if p != matching.WHOLE)
            row["charges_linked"] = len(row["charges_taken"])
            row["card_charges"] = [dict(c, index=n) for n, c in matching.listed_charges(inv)]
        out.append(row)
    return jsonify({"invoices": out, "month_folders": "all" if everything else folders})


@app.route("/api/cards", methods=["GET"])
def api_cards():
    require_user()
    store = get_store()
    return jsonify({"cards": card_views(store.list_cards(), store.invoice_card_numbers())})


@app.route("/api/cards", methods=["POST"])
def api_cards_update():
    """Save a card's settings (the Cards… dialog, /admin). Only the fields sent change. A card not on the
    dashboard is added only with create: true (statements add theirs on import), so a typo in last4
    cannot quietly make a new card."""
    user = require_user(ap_only=True)
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "the body must be a JSON object"}), 400
    last4 = statements.last4(body.get("last4"))
    if not last4:
        return jsonify({"error": "last4 must be four digits"}), 400
    store = get_store()
    cards = {c["last4"]: c for c in store.list_cards()}
    current = cards.get(last4) or {}
    create = body.get("create") is True
    if last4 not in cards and not create:
        return jsonify({"error": f"card {last4} is not on the dashboard; add it with create: true"}), 404
    if create and last4 in cards:
        return jsonify({"error": f"card {last4} already exists"}), 409
    if "expect_card_number" in body:
        # The portal's "Use as card number" says which number it showed: a number changed since then
        # (someone else saved one) is never replaced unseen.
        expected = re.sub(r"[^0-9]", "", str(body.get("expect_card_number") or ""))
        now_number = current.get("card_number") or ""
        if expected != now_number:
            return jsonify({"error": f"card_number: card {last4}'s number is now {now_number or 'empty'}, not what this "
                                     "page showed; reload and try again", "card_number": now_number}), 409
    row = {"last4": last4, "updated_at": now_iso()}
    for field in ("label", "holder_name", "chat_space"):
        if field in body:
            row[field] = " ".join((body.get(field) or "").split())[:200] or None
    if "group_name" in body:
        group = " ".join(str(body.get("group_name") or "").split())[:GROUP_MAX] or None
        # One spelling per group: 'hair' joins the cards already in 'Hair'.
        same = next((c["group_name"] for c in cards.values() if c["last4"] != last4 and c.get("group_name")
                     and group and c["group_name"].lower() == group.lower()), None)
        row["group_name"] = same or group
    if "name_on_card" in body:
        row["name_on_card"] = " ".join(str(body.get("name_on_card") or "").split())[:80] or None
    if "card_number" in body:
        number = re.sub(r"[^0-9]", "", str(body.get("card_number") or ""))
        if number and len(number) != 4:
            return jsonify({"error": "card_number: the last four digits printed on the card"}), 400
        if number and number in cards and number != last4:
            return jsonify({"error": f"card_number: {number} is a statement card of its own"}), 400
        other = next((c["last4"] for c in cards.values() if c["last4"] != last4 and c.get("card_number") == number), None) \
            if number else None
        if other:
            return jsonify({"error": f"card_number: {number} is already the number on card {other}"}), 400
        reissued = matching.card_aliases(list(cards)).get(number) if number else None
        if reissued and reissued != last4 and number in store.invoice_card_numbers():
            return jsonify({"error": f"card_number: receipts showing {number} are card {reissued}'s (its reissued number)"}), 400
        row["card_number"] = number if number and number != last4 else None
    if "primary_last4" in body:
        primary = statements.last4(body.get("primary_last4")) if body.get("primary_last4") else None
        if primary and (primary == last4 or primary not in cards):
            return jsonify({"error": "primary_last4: another card on the dashboard, or empty"}), 400
        if primary and (cards[primary].get("primary_last4") or any(c.get("primary_last4") == last4 for c in cards.values())):
            return jsonify({"error": "primary_last4: a supplementary card is linked under a primary card, one level"}), 400
        row["primary_last4"] = primary
    if "owner_email" in body:
        # Several owners (the card holder and someone who files their receipts) are kept as one
        # comma-separated, lower-case list.
        emails = list(dict.fromkeys(owner_emails(body.get("owner_email"))))
        bad = [e for e in emails if not EMAIL_RE.match(e)]
        if bad:
            return jsonify({"error": f"owner_email: '{bad[0][:80]}' is not an e-mail address"}), 400
        row["owner_email"] = ", ".join(emails)[:500] or None
    if "active" in body:
        row["active"] = bool(body.get("active"))
    active_cc = None
    if "default_cost_center" in body:
        # A salon's card is that salon: filled into its purchases' empty cost centre when their use case
        # is set. "" or null clears it.
        value = admin.norm_space(body.get("default_cost_center"))
        if value:
            active_cc = store.list_cost_centers()
            value = admin.snap_name(value, active_cc)
            if not value:
                return jsonify({"error": "default_cost_center: not an active cost centre"}), 400
        row["default_cost_center"] = value or None
    if "receipt_upload" in body:
        # The card's own receipt upload switch; null follows RECEIPT_UPLOAD_CARDS.
        if body["receipt_upload"] is not None and not isinstance(body["receipt_upload"], bool):
            return jsonify({"error": "receipt_upload: true, false or null"}), 400
        row["receipt_upload"] = body["receipt_upload"]
    # The card as issued (the face the Cards gallery draws). "" or null clears each.
    if "bank" in body:
        value = body["bank"]
        if value is not None and not isinstance(value, str):
            return jsonify({"error": "bank: " + ", ".join(BANKS) + ", or empty"}), 400
        value = (value or "").strip().lower()
        if value and value not in BANKS:
            return jsonify({"error": f"bank: '{value[:40]}' is not one of " + ", ".join(BANKS)}), 400
        row["bank"] = value or None
    if "network" in body:
        value = body["network"]
        if value is not None and not isinstance(value, str):
            return jsonify({"error": "network: " + ", ".join(CARD_NETWORKS) + ", or empty"}), 400
        value = (value or "").strip().lower()
        if value and value not in CARD_NETWORKS:
            return jsonify({"error": f"network: '{value[:40]}' is not one of " + ", ".join(CARD_NETWORKS)}), 400
        row["network"] = value or None
    if "product" in body:
        if body["product"] is not None and not isinstance(body["product"], str):
            return jsonify({"error": f"product: text, at most {CARD_PRODUCT_MAX} characters"}), 400
        row["product"] = admin.norm_space(body["product"])[:CARD_PRODUCT_MAX].strip() or None
    changed = "holder_name" in body and (row["holder_name"] or "") != (current.get("holder_name") or "")
    if changed:
        # A label still the default one follows the new holder.
        old_label = current.get("label") or default_card_label(last4, current.get("holder_name"))
        sent = row.get("label") if "label" in body else old_label
        if not sent or sent in (old_label, default_card_label(last4, current.get("holder_name"))) \
                and CARD_LABEL_RE.match(old_label or ""):
            row["label"] = default_card_label(last4, row["holder_name"])
    if create:
        if "label" not in body and not row.get("label"):
            row["label"] = default_card_label(last4, row.get("holder_name"))
        row.setdefault("active", True)
        if body.get("confirm") is not True:
            # A known card is never itself a reissue alias (matching.card_aliases skips known numbers), so
            # adding one can take receipt numbers from another card: said first, added on confirm.
            changes = admin.alias_changes(list(cards.values()), list(cards.values()) + [row], last4)
            if changes:
                return jsonify({"error": f"adding card {last4} changes which card receipts showing "
                                         + ", ".join(c["number"] for c in changes)
                                         + " belong to; send confirm: true to add it anyway",
                                "alias_changes": changes}), 409
    # Last, once nothing more can be refused: the holder as a person, the Person of the card's purchases
    # nobody assigned to someone else. A card saved with its holder not yet a person links them too.
    if changed or ("holder_name" in body and row["holder_name"] and not current.get("person_id")):
        if row["holder_name"]:
            person, _ = store.create_person(row["holder_name"], created_by=user["email"])
            row["person_id"] = person["id"]
        else:
            row["person_id"] = None
    store.upsert_cards([row])
    matching.forget_card_aliases()
    print(f"card {last4} {'added' if create else 'updated'} by {user['email']}")
    all_cards = store.list_cards()
    saved = next((c for c in all_cards if c["last4"] == last4), None) or dict(current, **row)
    before = None if create else {k: current.get(k) for k in CARD_FIELDS}
    after = {k: saved.get(k) for k in CARD_FIELDS}
    if create or {k: v for k, v in before.items() if k != "updated_at"} != {k: v for k, v in after.items() if k != "updated_at"}:
        audit("card", last4, "create" if create else "update", before, after)
    views = card_views(all_cards, store.invoice_card_numbers())
    out = {"cards": views, "people": store.list_people(),
           "card": next((v for v in views if v["last4"] == last4), None)}
    new_default = saved.get("default_cost_center")
    if new_default and new_default != current.get("default_cost_center"):
        # The purchases already coded without a cost centre, which the fill rule would have filled had
        # the default been there: the page offers to fill them (POST /api/admin/cards/<last4>/fill-default).
        try:
            if new_default in (active_cc if active_cc is not None else store.list_cost_centers()):
                out["fillable"] = sum(1 for t in admin.transaction_rows(store, fresh=True)
                                      if t.get("card_last4") == last4 and t.get("type") == "purchase"
                                      and str(t.get("usage") or "").strip() and not str(t.get("cost_center") or "").strip())
        except StoreError as exc:
            print(f"card {last4}: purchases to fill not counted ({exc})")
    return jsonify(out)


NO_GROUP = "-"                         # ?group=- : the cards in no group


def in_group(card, group):
    """Is the card (a cards row, or None) in the group asked for: '' any, NO_GROUP none, else that group
    ignoring case and spaces?"""
    if not group:
        return True
    mine = " ".join(str((card or {}).get("group_name") or "").split()).lower()
    return not mine if group == NO_GROUP else mine == " ".join(group.split()).lower()


def _text_cell(cell, value):
    """A spreadsheet cell holding text as text: a description such as '=SUM(...)' or '+1 800 ...' is never
    read as a formula by Excel, and control characters a note pasted from Word may hold (a vertical tab)
    are left out, as Excel files cannot hold them."""
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"


def missing_receipts_xlsx(store, group="", card=None, year=None, month=None):
    """(xlsx bytes, rows) of the purchases still missing their invoice: one month or every month, the
    cards of a group (or one card), with the card's group, number, primary card, holder and the person
    each purchase is for (the holder unless someone else was assigned). A second sheet totals them per
    group and currency: amounts in different currencies are never added together."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    cards = {c["last4"]: c for c in store.list_cards()}
    people = {p["id"]: p["name"] for p in store.list_people()}
    if year and month:
        months = [(int(year), int(month))]
    else:
        months = sorted({(int(r["year"]), int(r["month"])) for r in store.month_summary()})
    rows = []
    for y, m in months:
        for t in store.list_transactions(y, m, card, statuses=["missing"]):
            c = cards.get(t["card_last4"])
            if t.get("type") == "purchase" and in_group(c, group):
                rows.append((t, c or {}))
    rows.sort(key=lambda r: ((r[1].get("group_name") or "~").lower(), r[0]["card_last4"], str(r[0]["txn_date"]), r[0]["id"]))
    wb = Workbook()
    ws = wb.active
    ws.title = "Missing receipts"
    head = ["Group", "Card (account)", "Card number", "Linked under", "Card holder", "Person", "Date", "Description",
            "Amount", "Currency", "Foreign amount", "Foreign currency", "Cost centre", "Use case", "Note", "Dashboard"]
    ws.append(head)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    base = dashboard_url()
    totals, names = {}, {}
    for t, c in rows:
        primary = cards.get(c.get("primary_last4") or "") or {}
        person = people.get(t.get("person_id") or "") or people.get(c.get("person_id") or "") or c.get("holder_name") or ""
        link = f"{base}/#/{t['year']}/{int(t['month']):02d}?card={t['card_last4']}" if base else ""
        values = [c.get("group_name") or "", t["card_last4"], c.get("card_number") or "",
                  (f"{primary.get('last4', c.get('primary_last4'))} ({primary.get('holder_name')})" if primary.get("holder_name")
                   else c.get("primary_last4") or ""),
                  c.get("holder_name") or "", person, str(t["txn_date"])[:10], t.get("description") or "",
                  float(t["amount"]), t.get("currency") or "",
                  float(t["source_amount"]) if t.get("source_amount") is not None else None, t.get("source_currency") or "",
                  t.get("cost_center") or "", t.get("usage") or "", t.get("note") or "", link]
        ws.append([None] * len(values))
        for col, value in enumerate(values, start=1):
            _text_cell(ws.cell(row=ws.max_row, column=col), value)
        if link:
            ws.cell(row=ws.max_row, column=len(values)).hyperlink = link
        group_key = " ".join((c.get("group_name") or "").split()).lower()
        names.setdefault(group_key, c.get("group_name") or "(no group)")
        key = (group_key, t.get("currency") or "")
        n, total = totals.get(key, (0, 0.0))
        totals[key] = (n + 1, round(total + float(t["amount"]), 2))
    for col, width in zip("ABCDEFGHIJKLMNOP", (10, 14, 12, 18, 16, 16, 11, 40, 12, 9, 14, 10, 18, 24, 24, 40)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    summary = wb.create_sheet("Totals")
    summary.append(["Group", "Currency", "Purchases missing a receipt", "Amount"])
    for cell in summary[1]:
        cell.font = Font(bold=True)
    for (group_key, cur), (n, total) in sorted(totals.items()):
        summary.append([None] * 4)
        for col, value in enumerate([names[group_key], cur, n, total], start=1):
            _text_cell(summary.cell(row=summary.max_row, column=col), value)
    for col, width in zip("ABCD", (16, 10, 28, 14)):
        summary.column_dimensions[col].width = width
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), len(rows)


@app.route("/api/export/missing")
def api_export_missing():
    """The purchases still missing their invoice, as an Excel file: ?group=Hair (or - for the cards in no
    group), ?card=, and ?year=&month= for one month (else every month)."""
    require_user()
    store = get_store()
    group = " ".join(str(request.args.get("group") or "").split())[:GROUP_MAX]
    card = statements.last4(request.args.get("card")) if request.args.get("card") else None
    if request.args.get("card") and not card:
        return jsonify({"error": "card: the four digits of a card"}), 400
    year, month = request.args.get("year"), request.args.get("month")
    if (year or month) and not (re.fullmatch(r"20[0-9][0-9]", str(year or "")) and re.fullmatch(r"[0-9]{1,2}", str(month or ""))
                                and 1 <= int(month) <= 12):
        return jsonify({"error": "year and month: both, as numbers"}), 400
    data, n = missing_receipts_xlsx(store, group, card, year, month)
    name = "missing-receipts" + (f"-{re.sub(r'[^A-Za-z0-9]+', '-', group).strip('-') or 'no-group'}" if group else "") + \
        (f"-{card}" if card else "") + (f"-{int(year)}-{int(month):02d}" if year and month else "-all-months") + ".xlsx"
    resp = send_file(io.BytesIO(data), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=name)
    resp.headers["X-Rows"] = str(n)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/statements", methods=["GET"])
def api_statements():
    require_user()
    year, month = _year_month()
    return jsonify({"statements": get_store().list_statements(year, month)})


@app.route("/api/statements/<statement_id>", methods=["DELETE"])
def api_statement_delete(statement_id):
    """Remove a wrong upload. Lines someone already coded or decided on are kept (detached)."""
    user = require_user(ap_only=True)
    store = get_store()
    upload = store.get_statement(statement_id)
    result = store.delete_statement(statement_id)
    if not result.get("deleted"):
        return jsonify({"error": "upload not found"}), 404
    print(f"statement {statement_id} removed by {user['email']}: {result}")
    # An upload the Drive import made: its file is not read again (each new version of it would
    # otherwise bring the removed lines back). Any other imported file of the same months is read
    # again on the next run: lines it found already stored (under this upload) are gone now.
    months = set((upload or {}).get("months") or [])
    reread = []
    for source in store.list_statement_sources():
        if upload and upload.get("drive_file_id") and source["file_id"] == upload["drive_file_id"]:
            source.update(status="removed", checked_at=now_iso(),
                          error=f"Removed from the dashboard by {user['email']}; not read again. Copy the file in "
                                "Drive to have it read as a new one.")
            store.save_statement_source(source)
            result["file_excluded"] = source["name"]
        elif source.get("status") == "imported" and months & set(source.get("months") or []):
            store.save_statement_source(dict(source, modified_time=None))
            reread.append(source["name"])
    if reread:
        result["files_read_again"] = reread
    return jsonify({"status": "ok", **result})


@app.route("/api/bank-imports")
def api_bank_imports():
    """The Drive import (the Bank Transactions folder): its last check and the files it read, newest
    first. AP only; {"enabled": false} when BANK_FOLDER_ID is not set."""
    require_user(ap_only=True)
    if not BANK_FOLDER_ID:
        return jsonify({"enabled": False})
    store = get_store()
    state = store.get_state("bank_sync") or {}
    # Between months the run lets the lease go for a moment; it is still running while its state
    # says so (a run killed outright stops counting after an hour).
    checked = _when(state.get("checked_at"))
    running = lease_live(store.get_lease(LEASE_NAME), job="drive-sync") or (
        bool(state.get("running")) and checked is not None
        and (dt.datetime.now(dt.timezone.utc) - checked).total_seconds() < 3600)
    rows = sorted(store.list_statement_sources(), key=lambda r: str(r.get("checked_at") or ""), reverse=True)
    files = [{k: r.get(k) for k in ("name", "path", "status", "new_count", "transaction_count", "months", "error",
                                    "warnings", "web_link", "checked_at")} for r in rows[:50]]
    view = {k: state.get(k) for k in ("checked_at", "ok", "error", "folder_name", "files", "settling",
                                      "months_pending", "last_import", "finished_at")}
    view["running"] = running
    watch = (store.get_state(INVOICE_WATCH) or {}) if INVOICE_WATCH_ON else {}
    invoices = {"enabled": INVOICE_WATCH_ON, "checked_at": watch.get("checked_at"), "error": watch.get("error") or "",
                "folders_pending": watch.get("folders_pending") or [], "last": watch.get("last") or None}
    return jsonify({"enabled": True, "folder_url": folder_url(BANK_FOLDER_ID), "state": view, "files": files,
                    "invoices": invoices})


@app.route("/api/statements", methods=["POST"])
def api_statement_upload():
    user = require_user(ap_only=True)
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "attach the statement as the 'file' field"}), 400
    data = upload.read()
    if not data:
        return jsonify({"error": "the file is empty"}), 400
    sync = request.args.get("sync") == "1"
    notify = request.args.get("notify", "1") != "0"
    if request.args.get("background") == "1":
        # The dashboard's way: a PDF or image statement is read by Gemini, which can take a while.
        status, payload = start_job("upload", ingest_statement, upload.filename, data, upload.mimetype or "", user["email"])
        if status == 202:
            print(f"statement {upload.filename} by {user['email']}: reading in the background")
        return jsonify(payload), status
    if sync:
        status, payload = run_locked("statement", process_statement_sync, upload.filename, data,
                                     upload.mimetype or "", user["email"], notify)
        return jsonify(payload), status
    summary = ingest_statement(upload.filename, data, upload.mimetype or "", user["email"])
    print(f"statement {upload.filename} by {user['email']}: {summary['new']} new of {summary['transactions']}")
    return jsonify({"status": "ok", **summary})


@app.route("/api/index", methods=["POST"])
def api_index():
    require_user(ap_only=True)
    year, month = _year_month()
    limit = min(int(request.args.get("limit", INDEX_BATCH)), 100)
    status, payload = run_locked("index", index_month, year, month, limit)
    return jsonify(payload), status


@app.route("/api/match", methods=["POST"])
def api_match():
    require_user(ap_only=True)
    year, month = _year_month()
    status, payload = run_locked("match", match_month, year, month, request.args.get("card") or None)
    return jsonify(payload), status


@app.route("/api/notify", methods=["POST"])
def api_notify():
    user = require_user(ap_only=True)
    year, month = _year_month()
    dry = request.args.get("dry_run") == "1"
    status, payload = run_locked("notify", notify_month, year, month, request.args.get("card") or None, user["email"], dry)
    return jsonify(payload), status


@app.route("/api/jobs", methods=["POST"])
def api_job_start():
    """Start a long step in the background (kind=index|match|notify); the page polls /api/jobs/current."""
    user = require_user(ap_only=True)
    kind = (request.args.get("kind") or "").strip()
    year, month = _year_month()
    card = request.args.get("card") or None
    if kind == "index":
        try:
            limit = min(max(int(request.args.get("limit", JOB_INDEX_LIMIT)), 1), 100)
        except ValueError:
            raise AuthError(400, "limit must be a number")
        status, payload = start_job("index", index_until_done, year, month, limit)
    elif kind == "match":
        status, payload = start_job("match", match_month, year, month, card, log=_job_log)
    elif kind == "notify":
        status, payload = start_job("notify", notify_month, year, month, card, user["email"],
                                    request.args.get("dry_run") == "1", dashboard=dashboard_url())
    else:
        return jsonify({"error": "kind must be index, match or notify"}), 400
    if status == 202:
        _last_job["progress"].update({"year": year, "month": month, "card": card})
        payload["job"] = job_view(_jobs[payload["job"]["id"]])
    return jsonify(payload), status


@app.route("/api/jobs/current")
def api_job_current():
    """The job with ?id=..., else the newest one. 404 for an id this instance never saw (a restart)."""
    require_user()
    jid = request.args.get("id")
    if jid:
        job = _jobs.get(jid)
        if job is None:
            return jsonify({"error": "no such job; the server may have restarted while it was running"}), 404
        return jsonify(job_view(job))
    view = job_view()
    if not view["running"]:
        # A step of the Cloud Run jobs (the Drive import, the weekly reminder) holds the job lease:
        # the page waits on this cheap call rather than resending its request.
        lease = get_store().get_lease(LEASE_NAME)
        if lease_live(lease, holder_prefix="job:"):
            view["elsewhere"] = {"job": lease.get("job"), "since": lease.get("acquired_at")}
    return jsonify(view)


# ---------------------------------------------------------------------------
# Admin pages (/admin): AP only. Reads under require_admin(), writes under require_admin(write=True);
# every write is audited (best effort). Matching never reads anything written here in Round A.
# ---------------------------------------------------------------------------
@app.route("/api/admin/settings")
def api_admin_settings():
    """What the service runs with, read only, plus the one switch it has (coding suggestions)."""
    require_admin()
    store = get_store()
    cards = store.list_cards()
    out_schema, schema_error = {}, None
    try:
        out_schema = dict(store.schema_status())
    except StoreError as exc:
        print(f"settings: schema not checked ({exc})")
        schema_error = str(exc)[:300]
    split = store.get_state("coding_split") or None
    out_schema["coding_split"] = bool(split)
    coding = coding_settings(store)
    registry_state = store.get_state("vendor_registry") or {}
    body = {
        "auth_mode": auth_mode(), "store": STORE_KIND, "ai_enabled": not AI_DISABLED,
        "allowed_domains": sorted(ALLOWED_DOMAINS), "ap_emails": sorted(AP_EMAILS),
        "ap_emails_how": ("AP_EMAILS: main.py (default) and .github/workflows/deploy-ap-reconciliation.yml line 61; "
                          "change both and push. A console-only edit is undone by the next deploy."),
        "receipt_upload": {"env": sorted(RECEIPT_UPLOAD_CARDS), "all": bool(RECEIPT_UPLOAD_CARDS & {"all", "*"}),
                           "from_default": "RECEIPT_UPLOAD_CARDS" not in os.environ,
                           "per_card": {c["last4"]: bool(c["receipt_upload"]) for c in cards
                                        if c.get("receipt_upload") is not None}},
        "webhooks": {"cards": sorted(k for k in CARD_WEBHOOKS if k != "default"), "default": "default" in CARD_WEBHOOKS,
                     "secret": "ap-reconciliation-card-webhooks",
                     "how": ("Webhooks are set in Secret Manager; a new version reaches the service on its next "
                             "deploy or new instance.")},
        "alias_cache_seconds": matching.CARD_ALIASES_MAX_AGE,
        "schema": out_schema, "coding_split": split,
        "coding": {"suggestions": bool(coding.get("suggestions")), "by": coding.get("by"), "at": coding.get("at")},
        "registry_version": int(registry_state.get("version") or 0),
        "iap_note": "IAP's own access list is managed in the Cloud console; the service cannot read it.",
    }
    if schema_error:
        body["schema_error"] = schema_error
    return jsonify(body)


@app.route("/api/admin/settings/coding", methods=["PUT"])
def api_admin_settings_coding():
    """{"suggestions": bool}: offer card owners a use case from the vendor registry (off by default)."""
    user = require_admin(write=True)
    body = json_body()
    suggestions = bool_field(body, "suggestions")
    store = get_store()
    before = coding_settings(store) or None
    after = {"suggestions": suggestions, "by": user["email"], "at": now_iso()}
    store.set_state("coding_settings", after)
    audit("setting", "coding", "update", before, after)
    return jsonify({"coding": after})


# -- cards ----------------------------------------------------------------------------------
def last4_or_400(last4):
    if not re.fullmatch(r"\d{4}", str(last4 or "")):
        raise AdminError(400, "last4: four digits")
    return last4


def card_label(c):
    return c.get("label") or default_card_label(c["last4"], c.get("holder_name"))


def ignored_numbers(store):
    """{number: {"reason", "by", "at"}}: card numbers on receipts AP said to leave alone."""
    return dict((store.get_state("ignored_card_numbers") or {}).get("numbers") or {})


@app.route("/api/admin/cards")
def api_admin_cards():
    """Every card with what needs attention (flags), its activity, last change and whether it can be
    deleted, plus the card numbers receipts show that no card claims."""
    require_admin()
    fresh_requested()
    store = get_store()
    cards = store.list_cards()
    by_last4 = {c["last4"]: c for c in cards}
    views = card_views(cards, store.invoice_card_numbers())
    activity = admin.card_activity(store.month_summary(), dt.date.today())
    active_cc = store.list_cost_centers()
    uploads = store.list_statements()
    try:
        last_change, audit_available = {}, True
        for r in store.list_audit(entity="card", limit=200):        # newest first: the first per card wins
            last_change.setdefault(r["entity_id"], {"at": r["at"], "actor": r["actor"], "action": r["action"]})
    except StoreError as exc:
        print(f"admin cards: no history ({exc})")
        last_change, audit_available = {}, False
    try:
        notified = store.latest_notifications()
    except StoreError as exc:
        print(f"admin cards: no notifications ({exc})")
        notified = {}
    counts = {"all": len(views), **{f: 0 for f in admin.FLAGS}}
    rows = []
    for view in views:
        last4 = view["last4"]
        c = by_last4[last4]
        a = activity.get(last4) or dict(NO_ACTIVITY)
        default = c.get("default_cost_center") or ""
        primary_default = (by_last4.get(c.get("primary_last4") or "") or {}).get("default_cost_center") or None
        flags = admin.card_flags(view, a, c.get("updated_at"), active_cc, primary_default)
        for f in flags:
            counts[f] += 1
        # What stops a delete, worked out in bulk (the DELETE route checks again with card_usage).
        blockers = []
        if a["transactions"]:
            blockers.append(plural(a["transactions"], "transaction"))
        listing = [u for u in uploads if last4 in (u.get("cards") or ())]
        if listing:
            blockers.append(f"listed on upload {listing[0].get('file_name') or listing[0].get('id')}"
                            + (f" and {plural(len(listing) - 1, 'other')}" if len(listing) > 1 else ""))
        if view["supplementary"]:
            blockers.append("primary of " + ", ".join(view["supplementary"]))
        rows.append(dict(view, updated_at=c.get("updated_at"), receipt_upload_env=receipt_upload_env(last4),
                         activity=a, flags=flags, owners=admin.owner_list(c.get("owner_email"), ALLOWED_DOMAINS),
                         last_notification=notified.get(last4), last_change=last_change.get(last4),
                         default_cost_center_active=not default or default in active_cc,
                         primary_default=primary_default, deletable=not blockers, delete_blockers=blockers))
    return jsonify({"cards": rows, "counts": counts, "flag_labels": admin.FLAG_LABELS, "banks": bank_list(),
                    "networks": list(CARD_NETWORKS),
                    "groups": admin.card_groups(cards), "people": store.list_people(), "cost_centers": active_cc,
                    "owners": admin.owners_index(cards), "allowed_domains": sorted(ALLOWED_DOMAINS),
                    "unclaimed": admin.unclaimed_numbers(admin.invoice_rows(store), cards, ignored_numbers(store)),
                    "audit_available": audit_available})


@app.route("/api/admin/card-numbers/<number>")
def api_admin_card_number(number):
    """The receipts showing a number no card claims, and the cards with a purchase of their amount soon
    after (the likely owner)."""
    require_admin()
    if not re.fullmatch(r"\d{4}", number):
        raise AdminError(400, "number: four digits")
    fresh_requested()
    store = get_store()
    result = admin.likely_cards(number, admin.invoice_rows(store), admin.transaction_rows(store), store.list_cards())
    if not result["invoices"]:
        raise AdminError(404, f"no receipt shows card number {number}")
    result["ignored"] = ignored_numbers(store).get(number) or None
    return jsonify(result)


@app.route("/api/admin/card-numbers/<number>/ignore", methods=["POST"])
def api_admin_card_number_ignore(number):
    user = require_admin(write=True)
    if not re.fullmatch(r"\d{4}", number):
        raise AdminError(400, "number: four digits")
    body = json_body()
    reason = admin.norm_space(body.get("reason"))
    if not 1 <= len(reason) <= 200:
        raise AdminError(400, "reason: say why (1-200 characters)")
    store = get_store()
    state = store.get_state("ignored_card_numbers") or {}
    numbers = dict(state.get("numbers") or {})
    before = numbers.get(number)
    numbers[number] = {"reason": reason, "by": user["email"], "at": now_iso()}
    store.set_state("ignored_card_numbers", dict(state, numbers=numbers))
    audit("card_number", number, "ignore", before, numbers[number])
    return jsonify({"ignored": numbers[number]})


@app.route("/api/admin/card-numbers/<number>/ignore", methods=["DELETE"])
def api_admin_card_number_unignore(number):
    require_admin(write=True)
    if not re.fullmatch(r"\d{4}", number):
        raise AdminError(400, "number: four digits")
    store = get_store()
    state = store.get_state("ignored_card_numbers") or {}
    numbers = dict(state.get("numbers") or {})
    if number not in numbers:
        raise AdminError(404, "not ignored")
    before = numbers.pop(number)
    store.set_state("ignored_card_numbers", dict(state, numbers=numbers))
    audit("card_number", number, "unignore", before, None)
    return jsonify({"ok": True})


def card_in_use(last4, counts):
    return AdminError(409, f"card {last4} is used ({plural(counts.get('transactions', 0), 'transaction')}, "
                           f"{plural(counts.get('statements', 0), 'upload')}, "
                           f"{plural(counts.get('supplementary', 0), 'supplementary card')}); only an unused card "
                           "can be deleted", counts=counts)


def alias_moves(changes):
    """Where the receipt numbers would go, in words: "they would count as card 3922's"."""
    after = {c["after"] for c in changes}
    if len(after) == 1 and None not in after:
        return f"they would count as card {next(iter(after))}'s"
    return "; ".join(f"{c['number']} would count as " + (f"card {c['after']}'s" if c["after"] else "no card's")
                     for c in changes)


def deletable_card(store, last4):
    """(cards, the card's row, the receipt numbers its delete would move) when the card may be deleted;
    the refusal otherwise (404 unknown, 409 used, 409 moving receipt numbers without ?confirm=1)."""
    cards = store.list_cards()
    row = next((c for c in cards if c["last4"] == last4), None)
    if row is None:
        raise AdminError(404, f"card {last4} is not on the dashboard")
    counts = store.card_usage(last4)
    if any(counts.values()):
        raise card_in_use(last4, counts)
    changes = admin.alias_changes(cards, [c for c in cards if c["last4"] != last4], last4)
    if changes and request.args.get("confirm") != "1":
        raise AdminError(409, f"deleting card {last4} changes which card receipts showing "
                              f"{', '.join(c['number'] for c in changes)} belong to ({alias_moves(changes)})",
                         alias_changes=changes)
    return cards, row, changes


@app.route("/api/admin/cards/<last4>", methods=["DELETE"])
def api_admin_card_delete(last4):
    """Delete a card nothing uses: no transaction, no upload listing it, no supplementary card. A delete
    that would hand its receipt numbers to another card is said first (?confirm=1 goes ahead)."""
    require_admin(write=True)
    last4_or_400(last4)
    store = get_store()
    deletable_card(store, last4)
    # Checked again under the job lock and the job lease: an import that read the card list before the
    # delete would otherwise fail on the transactions' foreign key. A job of this instance holds the store
    # lease under this instance's own holder, which take_lease would hand back, so the lock comes first.
    if not _job_lock.acquire(blocking=False):
        raise AdminError(409, "an import or match is running; try again in a minute")
    try:
        lease = take_lease("card-delete")
        if lease is None:
            raise AdminError(409, "an import or match is running; try again in a minute")
        try:
            _cards, row, changes = deletable_card(store, last4)
            try:
                deleted = store.delete_card(last4)
            except StoreError as exc:
                if exc.conflict:            # a transaction arrived meanwhile (the foreign key)
                    raise card_in_use(last4, store.card_usage(last4)) from None
                raise
            if not deleted:
                raise AdminError(404, f"card {last4} is not on the dashboard")
        finally:
            lease.release()
    finally:
        _job_lock.release()
    matching.forget_card_aliases()
    note = ("alias changes confirmed: " + "; ".join(f"{c['number']} {c['before'] or '-'} -> {c['after'] or '-'}"
                                                    for c in changes)) if changes else None
    audit("card", last4, "delete", before=row, note=note)
    print(f"card {last4} deleted by {actor()}")
    return jsonify({"deleted": last4, "cards": card_views(store.list_cards(), store.invoice_card_numbers())})


@app.route("/api/admin/cards/<last4>/fill-default", methods=["POST"])
def api_admin_card_fill_default(last4):
    """Fill the card's default cost centre into its purchases coded (a use case) before the default was
    set, and still without a cost centre. A cost centre already set is never touched."""
    user = require_admin(write=True)
    last4_or_400(last4)
    store = get_store()
    card = next((c for c in store.list_cards() if c["last4"] == last4), None)
    if card is None:
        raise AdminError(404, f"card {last4} is not on the dashboard")
    default = card.get("default_cost_center")
    if not default or default not in store.list_cost_centers():
        raise AdminError(400, "default_cost_center: the card has no active default")
    rows = [t for t in admin.transaction_rows(store, fresh=True)
            if t.get("card_last4") == last4 and t.get("type") == "purchase"
            and str(t.get("usage") or "").strip() and not str(t.get("cost_center") or "").strip()]
    filled, stamp = [], now_iso()
    for t in rows:
        # Compare and set on the empty value read: a cost centre someone set meanwhile is kept.
        if store.update_transaction(t["id"], {"cost_center": default, "updated_by": user["email"], "updated_at": stamp},
                                    expect={"cost_center": t.get("cost_center")}) is not None:
            filled.append(t["id"])
    admin.invalidate("transactions")
    audit("card", last4, "fill_default", None, {"cost_center": default, "transactions": filled})
    return jsonify({"filled": len(filled), "cost_center": default})


# -- use cases and cost centres -------------------------------------------------------------
def rename_refused(old, uses, what):
    return AdminError(409, f"{plural(uses, 'purchase')} carry {old}; add the new {what} and retire this one instead. "
                           f"Purchases already coded {old} keep {old} (they still count as coded); new purchases get "
                           "the new name.", uses=uses)


@app.route("/api/admin/coding")
def api_admin_coding():
    """The use cases and cost centres (retired ones too) with how much each is used, and the values
    typed on purchases that are on neither list."""
    require_admin()
    store = get_store()
    cards = store.list_cards()
    cost_centers = store.list_cost_center_rows()
    schema_missing = []
    try:
        use_cases = store.list_use_cases()
    except StoreError as exc:
        if not exc.missing:
            raise
        use_cases = []
        schema_missing.append("use_cases")
    overview = admin.coding_overview(store.coding_values_in_use(), cost_centers, use_cases, cards)
    try:
        # From the probes, not the rows: list_cost_center_rows() reads a missing active column as active.
        status = store.schema_status()
        for name in ("cost_centers.active", "use_cases"):
            if status.get(name) is False and name not in schema_missing:
                schema_missing.append(name)
    except StoreError as exc:
        print(f"admin coding: schema not checked ({exc})")
    order = {"cost_centers.active": 0, "use_cases": 1}
    return jsonify({
        "cost_centers": [dict(r, uses=overview["cost_center_uses"].get(r["name"], 0),
                              card_defaults=overview["card_defaults"].get(r["name"], [])) for r in cost_centers],
        "use_cases": [dict(u, uses=overview["use_case_uses"].get(u["id"], 0)) for u in use_cases],
        "usage_not_listed": overview["usage_not_listed"], "cost_centers_not_listed": overview["cost_centers_not_listed"],
        "cost_centers_like_use_cases": overview["cost_centers_like_use_cases"],
        "groups": admin.card_groups(cards), "schema_missing": sorted(schema_missing, key=order.get)})


def cost_center_clash(rows, name, but=None):
    key = admin.name_key(name)
    return next((r for r in rows if r["name"] != but and admin.name_key(r["name"]) == key), None)


def cost_center_exists(row):
    return AdminError(409, f"a cost centre named {row['name']} already exists",
                      existing={"name": row["name"], "active": row.get("active", True) is not False})


@app.route("/api/admin/cost-centers", methods=["POST"])
def api_admin_cost_center_add():
    user = require_admin(write=True)
    body = json_body()
    name = text_field(body, "name", 100, required=True)
    description = text_field(body, "description", 300)
    store = get_store()
    rows = store.list_cost_center_rows()
    clash = cost_center_clash(rows, name)
    if clash:
        raise cost_center_exists(clash)
    sort = int_field(body, "sort", max((int(r.get("sort") or 0) for r in rows), default=0) + 10)
    try:
        row = store.add_cost_center({"name": name, "sort": sort, "description": description,
                                     "updated_by": user["email"], "updated_at": now_iso()})
    except StoreError as exc:
        clash = cost_center_clash(store.list_cost_center_rows(), name) if exc.conflict else None
        if clash:
            raise cost_center_exists(clash) from None
        raise
    audit("cost_center", row["name"], "create", None, row)
    return jsonify({"cost_center": row}), 201


@app.route("/api/admin/cost-centers", methods=["PATCH"])
def api_admin_cost_center_update():
    """?name=<the exact current name>: rename, describe, move, retire or restore a cost centre. A name
    purchases carry is not renamed (they would keep the old one): add the new name and retire the old."""
    user = require_admin(write=True)
    name = request.args.get("name") or ""
    body = json_body()
    store = get_store()
    rows = store.list_cost_center_rows()
    row = next((r for r in rows if r["name"] == name), None)
    if row is None:
        raise AdminError(404, f"no cost centre named {name}")
    patch = {}
    if "name" in body:
        new = text_field(body, "name", 100, required=True)
        if new != name:
            if admin.name_key(new) != admin.name_key(name):
                # A change of capitals or spaces is always allowed: every comparison ignores them.
                uses = admin.uses(name, store.coding_values_in_use()["cost_center"])
                if uses:
                    raise rename_refused(name, uses, "name")
            clash = cost_center_clash(rows, new, but=name)
            if clash:
                raise cost_center_exists(clash)
            patch["name"] = new
    if "description" in body:
        patch["description"] = text_field(body, "description", 300)
    if "sort" in body:
        patch["sort"] = int_field(body, "sort", row.get("sort"))
    if "active" in body:
        patch["active"] = bool_field(body, "active")
    if not patch:
        raise AdminError(400, "nothing to update")
    patch.update({"updated_by": user["email"], "updated_at": now_iso()})
    try:
        saved = store.update_cost_center(name, patch)
    except StoreError as exc:
        clash = cost_center_clash(store.list_cost_center_rows(), patch.get("name", name), but=name) if exc.conflict else None
        if clash:
            raise cost_center_exists(clash) from None
        raise
    if saved is None:
        raise AdminError(404, f"no cost centre named {name}")
    was, now_active = row.get("active", True) is not False, saved.get("active", True) is not False
    warnings = []
    if was and not now_active:
        warnings = [f"Card {c['last4']} ({card_label(c)}) defaults to {saved['name']}: its purchases are no longer "
                    "filled until it gets another default."
                    for c in store.list_cards() if c.get("default_cost_center") == saved["name"]]
    action = ("rename" if saved["name"] != name else "retire" if was and not now_active
              else "restore" if now_active and not was else "update")
    audit("cost_center", saved["name"], action, row, saved, note=f"renamed from {name}" if action == "rename" else None)
    return jsonify({"cost_center": saved, "warnings": warnings})


def new_order(given, current, field):
    """The full new order: the names (or ids) given, then any the page did not know of (added meanwhile)
    in their current order. An unknown or repeated entry is a 400."""
    if not isinstance(given, list) or not all(isinstance(v, str) for v in given):
        raise AdminError(400, f"{field}: the list in its new order")
    unknown = [v for v in given if v not in current]
    if unknown:
        raise AdminError(400, f"{field}: {unknown[0]} is not on the list")
    if len(set(given)) != len(given):
        raise AdminError(400, f"{field}: each entry once")
    return list(given) + [v for v in current if v not in given]


@app.route("/api/admin/cost-centers/order", methods=["POST"])
def api_admin_cost_center_order():
    user = require_admin(write=True)
    body = json_body()
    store = get_store()
    rows = store.list_cost_center_rows()
    before = [r["name"] for r in rows]
    names = new_order(body.get("names"), before, "names")
    sorts, stamp = {r["name"]: r.get("sort") for r in rows}, now_iso()
    for i, name in enumerate(names):
        if sorts.get(name) != (i + 1) * 10:
            store.update_cost_center(name, {"sort": (i + 1) * 10, "updated_by": user["email"], "updated_at": stamp})
    audit("cost_center", "*", "reorder", before, names)
    return jsonify({"cost_centers": store.list_cost_center_rows()})


def use_case_group(body, store):
    """group_name: empty for any card, else one of the card groups (in its spelling)."""
    group = admin.norm_space(body.get("group_name"))
    if not group:
        return None
    snapped = admin.snap_name(group, admin.card_groups(store.list_cards()))
    if not snapped:
        raise AdminError(400, f"group_name: no card is in a group named '{group}'")
    return snapped


def use_case_clash(rows, name, but=None):
    key = admin.name_key(name)
    return next((u for u in rows if u["id"] != but and admin.name_key(u["name"]) == key), None)


def use_case_exists(row):
    return AdminError(409, f"a use case named {row['name']} already exists"
                           + ("" if row.get("active") else " (retired: restore it)"),
                      existing={"id": row["id"], "name": row["name"], "active": bool(row.get("active"))})


@app.route("/api/admin/use-cases", methods=["POST"])
def api_admin_use_case_add():
    user = require_admin(write=True)
    body = json_body()
    name = text_field(body, "name", 80, required=True)
    description = text_field(body, "description", 300)
    store = get_store()
    rows = store.list_use_cases()
    group = use_case_group(body, store)
    clash = use_case_clash(rows, name)
    if clash:
        raise use_case_exists(clash)
    # Before "Other" (999) by default: a plain max + 10 would put a new use case after it.
    sort = int_field(body, "sort", admin.new_use_case_sort(rows))
    try:
        row = store.create_use_case({"name": name, "description": description, "group_name": group, "sort": sort,
                                     "active": True, "created_by": user["email"], "created_at": now_iso()})
    except StoreError as exc:
        clash = use_case_clash(store.list_use_cases(), name) if exc.conflict else None
        if clash:
            raise use_case_exists(clash) from None
        raise
    audit("use_case", row["id"], "create", None, row)
    return jsonify({"use_case": row}), 201


@app.route("/api/admin/use-cases/<use_case_id>", methods=["PATCH"])
def api_admin_use_case_update(use_case_id):
    user = require_admin(write=True)
    uuid_or_404(use_case_id, "use case")
    body = json_body()
    store = get_store()
    rows = store.list_use_cases()
    row = next((u for u in rows if u["id"] == use_case_id), None)
    if row is None:
        raise AdminError(404, "no such use case")
    patch = {}
    if "name" in body:
        new = text_field(body, "name", 80, required=True)
        if new != row["name"]:
            if admin.name_key(new) != admin.name_key(row["name"]):
                uses = admin.uses(row["name"], store.coding_values_in_use()["usage"])
                if uses:
                    raise rename_refused(row["name"], uses, "use case")
            clash = use_case_clash(rows, new, but=use_case_id)
            if clash:
                raise use_case_exists(clash)
            patch["name"] = new
    if "group_name" in body:
        patch["group_name"] = use_case_group(body, store)
    if "description" in body:
        patch["description"] = text_field(body, "description", 300)
    if "sort" in body:
        patch["sort"] = int_field(body, "sort", row.get("sort"))
    if "active" in body:
        patch["active"] = bool_field(body, "active")
    if not patch:
        raise AdminError(400, "nothing to update")
    patch.update({"updated_by": user["email"], "updated_at": now_iso()})
    try:
        saved = store.update_use_case(use_case_id, patch)
    except StoreError as exc:
        clash = use_case_clash(store.list_use_cases(), patch.get("name", row["name"]), but=use_case_id) if exc.conflict else None
        if clash:
            raise use_case_exists(clash) from None
        raise
    if saved is None:
        raise AdminError(404, "no such use case")
    was, now_active = bool(row.get("active")), bool(saved.get("active"))
    warnings = []
    if was and not now_active:
        reg = admin_registry(store)
        if reg is not None:
            # A vendor default naming a retired use case suggests nothing any more.
            for c in reg.coding:
                if c.get("use_case_id") == use_case_id and c.get("vendor_id") in reg.vendors:
                    group = admin.norm_space(c.get("group_name"))
                    warnings.append(f"Vendor {reg.vendors[c['vendor_id']]['name']} suggests it for "
                                    + (f"{group} cards" if group else "all cards"))
    action = ("rename" if saved["name"] != row["name"] else "retire" if was and not now_active
              else "restore" if now_active and not was else "update")
    audit("use_case", use_case_id, action, row, saved)
    return jsonify({"use_case": saved, "warnings": warnings})


@app.route("/api/admin/use-cases/order", methods=["POST"])
def api_admin_use_case_order():
    user = require_admin(write=True)
    body = json_body()
    store = get_store()
    rows = store.list_use_cases()
    before = [u["id"] for u in rows]
    ids = new_order(body.get("ids"), before, "ids")
    sorts, stamp = {u["id"]: u.get("sort") for u in rows}, now_iso()
    for i, use_case_id in enumerate(ids):
        if sorts.get(use_case_id) != (i + 1) * 10:
            store.update_use_case(use_case_id, {"sort": (i + 1) * 10, "updated_by": user["email"], "updated_at": stamp})
    names = {u["id"]: u["name"] for u in rows}
    audit("use_case", "*", "reorder", [names[i] for i in before], [names[i] for i in ids])
    return jsonify({"use_cases": store.list_use_cases()})


# -- vendors: the folder catalogue, the registry and proposals (matching is unchanged) --------
def vendor_or_404(reg, vendor_id):
    uuid_or_404(vendor_id, "vendor")
    vendor = reg.vendors.get(vendor_id)
    if vendor is None:
        raise AdminError(404, "no such vendor")
    return vendor


def brief(vendor):
    return {"id": vendor["id"], "name": vendor["name"]}


def clean_folders(values, reg, vendor_id=None):
    """[folder] cleaned and checked: 1-200 characters, each once, none another vendor holds (409). The
    folders this vendor already holds are left out."""
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise AdminError(400, "folders: a list of folder names")
    out, seen = [], set()
    for value in values:
        folder = admin.norm_space(value)
        if not 1 <= len(folder) <= 200:
            raise AdminError(400, "folders: each folder name is 1 to 200 characters")
        key = admin.folder_key(folder)
        if key in seen:
            continue
        seen.add(key)
        owner = reg.folder_owner(key)
        if owner is not None and owner["id"] == vendor_id:
            continue
        if owner is not None:
            raise AdminError(409, f"folder '{folder}' already belongs to vendor {owner['name']}", clash="folder",
                             value=folder, existing=brief(owner))
        out.append(folder)
    return out


def clean_pattern(spec, reg, vendor_id=None, field="pattern"):
    """(kind, match_type, pattern) normalised and checked, or None when this vendor already holds it.
    A pattern another vendor holds is a 409."""
    if isinstance(spec, str):
        spec = {"pattern": spec}
    if not isinstance(spec, dict):
        raise AdminError(400, f"{field}: a pattern, or {{pattern, kind, match_type}}")
    kind = spec.get("kind") or "descriptor"
    # A supplier name on documents is matched whole: exact unless said otherwise.
    match_type = spec.get("match_type") or ("exact" if kind == "invoice_vendor" else "prefix")
    pattern = admin.normalise_pattern(spec.get("pattern"))
    problem = admin.pattern_problem(pattern, kind, match_type)
    if problem:
        raise AdminError(400, problem if field == "pattern" else f"{field}: {problem}")
    owner = reg.pattern_owner(kind, pattern)
    if owner is not None and owner["id"] == vendor_id:
        return None
    if owner is not None:
        raise AdminError(409, f"'{pattern}' already names vendor {owner['name']}", clash="pattern", value=pattern,
                         kind=kind, existing=brief(owner))
    return kind, match_type, pattern


def raced(store, vendor_id, what, value, kind=None):
    """A folder or pattern taken by another vendor between the check and the insert: 409 with the vendor as
    it was saved, and who holds the item now."""
    bump_registry(store)
    reg = admin_registry(store, fresh=True)
    owner = None
    if reg is not None:
        owner = reg.folder_owner(admin.folder_key(value)) if what == "folder" else reg.pattern_owner(kind, value)
    extra = {"clash": what, "value": value, "existing": brief(owner) if owner else None,
             "vendor": vendor_payload(store, reg, vendor_id)}
    if kind:
        extra["kind"] = kind
    return AdminError(409, f"{what} '{value}' was taken by " + (f"vendor {owner['name']}" if owner else "another vendor")
                      + " meanwhile; the rest was saved", **extra)


def registry_write_done(store, vendor_id):
    """After a registry write: count it, drop the cached registry, and answer the vendor as saved."""
    bump_registry(store)
    return vendor_payload(store, admin_registry(store, fresh=True), vendor_id)


@app.route("/api/admin/vendors")
def api_admin_vendors():
    """The vendor folders under Invoice/, one row each, a page at a time: how the purchases link to them
    and what needs attention. Read from cached whole-table reads (5 minutes; ?fresh=1 reads again)."""
    require_admin()
    fresh_requested()
    store = get_store()
    reg = admin_registry(store)
    rows = admin.catalogue(admin.invoice_rows(store), admin.transaction_rows(store), reg)
    sort = request.args.get("sort") if request.args.get("sort") in admin.CATALOGUE_SORTS else "attention"
    registered = request.args.get("registered") if request.args.get("registered") in ("all", "yes", "no") else "all"
    page = admin.catalogue_page(rows, q=request.args.get("q") or "", sort=sort, page=request.args.get("page") or 1,
                                per=request.args.get("per") or 50, registered=registered)
    stamps = [s for s in (admin.cached_at("transactions"), admin.cached_at("invoices")) if s]
    page.update(generated_at=min(stamps) if stamps else None, registry_available=reg is not None)
    return jsonify(page)


def current_proposals(store, reg, invoices=None, transactions=None):
    """(proposals, dismissed ids) computed now from the link history; none while the registry is missing."""
    dismissed = (store.get_state("vendor_proposals") or {}).get("dismissed") or {}
    if reg is None:
        return [], dismissed
    invoices = admin.invoice_rows(store) if invoices is None else invoices
    transactions = admin.transaction_rows(store) if transactions is None else transactions
    return admin.proposals(invoices, transactions, reg, all_use_cases(store), store.list_cards(), set(dismissed)), dismissed


@app.route("/api/admin/vendors/folder")
def api_admin_vendor_folder():
    """?name=<folder>: one folder's descriptors, spellings and unmatched documents (the name goes in the
    query string: folders hold commas and slashes)."""
    require_admin()
    name = request.args.get("name") or ""
    if not admin.norm_space(name):
        raise AdminError(400, "name: the folder's name")
    fresh_requested()
    store = get_store()
    reg = admin_registry(store)
    invoices, transactions = admin.invoice_rows(store), admin.transaction_rows(store)
    detail = admin.folder_detail(name, invoices, transactions, reg)
    if detail is None:
        raise AdminError(404, f"no documents in a folder named {admin.norm_space(name)}")
    key = detail["folder_key"]
    proposals, _ = current_proposals(store, reg, invoices, transactions)
    detail["proposals"] = [p for p in proposals
                           if admin.folder_key(p["evidence"].get("folder")) == key
                           or key in {admin.folder_key(f) for f in p["evidence"].get("folders") or ()}]
    return jsonify(detail)


@app.route("/api/admin/registry")
def api_admin_registry():
    require_admin()
    fresh_requested()
    store = get_store()
    reg = admin_registry(store)
    state = store.get_state("vendor_registry") or {}
    return jsonify({"available": reg is not None, "version": int(state.get("version") or 0),
                    "vendors": vendor_entries(store, reg), "use_cases": use_case_list(store),
                    "groups": admin.card_groups(store.list_cards()), "kinds": VENDOR_KINDS,
                    "folder_names": admin.folder_names(admin.invoice_rows(store))})


@app.route("/api/admin/vendors", methods=["POST"])
def api_admin_vendor_add():
    """Register a vendor with its folders and patterns in one go (the editor, or a proposal's Accept).
    Everything is checked before anything is written; patterns start "probation" (suggestions only)."""
    user = require_admin(write=True)
    body = json_body()
    store = get_store()
    reg = need_registry(store)
    name = text_field(body, "name", 100, required=True)
    note = text_field(body, "note", 500)
    kind = body.get("kind") or "supplier"
    if kind not in VENDOR_KINDS:
        raise AdminError(400, "kind: supplier, processor, agent or not_vendor")
    code_each = bool_field(body, "code_each") if "code_each" in body else False
    source = body.get("source") or "admin"
    if source not in ("admin", "proposal"):
        raise AdminError(400, "source: admin or proposal")
    clash = next((v for v in reg.vendors.values() if admin.name_key(v["name"]) == admin.name_key(name)), None)
    if clash:
        raise AdminError(409, f"a vendor named {clash['name']} already exists", clash="name", value=name,
                         existing=brief(clash))
    folders = clean_folders(body.get("folders") or [], reg)
    patterns_in = body.get("patterns") or []
    if not isinstance(patterns_in, list):
        raise AdminError(400, "patterns: a list of {pattern, kind, match_type}")
    patterns, seen = [], set()
    for spec in patterns_in:
        p = clean_pattern(spec, reg, field="patterns")
        if p and (p[0], p[2]) not in seen:
            seen.add((p[0], p[2]))
            patterns.append(p)
    stamp = now_iso()
    try:
        vendor = store.create_vendor({"name": name, "kind": kind, "code_each": code_each, "active": True, "note": note,
                                      "created_by": user["email"], "created_at": stamp})
    except StoreError as exc:
        if not exc.conflict:
            raise
        admin.invalidate("registry")
        reg = admin_registry(store, fresh=True)
        clash = next((v for v in (reg.vendors.values() if reg else ())
                      if admin.name_key(v["name"]) == admin.name_key(name)), None)
        raise AdminError(409, f"a vendor named {name} already exists", clash="name", value=name,
                         existing=brief(clash) if clash else None) from None
    what, value, kind_now = None, None, None
    try:
        for folder in folders:
            what, value, kind_now = "folder", folder, None
            store.add_vendor_folder({"vendor_id": vendor["id"], "folder": folder, "folder_key": admin.folder_key(folder),
                                     "created_by": user["email"], "created_at": stamp})
        for p_kind, match_type, pattern in patterns:
            what, value, kind_now = "pattern", pattern, p_kind
            store.add_vendor_pattern({"vendor_id": vendor["id"], "kind": p_kind, "match_type": match_type,
                                      "pattern": pattern, "status": "probation", "source": source,
                                      "created_by": user["email"], "created_at": stamp})
    except StoreError as exc:
        if not exc.conflict:
            bump_registry(store)            # the vendor is saved: the next read must see it
            raise
        error = raced(store, vendor["id"], what, value, kind_now)
        audit("vendor", vendor["id"], "create", None, error.body.get("vendor"), note=f"partly saved: {error.body['error']}")
        raise error from None
    nested = vendor_payload(store, admin_registry(store, fresh=True), vendor["id"])
    audit("vendor", vendor["id"], "create", None, nested)
    bump_registry(store)
    return jsonify({"vendor": nested}), 201


@app.route("/api/admin/vendors/<vendor_id>", methods=["PATCH"])
def api_admin_vendor_update(vendor_id):
    user = require_admin(write=True)
    uuid_or_404(vendor_id, "vendor")
    body = json_body()
    store = get_store()
    reg = need_registry(store)
    vendor = vendor_or_404(reg, vendor_id)
    patch = {}
    if "name" in body:
        name = text_field(body, "name", 100, required=True)
        clash = next((v for v in reg.vendors.values()
                      if v["id"] != vendor_id and admin.name_key(v["name"]) == admin.name_key(name)), None)
        if clash:
            raise AdminError(409, f"a vendor named {clash['name']} already exists", clash="name", value=name,
                             existing=brief(clash))
        patch["name"] = name
    if "kind" in body:
        if body["kind"] not in VENDOR_KINDS:
            raise AdminError(400, "kind: supplier, processor, agent or not_vendor")
        patch["kind"] = body["kind"]
    for field in ("code_each", "active"):
        if field in body:
            patch[field] = bool_field(body, field)
    if "note" in body:
        patch["note"] = text_field(body, "note", 500)
    if not patch:
        raise AdminError(400, "nothing to update")
    patch.update({"updated_by": user["email"], "updated_at": now_iso()})
    try:
        saved = store.update_vendor(vendor_id, patch)
    except StoreError as exc:
        if exc.conflict:
            raise AdminError(409, f"a vendor named {patch.get('name')} already exists", clash="name",
                             value=patch.get("name"), existing=None) from None
        raise
    if saved is None:
        raise AdminError(404, "no such vendor")
    # Default use cases that stop applying: a marketplace is coded purchase by purchase, and only a
    # supplier's purchases get a default.
    coded = [c for c in reg.coding if c["vendor_id"] == vendor_id]
    warnings = []
    was_codable = vendor.get("kind") == "supplier" and not vendor.get("code_each")
    if coded and was_codable and (saved.get("code_each") or saved.get("kind") != "supplier"):
        why = ("it is coded purchase by purchase" if saved.get("code_each")
               else f"it is {KIND_WORDS.get(saved.get('kind'), saved.get('kind'))}")
        warnings.append(f"{saved['name']}'s default use case{'s' if len(coded) != 1 else ''} "
                        f"({plural(len(coded), 'group')}) will be ignored: {why}.")
    nested = registry_write_done(store, vendor_id)
    audit("vendor", vendor_id, "update", {k: vendor.get(k) for k in VENDOR_FIELDS}, saved)
    return jsonify({"vendor": nested, "warnings": warnings})


@app.route("/api/admin/vendors/<vendor_id>/folders", methods=["POST"])
def api_admin_vendor_folders(vendor_id):
    """{"folder"} or {"folders": [...]}: more Drive folders holding this vendor's documents."""
    user = require_admin(write=True)
    uuid_or_404(vendor_id, "vendor")
    body = json_body()
    store = get_store()
    reg = need_registry(store)
    vendor_or_404(reg, vendor_id)
    values = body.get("folders") if "folders" in body else [body.get("folder")]
    if not values or not all(isinstance(v, str) for v in values):
        raise AdminError(400, "folders: a list of folder names")
    folders, stamp = clean_folders(values, reg, vendor_id), now_iso()
    for folder in folders:
        try:
            row = store.add_vendor_folder({"vendor_id": vendor_id, "folder": folder, "folder_key": admin.folder_key(folder),
                                           "created_by": user["email"], "created_at": stamp})
        except StoreError as exc:
            if not exc.conflict:
                bump_registry(store)
                raise
            raise raced(store, vendor_id, "folder", folder) from None
        audit("vendor_folder", row["id"], "create", None, row)
    nested = registry_write_done(store, vendor_id)
    return jsonify({"vendor": nested}), 201 if folders else 200


@app.route("/api/admin/vendor-folders/<folder_id>", methods=["DELETE"])
def api_admin_vendor_folder_delete(folder_id):
    require_admin(write=True)
    uuid_or_404(folder_id, "folder")
    store = get_store()
    reg = need_registry(store)
    row = next((f for f in reg.folders if f["id"] == folder_id), None)
    if row is None or not store.delete_vendor_folder(folder_id):
        raise AdminError(404, "no such folder")
    audit("vendor_folder", folder_id, "delete", row, None)
    return jsonify({"vendor": registry_write_done(store, row["vendor_id"])})


@app.route("/api/admin/vendors/<vendor_id>/patterns", methods=["POST"])
def api_admin_vendor_pattern_add(vendor_id):
    """{"pattern", "kind"?, "match_type"?, "source"?}: what names the vendor (a card descriptor's start,
    or the supplier name on its documents). Saved "probation": suggestions only, never matching."""
    user = require_admin(write=True)
    uuid_or_404(vendor_id, "vendor")
    body = json_body()
    store = get_store()
    reg = need_registry(store)
    vendor_or_404(reg, vendor_id)
    source = body.get("source") or "admin"
    if source not in ("admin", "proposal"):
        raise AdminError(400, "source: admin or proposal")
    p = clean_pattern(body, reg, vendor_id)
    if p is None:                   # this vendor's already
        return jsonify({"vendor": vendor_payload(store, reg, vendor_id)})
    kind, match_type, pattern = p
    try:
        row = store.add_vendor_pattern({"vendor_id": vendor_id, "kind": kind, "match_type": match_type,
                                        "pattern": pattern, "status": "probation", "source": source,
                                        "created_by": user["email"], "created_at": now_iso()})
    except StoreError as exc:
        if not exc.conflict:
            raise
        raise raced(store, vendor_id, "pattern", pattern, kind) from None
    audit("vendor_pattern", row["id"], "create", None, row)
    return jsonify({"vendor": registry_write_done(store, vendor_id)}), 201


@app.route("/api/admin/vendor-patterns/<pattern_id>", methods=["PATCH"])
def api_admin_vendor_pattern_update(pattern_id):
    """{"status"?: "probation"|"off", "note"?}. Round A takes no "trusted": patterns do not match yet."""
    user = require_admin(write=True)
    uuid_or_404(pattern_id, "pattern")
    body = json_body()
    store = get_store()
    reg = need_registry(store)
    row = next((p for p in reg.patterns if p["id"] == pattern_id), None)
    if row is None:
        raise AdminError(404, "no such pattern")
    patch = {}
    if "status" in body:
        if body["status"] not in ("probation", "off"):
            raise AdminError(400, "status: probation (on) or off; matching does not use patterns yet")
        patch["status"] = body["status"]
    if "note" in body:
        patch["note"] = text_field(body, "note", 500)
    if not patch:
        raise AdminError(400, "nothing to update")
    patch.update({"updated_by": user["email"], "updated_at": now_iso()})
    saved = store.update_vendor_pattern(pattern_id, patch)
    if saved is None:
        raise AdminError(404, "no such pattern")
    audit("vendor_pattern", pattern_id, "update", row, saved)
    return jsonify({"vendor": registry_write_done(store, row["vendor_id"])})


@app.route("/api/admin/vendor-patterns/<pattern_id>", methods=["DELETE"])
def api_admin_vendor_pattern_delete(pattern_id):
    require_admin(write=True)
    uuid_or_404(pattern_id, "pattern")
    store = get_store()
    reg = need_registry(store)
    row = next((p for p in reg.patterns if p["id"] == pattern_id), None)
    if row is None or not store.delete_vendor_pattern(pattern_id):
        raise AdminError(404, "no such pattern")
    audit("vendor_pattern", pattern_id, "delete", row, None)
    return jsonify({"vendor": registry_write_done(store, row["vendor_id"])})


@app.route("/api/admin/vendors/<vendor_id>/coding", methods=["PUT"])
def api_admin_vendor_coding(vendor_id):
    """{"group_name": "" | group, "use_case_id": id | null}: the use case suggested for the vendor's
    purchases on the group's cards ("" any card); null removes it."""
    user = require_admin(write=True)
    uuid_or_404(vendor_id, "vendor")
    body = json_body()
    store = get_store()
    reg = need_registry(store)
    vendor = vendor_or_404(reg, vendor_id)
    if "use_case_id" not in body:
        raise AdminError(400, "use_case_id: a use case, or null to remove the default")
    use_case_id = body.get("use_case_id")
    if use_case_id is None:
        # Removing is always allowed. The checks below are about setting one: a default left behind
        # when the vendor became code-each or not a supplier, or when its group lost its last card,
        # must still be removable, or it lingers in the registry and comes back on its own later.
        group = admin.norm_space(body.get("group_name"))
    else:
        group = use_case_group(body, store) or ""
        if vendor.get("code_each"):
            raise AdminError(400, f"{vendor['name']} is coded purchase by purchase (code each), so it has no default")
        if vendor.get("kind") != "supplier":
            raise AdminError(400, f"{vendor['name']} is {KIND_WORDS.get(vendor.get('kind'), vendor.get('kind'))}, "
                                  "so it has no default")
    key = admin.name_key(group)
    before = next((c for c in reg.coding if c["vendor_id"] == vendor_id and admin.name_key(c.get("group_name")) == key), None)
    if use_case_id is None:
        if store.delete_vendor_coding(vendor_id, group):
            audit("vendor_coding", f"{vendor_id}:{key}", "delete", before, None)
    else:
        allowed = {u["id"] for u in admin.use_cases_for_group(all_use_cases(store), group)}
        if use_case_id not in allowed:
            raise AdminError(400, f"use_case_id: not an active use case for {group or 'any group'}")
        saved = store.set_vendor_coding(vendor_id, group, use_case_id, user["email"])
        audit("vendor_coding", f"{vendor_id}:{key}", "update", before, saved)
    return jsonify({"vendor": registry_write_done(store, vendor_id)})


@app.route("/api/admin/proposals")
def api_admin_proposals():
    """What the link history suggests registering (aliases, folders that are one vendor, coding), worked
    out on each read; each carries the request that accepts it."""
    require_admin()
    fresh_requested()
    store = get_store()
    reg = admin_registry(store)
    proposals, dismissed = current_proposals(store, reg)
    return jsonify({"proposals": proposals, "dismissed": len(dismissed), "available": reg is not None})


@app.route("/api/admin/proposals/dismiss", methods=["POST"])
def api_admin_proposal_dismiss():
    """{"id", "undo"?}: hide a proposal (or show it again)."""
    user = require_admin(write=True)
    body = json_body()
    proposal_id = str(body.get("id") or "").strip()
    if not 1 <= len(proposal_id) <= 500:
        raise AdminError(400, "id: the proposal's id")
    undo = body.get("undo") is True
    store = get_store()
    state = store.get_state("vendor_proposals") or {}
    dismissed = dict(state.get("dismissed") or {})
    if undo:
        dismissed.pop(proposal_id, None)
    else:
        dismissed[proposal_id] = {"by": user["email"], "at": now_iso()}
    store.set_state("vendor_proposals", dict(state, dismissed=dismissed))
    audit("proposal", proposal_id, "undismiss" if undo else "dismiss")
    return jsonify({"ok": True})


# -- people -----------------------------------------------------------------------------------
def likely_person(name, people):
    """The person a card holder probably is: the same name, else the same words (or the words of one
    inside the other's, at least two: "NEW FRONTIER GLOBAL LIMITED" is "New Frontier Global")."""
    same = person_named(people, name)
    if same:
        return same
    mine = set(admin.person_tokens(name))
    for p in people:
        theirs = set(admin.person_tokens(p.get("name")))
        small, big = sorted((mine, theirs), key=len)
        if mine and theirs and (mine == theirs or (len(small) >= 2 and small <= big)):
            return p
    return None


def duplicate_pairs(rows):
    by_id = {p["id"]: p for p in rows}
    return [[{"id": a, "name": by_id[a]["name"]}, {"id": b, "name": by_id[b]["name"]}]
            for a, b in admin.likely_duplicate_people(rows)]


def holder_change(card, person_id, holder):
    """The card row for a new holder name (rename or merge): the label follows when it is the default one."""
    change = {"last4": card["last4"], "person_id": person_id, "holder_name": holder, "updated_at": now_iso()}
    old_label = card.get("label") or default_card_label(card["last4"], card.get("holder_name"))
    if CARD_LABEL_RE.match(old_label) and old_label == default_card_label(card["last4"], card.get("holder_name")):
        change["label"] = default_card_label(card["last4"], holder)
    return change


def move_card_holder(store, card, person_id, holder, note):
    """Rewrite a card's holder (and default label), and audit it on the card, so its History shows it."""
    change = holder_change(card, person_id, holder)
    store.upsert_cards([change])
    fields = ("holder_name", "label", "person_id")
    audit("card", card["last4"], "update", {k: card.get(k) for k in fields}, {k: change.get(k, card.get(k)) for k in fields},
          note=note)


@app.route("/api/admin/people")
def api_admin_people():
    """Everyone purchases can be assigned to: the cards they hold and own, what they can code, and the
    card holders not linked to anyone yet."""
    require_admin()
    store = get_store()
    rows = store.list_people_rows()
    cards = store.list_cards()
    usage = store.people_usage()
    people = []
    for p in rows:
        email = (p.get("email") or "").lower()
        held = sorted(c["last4"] for c in cards if c.get("person_id") == p["id"])
        owned = sorted(c["last4"] for c in cards if email and email in owner_emails(c.get("owner_email")))
        people.append(dict(p, cards_held=held, cards_owned=owned,
                           # holds the card but cannot code its purchases (not one of its owners)
                           cannot_code=[last4 for last4 in held if last4 not in owned] if email else [],
                           purchases=usage.get(p["id"], 0)))
    unlinked = []
    for c in cards:
        if c.get("holder_name") and not c.get("person_id"):
            likely = likely_person(c["holder_name"], rows)
            unlinked.append({"last4": c["last4"], "holder_name": c["holder_name"],
                             "likely": {"id": likely["id"], "name": likely["name"]} if likely else None})
    return jsonify({"people": people, "holders_unlinked": unlinked, "duplicates": duplicate_pairs(rows)})


@app.route("/api/admin/people/<person_id>", methods=["PATCH"])
def api_admin_person_update(person_id):
    """{"name"?, "email"?}. A rename rewrites the holder (and a default label) on the person's cards; a
    name someone else has is a 409 that offers a merge."""
    user = require_admin(write=True)
    uuid_or_404(person_id, "person")
    body = json_body()
    store = get_store()
    rows = store.list_people_rows()
    person = next((p for p in rows if p["id"] == person_id), None)
    if person is None:
        raise AdminError(404, "no such person")
    patch = {}
    if "name" in body:
        new = text_field(body, "name", PERSON_NAME_MAX, required=True)
        other = next((p for p in rows if p["id"] != person_id and admin.name_key(p["name"]) == admin.name_key(new)), None)
        if other:
            raise AdminError(409, f"{other['name']} already exists; merge instead?",
                             merge_into={"id": other["id"], "name": other["name"]})
        if new != person["name"]:
            patch["name"] = new
    if "email" in body:
        # Lower case like owner_emails() (cards_owned compares with it); "" is no e-mail (NULL), so two
        # people without one never clash on people_email_unique.
        email = str(body.get("email") or "").strip().lower() or None
        if email and not EMAIL_RE.match(email):
            raise AdminError(400, f"email: '{email[:80]}' is not an e-mail address")
        other = next((p for p in rows if p["id"] != person_id and email and (p.get("email") or "").lower() == email), None)
        if other:
            raise AdminError(409, f"email: that e-mail already belongs to {other['name']}")
        if email != (person.get("email") or None):
            patch["email"] = email
    if not patch:
        return jsonify({"person": {k: person.get(k) for k in ("id", "name", "email")}, "cards_updated": []})
    patch.update({"updated_by": user["email"], "updated_at": now_iso()})
    saved = store.update_person(person_id, patch)
    if saved is None:
        raise AdminError(404, "no such person")
    updated = []
    if "name" in patch:
        for c in store.list_cards():
            if c.get("person_id") == person_id:
                move_card_holder(store, c, person_id, patch["name"], "via person rename")
                updated.append(c["last4"])
    audit("person", person_id, "rename" if "name" in patch else "update", person, dict(person, **patch))
    return jsonify({"person": saved, "cards_updated": updated})


@app.route("/api/admin/people/<person_id>/merge", methods=["POST"])
def api_admin_person_merge(person_id):
    """{"into": id}: one person in two names. Their purchases and cards move to `into`, then they are
    deleted; their e-mail goes along when `into` has none."""
    user = require_admin(write=True)
    uuid_or_404(person_id, "person")
    body = json_body()
    into = str(body.get("into") or "")
    if into == person_id:
        raise AdminError(400, "into: another person")
    uuid_or_404(into, "person to merge into")
    store = get_store()
    rows = {p["id"]: p for p in store.list_people_rows()}
    source, target = rows.get(person_id), rows.get(into)
    if source is None:
        raise AdminError(404, "no such person")
    if target is None:
        raise AdminError(404, "no such person to merge into")
    moved = store.reassign_person_transactions(person_id, into)
    cards_moved = []
    for c in store.list_cards():
        if c.get("person_id") == person_id:
            move_card_holder(store, c, into, target["name"], "via person merge")
            cards_moved.append(c["last4"])
    email = source.get("email")
    store.delete_person(person_id)
    after = {k: target.get(k) for k in ("id", "name", "email")}
    if email and not target.get("email"):
        # After the delete, so the unique e-mail index never sees it twice.
        after = store.update_person(into, {"email": email, "updated_by": user["email"], "updated_at": now_iso()}) or after
    audit("person", into, "merge", {"source": source, "target": target},
          {"target": after, "transactions": moved, "cards": cards_moved})
    return jsonify({"person": after, "moved": {"transactions": moved, "cards": cards_moved}})


@app.route("/api/admin/people/<person_id>", methods=["DELETE"])
def api_admin_person_delete(person_id):
    """A card holder is never deleted (the card would bring them back); a person with purchases only on
    ?confirm=1 (the purchases are unassigned)."""
    require_admin(write=True)
    uuid_or_404(person_id, "person")
    store = get_store()
    person = next((p for p in store.list_people_rows() if p["id"] == person_id), None)
    if person is None:
        raise AdminError(404, "no such person")
    held = sorted(c["last4"] for c in store.list_cards() if c.get("person_id") == person_id)
    counts = {"transactions": store.people_usage().get(person_id, 0), "cards": held}
    name = person["name"]
    if held:
        raise AdminError(409, f"{name} holds card{'s' if len(held) > 1 else ''} {', '.join(held)}: merge {name} into "
                              "the right person, or change the card's holder first", counts=counts, holds_cards=held)
    if counts["transactions"] and request.args.get("confirm") != "1":
        raise AdminError(409, f"{name} is assigned to {plural(counts['transactions'], 'purchase')}; deleting "
                              "unassigns them", counts=counts)
    if not store.delete_person(person_id):
        raise AdminError(404, "no such person")
    audit("person", person_id, "delete", person, None)
    return jsonify({"deleted": person_id, "counts": counts})


@app.route("/api/admin/people/link-holders", methods=["POST"])
def api_admin_link_holders():
    """Every card whose holder is not a person yet: the person of that name (made when there is none)."""
    user = require_admin(write=True)
    store = get_store()
    linked = []
    for c in store.list_cards():
        if not c.get("holder_name") or c.get("person_id"):
            continue
        person, created = store.create_person(c["holder_name"], created_by=user["email"])
        store.upsert_cards([{"last4": c["last4"], "person_id": person["id"], "updated_at": now_iso()}])
        if created:
            audit("person", person["id"], "create", None, person)
        audit("card", c["last4"], "link", {"person_id": None}, {"person_id": person["id"]})
        linked.append({"last4": c["last4"], "person": {"id": person["id"], "name": person["name"]}, "created": created})
    return jsonify({"linked": linked, "duplicates": duplicate_pairs(store.list_people_rows()),
                    "people": store.list_people()})


# -- history ----------------------------------------------------------------------------------
@app.route("/api/admin/audit")
def api_admin_audit():
    require_admin()
    try:
        limit = max(1, min(200, int(request.args.get("limit") or 50)))
    except ValueError:
        raise AdminError(400, "limit: a whole number") from None
    before_id = request.args.get("before_id") or None
    if before_id is not None:
        if not re.fullmatch(r"\d{1,18}", before_id):
            raise AdminError(400, "before_id: a whole number")
        before_id = int(before_id)
    entity = (request.args.get("entity") or "").strip() or None
    entity_id = (request.args.get("entity_id") or "").strip() or None
    who = (request.args.get("actor") or "").strip() or None
    try:
        entries = get_store().list_audit(entity=entity, entity_id=entity_id, actor=who, before_id=before_id, limit=limit)
    except StoreError as exc:
        if not exc.missing:
            raise
        return jsonify({"entries": [], "next_before_id": None, "available": False, "entities": AUDIT_ENTITIES})
    return jsonify({"entries": entries, "next_before_id": entries[-1]["id"] if len(entries) == limit else None,
                    "available": True, "entities": AUDIT_ENTITIES})


def remind_latest():
    """Re-index, re-match and re-notify the newest month per card that still has missing invoices.

    This is what the weekly Cloud Run job runs (`python main.py remind`, started by Cloud Scheduler).
    It waits up to 25 minutes for a step running elsewhere (the dashboard, the Drive import).
    """
    lease = take_lease("remind", wait=1500, fail_open=False)
    if lease is None:
        raise SystemExit("remind: another job held the job lease for 25 minutes; not run")
    try:
        return _remind_latest()
    finally:
        lease.release()


def _remind_latest():
    store = get_store()
    latest = {}
    for row in store.month_summary():          # newest month first, so the first hit per card wins
        key = (int(row["year"]), int(row["month"]))
        if int(row["missing"]) + int(row["possible"]) > 0:
            latest.setdefault(row["card_last4"], key)
    months = sorted(set(latest.values()), reverse=True)
    out = []
    for year, month in months:
        for _ in range(MAX_INDEX_ROUNDS):
            if not index_month(year, month).get("remaining"):
                break
        match_month(year, month)
        cards = [c for c, ym in latest.items() if ym == (year, month)]
        for card in cards:
            out.append(notify_month(year, month, card, sent_by="scheduler"))
    return {"reminded": out}


# ---------------------------------------------------------------------------
# The Bank Transactions folder: statements put there are imported on their own
# ---------------------------------------------------------------------------
GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BANK_EXTENSIONS = {"xlsx", "xlsm", "xls", "csv", "tsv", "txt", "docx", "pdf", "jpg", "jpeg", "png"}
BANK_MAX_BYTES = 25 * 1024 * 1024
BANK_MAX_TRIES = 3          # a file that fails for a reason other than its content (Drive, Gemini) is retried
BANK_RUN_BUDGET = 25 * 60   # a run starts no month after this long; the next run (15 minutes on) carries on
LEASE_GAP = 15              # seconds the import leaves the lease free between steps (LEASE_POLL is 5)
LEASE_HOLD = 60             # it lets the lease go after holding it this long, between two steps
# Files Drive knows by type even when their name has no extension.
BANK_MIME_EXT = {"application/pdf": "pdf", "text/csv": "csv", "text/plain": "txt", "text/tab-separated-values": "tsv",
                 "image/jpeg": "jpg", "image/png": "png", XLSX: "xlsx", "application/vnd.ms-excel": "xls",
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx"}


def bank_reader(f):
    """How a file in the folder is read: (export mime type or None, file name for the parser), or
    None for a file that is not a statement (left alone). A Google Doc holds a bank's text export
    pasted in, so it is read as its plain text; a Google Sheet as .xlsx."""
    mime, name = f.get("mimeType", ""), f.get("name") or ""
    if mime == GOOGLE_DOC:
        return "text/plain", name + ".txt"
    if mime == GOOGLE_SHEET:
        return XLSX, name + ".xlsx"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in BANK_EXTENSIONS:
        return None, name
    return (None, f"{name}.{BANK_MIME_EXT[mime]}") if mime in BANK_MIME_EXT else None


def _when(value):
    """An RFC 3339 time from Drive or Postgres as an aware datetime; None when empty or unreadable."""
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


# Refusals an earlier version of the reader gave and this one no longer does: a file turned down with one
# of them is read again although it has not changed (the .xls exports, before .xls could be read).
OLDER_READER_REFUSALS = ("Legacy .xls is not supported",)


def refused_by_an_older_reader(seen):
    return seen.get("status") == "failed" and str(seen.get("error") or "").startswith(OLDER_READER_REFUSALS)


def bank_todo(files, ledger, now):
    """(files to read now, names still being edited). A file is read once per version: when it is
    new, when its modified time differs from the version read, or when the last try failed for a
    reason other than its content. A version younger than BANK_SETTLE waits, so a sheet someone is
    still filling in, or a file still uploading, is not read half-way."""
    todo, settling = [], []
    for f in files:
        if not bank_reader(f):
            continue
        seen = ledger.get(f["id"])
        modified = _when(f.get("modifiedTime"))
        if seen and seen.get("status") == "removed":
            continue                        # AP removed its upload from the dashboard: not read again
        if (seen and seen.get("status") != "retrying" and _when(seen.get("modified_time")) == modified
                and not refused_by_an_older_reader(seen)):
            continue
        if modified and (now - modified).total_seconds() < BANK_SETTLE:
            settling.append(f.get("name"))
            continue
        todo.append(f)
    todo.sort(key=lambda f: (f.get("path") or "", f.get("name") or ""))
    return todo, settling


def log_error(log, message):
    """A failure the job must not hide. Printed (the jobs log through print) it is one JSON line that Cloud
    Logging records at severity ERROR, so alerts and severity>=ERROR filters see it: a plain print lands at
    DEFAULT, and the run still ends with exit 0 (no retry against, say, a revoked token). Any other log
    (a test's, a caller's own) gets the text."""
    if log is print:
        print(json.dumps({"severity": "ERROR", "message": message}), flush=True)
    else:
        log(message)


def bank_folder_problem(exc):
    """Why the folder could not be listed, in words AP can act on."""
    text = str(exc)
    if re.search(r"-> 40[34]\b", text):
        return "ap@superhairpieces.com cannot open the folder: share it with ap@superhairpieces.com (Viewer)."
    return f"The folder could not be read: {text[:300]}"


LEDGER_KEPT = ("statement_id", "transaction_count", "new_count", "months", "transaction_ids", "warnings")


def import_bank_file(drive, f, seen, uploaded_by, log=print):
    """Read one file of the folder into the dashboard: (its statement_sources row, the ingest
    summary or None). A failed version keeps what the last version read left in the row (its
    line ids above all), so a later version can still say which lines it dropped."""
    export_as, name = bank_reader(f)
    row = {"file_id": f["id"], "name": f.get("name") or "", "path": f.get("path") or "",
           "mime_type": f.get("mimeType"), "web_link": f.get("webViewLink"), "status": "imported",
           "modified_time": f.get("modifiedTime"), "statement_id": None, "transaction_count": 0,
           "new_count": 0, "months": [], "transaction_ids": [], "attempts": 0, "error": None,
           "warnings": [], "checked_at": now_iso()}
    try:
        if int(f.get("size") or 0) > BANK_MAX_BYTES:
            raise statements.StatementError("The file is larger than 25 MB; split it and put the parts in the folder.")
        if export_as:
            data, mime = drive.export(f["id"], export_as), export_as
        else:
            data, mime = drive.download(f)
        summary = ingest_statement(name, data, mime, uploaded_by, source=f)
    except Exception as exc:  # noqa: BLE001
        row.update({k: (seen or {}).get(k, row[k]) for k in LEDGER_KEPT})
        if isinstance(exc, statements.StatementError):
            # The content itself: reading it again gives the same answer, so wait for a new version.
            row.update(status="failed", error=str(exc))
            log(f"sync: {row['path']}/{row['name']} could not be read: {exc}")
            return row, None
        tries = ((seen or {}).get("attempts") or 0) + 1 if (seen or {}).get("status") == "retrying" else 1
        if tries < BANK_MAX_TRIES:
            # Keep the version read before (if any), so the next run tries this one again.
            row.update(status="retrying", attempts=tries, error=str(exc)[:500],
                       modified_time=(seen or {}).get("modified_time"))
        else:
            row.update(status="failed", attempts=tries, error=f"{str(exc)[:450]} (tried {tries} times)")
        log(f"sync: {row['path']}/{row['name']} failed (try {tries}): {exc}")
        return row, None
    gone = len(set((seen or {}).get("transaction_ids") or []) - set(summary["transaction_ids"]))
    warnings = list(summary["warnings"])
    if gone:
        warnings.append(f"{gone} line{'s' if gone != 1 else ''} of the version read before "
                        f"{'are' if gone != 1 else 'is'} no longer in the file; the dashboard keeps "
                        f"{'them' if gone != 1 else 'it'}. If a line was corrected, check that card's month.")
    row.update(statement_id=summary["statement_id"] or (seen or {}).get("statement_id"),
               transaction_count=summary["transactions"], new_count=summary["new"],
               months=[month_folder_name(y, m) for y, m in summary["months"]],
               transaction_ids=summary["transaction_ids"], warnings=warnings)
    log(f"sync: {row['path']}/{row['name']}: {summary['transactions']} lines, {summary['new']} new")
    return row, summary


def queue_months(store, add=(), done=(), started=None):
    """Months still to match (app_state "bank_sync" months_pending), changed in place: the stored list
    plus `add`, less `done`. Two writers (an import run and the reread command) each keep the other's
    months. A month done is kept when it was queued again after `started` (when its match began): the
    match did not see what made it queued again (an order whose charges were read meanwhile).
    Returns the list."""
    state = store.get_state("bank_sync") or {}
    pending = {tuple(m) for m in state.get("months_pending") or []}
    queued_at = dict(state.get("months_queued_at") or {})
    stamp = now_iso()
    for m in add:
        pending.add(tuple(m))
        queued_at[month_folder_name(*m)] = stamp
    for m in done:
        key = month_folder_name(*m)
        if started is not None and (queued_at.get(key) or "") > started:
            continue
        pending.discard(tuple(m))
        queued_at.pop(key, None)
    months = sorted(pending, reverse=True)
    update_sync_state(store, months_pending=months,
                      months_queued_at={k: v for k, v in queued_at.items() if tuple(map(int, k.split())) in pending})
    return months


def update_sync_state(store, **changes):
    """Merge `changes` into app_state "bank_sync" as it stands now: a run only writes the keys it
    owns, so an overlapping run's progress is not put back to an older copy."""
    state = store.get_state("bank_sync") or {}
    state.update(changes)
    store.set_state("bank_sync", state)
    return state


# -- the Invoice folder watch ---------------------------------------------------------------
# An invoice filed under Invoice/YYYY MM/<vendor>/ (the e-mail filer copies attachments there, AP files
# by hand, a receipt is uploaded from the dashboard) is read and matched by the next import run, against
# the purchases still waiting for one: matching.match_new_invoices, which only adds or improves links.
# Drive's change feed says what moved since the last run. app_state "invoice_watch" keeps its page
# token, the month folders still to read, and to_match: {invoice id: tries} for invoices filed or read
# but not matched yet. Everything here runs under the job lease, so two runs never write it at once; a
# run that finds the lease taken leaves the token where it was and the next run reads the same changes.
INVOICE_WATCH = "invoice_watch"
INVOICE_CATCH_UP_MONTHS = 2     # the first run reads this month's folder and the one before
INVOICE_MAX_TRIES = 4           # runs an invoice waits in to_match (not read yet, read or judge failed)
INVOICE_JUDGE_PER_RUN = max(1, int(os.environ.get("INVOICE_JUDGE_PER_RUN", "15")))   # invoices judged per run


def update_watch_state(store, **changes):
    state = store.get_state(INVOICE_WATCH) or {}
    state.update(changes)
    store.set_state(INVOICE_WATCH, state)
    return state


def _drive_passing(exc):
    """A Drive error that says "later", not "no": a rate limit or a server error."""
    text = str(exc)
    return bool(re.search(r"-> (429|5\d\d)\b", text) or (re.search(r"-> 403\b", text) and "ateLimit" in text))


def invoice_month_of(drive, folder_id, cache, max_depth=8):
    """Where a folder sits under the Invoice folder: its month folder ("YYYY MM": the outermost folder
    on the way named like a month, as the index reads it), "" when it is under the Invoice folder but
    in no month folder, None when it is not under it (or ap@ cannot see the way up). A rate limit or
    server error is raised, so the run keeps its place in the feed. cache: id -> (name, parent id)."""
    names, fid = [], folder_id
    for _ in range(max_depth):
        if not fid:
            return None
        if fid == INVOICE_FOLDER_ID:
            for name in reversed(names):
                parsed = invoice_index.parse_month_folder(name)
                if parsed:
                    return parsed
            return ""
        if fid not in cache:
            try:
                got = drive.get(fid, fields="id,name,parents")
                cache[fid] = (got.get("name") or "", (got.get("parents") or [None])[0])
            except DriveError as exc:
                if _drive_passing(exc):
                    raise
                cache[fid] = ("", None)
        name, parent = cache[fid]
        names.append(name)
        fid = parent
    return None


def invoice_changes(drive, store, token, log=print):
    """(month folders under Invoice where a file was added, changed, renamed, moved in or out, trashed
    or removed since `token`; {id: modifiedTime} of the files now filed in a month folder; the next
    token)."""
    changes, next_token = drive.changes(token)
    cache, months, ids, filed, outside = {}, set(), [], {}, 0
    for change in changes:
        f = change.get("file") or {}
        fid = change.get("fileId") or f.get("id")
        if not fid or fid == INVOICE_FOLDER_ID:
            continue
        ids.append(fid)
        if change.get("removed") or f.get("trashed"):
            continue                    # its month, if it was an invoice, comes from the index below
        is_folder = f.get("mimeType") == FOLDER
        month = invoice_month_of(drive, fid if is_folder else (f.get("parents") or [None])[0], cache)
        if month:
            months.add(month)
            if not is_folder:
                filed[fid] = f.get("modifiedTime")
        elif month == "" and not is_folder:
            outside += 1
    if ids:
        # A file the index knows: its month folder is walked again, so one moved away or removed is
        # marked removed there.
        months |= {i["month_folder"] for i in store.list_invoices(ids=ids) if i.get("month_folder")}
    if outside:
        log(f"invoices: {outside} file(s) under Invoice but in no month folder; they are not read")
    return months, filed, next_token


def fresh_filings(store, filed, since):
    """The files of the feed worth matching ({id: modifiedTime} -> ids): not read yet, a new version
    of one read before (the index reads it again, and its read may fail), or read since the last run
    (by someone's Rematch, say). An invoice read before that, unchanged, has been through a match
    already; the feed lists it again when only its sharing or other metadata changed (a whole folder
    shared at once lists every file in it), and a move to another month comes back through the
    index's relabelled ids."""
    if not filed:
        return []
    known = {r["id"]: r for r in store.list_invoices(ids=list(filed))}
    cut = _when(since)
    out = []
    for fid, modified in filed.items():
        row = known.get(fid)
        read = _when((row or {}).get("extracted_at"))
        changed = row is not None and modified and _when(row.get("modified_time")) != _when(modified)
        if row is None or changed or cut is None or read is None or read >= cut:
            out.append(fid)
    return out


def watch_invoice_folder(store, drive, now, log=print):
    """Under the job lease: note in app_state "invoice_watch" what changed under the Invoice folder
    since the last run. The first run starts from now and queues this month's folder and the one
    before, for invoices filed before the watch began. Returns the state."""
    state = store.get_state(INVOICE_WATCH) or {}
    pending = set(state.get("folders_pending") or [])
    to_match = dict(state.get("to_match") or {})
    try:
        if not state.get("token"):
            token = drive.start_page_token()
            pending |= {month_folder_name(y, m) for y, m in
                        neighbouring_months(now.year, now.month, before=INVOICE_CATCH_UP_MONTHS - 1, after=0)}
        else:
            months, filed, token = invoice_changes(drive, store, state["token"], log)
            pending |= months
            for fid in fresh_filings(store, filed, state.get("checked_at")):
                to_match.setdefault(fid, 0)
    except DriveError as exc:
        log_error(log, f"invoices: the Invoice folder's changes could not be read: {exc}")
        if state.get("token") and not _drive_passing(exc) and re.search(r"-> 40[04]\b", str(exc)):
            return update_watch_state(store, token=None, error=str(exc)[:300])   # start again next run
        return update_watch_state(store, error=str(exc)[:300])
    return update_watch_state(store, token=token, folders_pending=sorted(pending, reverse=True),
                              to_match=to_match, checked_at=now_iso(), error="")


def read_invoice_folder(store, drive, gemini, folder, log=print):
    """One watch step: read the new or changed files of one month folder. Each round's invoices go to
    to_match as soon as they are stored, so a later failure does not leave them read but unmatched.
    Returns how many were read."""
    read = 0
    if gemini is None:
        log(f"invoices: AI_DISABLED=1, {folder} is not read")
    else:
        for _ in range(MAX_INDEX_ROUNDS):
            result = invoice_index.index_invoices(store, drive, gemini, INVOICE_FOLDER_ID, month_folders=[folder],
                                                  limit=INDEX_BATCH, workers=INDEX_WORKERS, log=log)
            ids = (result.get("read_ids") or []) + (result.get("relabelled_ids") or [])
            if ids:
                to_match = dict((store.get_state(INVOICE_WATCH) or {}).get("to_match") or {})
                for iid in ids:
                    to_match.setdefault(iid, 0)
                update_watch_state(store, to_match=to_match)
            read += len(result.get("read_ids") or [])
            if not result.get("remaining"):
                break
    state = store.get_state(INVOICE_WATCH) or {}
    failures = {k: v for k, v in (state.get("folder_failures") or {}).items() if k != folder}
    update_watch_state(store, folders_pending=[f for f in state.get("folders_pending") or [] if f != folder],
                       folder_failures=failures)
    return read


def watch_step_failed(store, folder, exc, log=print):
    """A watch step raised: noted, and the run goes on with the next step. A month folder that fails
    INVOICE_MAX_TRIES runs in a row is let go until something in it changes again."""
    state = store.get_state(INVOICE_WATCH) or {}
    failures = dict(state.get("folder_failures") or {})
    pending = list(state.get("folders_pending") or [])
    if folder is not None:
        failures[folder] = failures.get(folder, 0) + 1
        if failures[folder] >= INVOICE_MAX_TRIES:
            log(f"invoices: giving up on {folder} after {failures[folder]} failed runs: {exc}")
            pending = [f for f in pending if f != folder]
            failures.pop(folder)
    log(f"invoices: {folder or 'matching'} failed: {exc}")
    update_watch_state(store, folders_pending=pending, folder_failures=failures,
                       error=f"{folder or 'matching'}: {str(exc)[:280]}")


def _read_failed(row):
    error = str(row.get("extraction_error") or "")
    return bool(error) and not error.startswith(("unsupported", "upload withdrawn"))


def match_watched_invoices(store, gemini, log=print):
    """The watch's match step: match_new_invoices on to_match. An invoice not read yet, read with an
    error that may pass, or left unplaced by a failed judge call stays for another try (its month
    folder queued again when it was the read that failed), up to INVOICE_MAX_TRIES runs."""
    to_match = dict((store.get_state(INVOICE_WATCH) or {}).get("to_match") or {})
    if not to_match:
        return {"matched": 0, "possible": 0}
    rows = {r["id"]: r for r in store.list_invoices(ids=list(to_match))}
    try:
        counts = matching.match_new_invoices(store, gemini, list(to_match), use_ai=not AI_DISABLED, log=log,
                                             judge_limit=INVOICE_JUDGE_PER_RUN)
    except Exception as exc:  # noqa: BLE001
        # Every invoice waiting spends one of its tries, so a step that keeps failing ends.
        keep = {iid: tries + 1 for iid, tries in to_match.items() if tries + 1 < INVOICE_MAX_TRIES}
        update_watch_state(store, to_match=keep)
        watch_step_failed(store, None, exc, log)
        return {"matched": 0, "possible": 0}
    unjudged, keep, refolder = set(counts.get("unjudged") or []), {}, set()
    deferred = set(counts.get("deferred") or [])
    for iid, tries in to_match.items():
        row = rows.get(iid)
        if iid in deferred:
            keep[iid] = tries                   # its turn with the judge comes next run; not a failure
            continue
        if row is None:
            why = "not read yet"
        elif _read_failed(row) and row.get("month_folder"):
            why = f"could not be read: {str(row['extraction_error'])[:120]}"
            refolder.add(row["month_folder"])
        elif iid in unjudged:
            why = "the judge did not answer"
        else:
            continue
        if tries + 1 < INVOICE_MAX_TRIES:
            keep[iid] = tries + 1
        else:
            log(f"invoices: giving up on {(row or {}).get('file_name') or iid} after {tries + 1} runs ({why})")
            refolder.discard((row or {}).get("month_folder"))
    state = store.get_state(INVOICE_WATCH) or {}
    folders = sorted(set(state.get("folders_pending") or []) | refolder, reverse=True)
    update_watch_state(store, to_match=keep, folders_pending=folders)
    return counts


def sync_bank_folder(log=print, now=None):
    """Import what is new in the Bank Transactions folder, then read invoices for and match the
    months those statements touch: what the Cloud Run job ap-reconciliation-sync runs every 15
    minutes (`python main.py sync`). Nothing is posted to Chat; reminders stay with AP's
    "Send reminders" and the weekly job.

    Months still to match are kept in app_state "bank_sync" (with the last check, for the
    dashboard), so a run cut short, or one that stopped after BANK_RUN_BUDGET, leaves them to the
    next. Between months the job lease is let go for a moment, so a dashboard step or the weekly
    reminder waiting for it gets its turn; the run then stops and the next one carries on."""
    if not BANK_FOLDER_ID:
        return {"status": "off", "detail": "BANK_FOLDER_ID is not set"}
    store, drive = get_store(), get_drive()
    now = now or dt.datetime.now(dt.timezone.utc)
    started = time.time()
    check = {"checked_at": now_iso(), "folder_id": BANK_FOLDER_ID}
    files, folder = [], {}
    try:
        folder = drive.get(BANK_FOLDER_ID)
        files = drive.walk(BANK_FOLDER_ID)
        check.update(ok=True, error="", folder_name=folder.get("name") or "", files=len(files))
    except DriveError as exc:
        check.update(ok=False, error=bank_folder_problem(exc))
        log_error(log, f"sync: the Bank Transactions folder could not be read: {exc}")
    todo, settling = bank_todo(files, {r["file_id"]: r for r in store.list_statement_sources()}, now)
    check["settling"] = settling
    state = update_sync_state(store, **check)
    status = "ok" if check["ok"] else "unreadable"
    nothing = {"status": "nothing new" if check["ok"] else "unreadable", "files": len(files), "settling": settling}
    if not check["ok"]:
        nothing["error"] = check["error"]
    if not todo and not state.get("months_pending") and not INVOICE_WATCH_ON:
        return nothing
    lease = take_lease("drive-sync", fail_open=False)
    if lease is None:
        update_sync_state(store, busy=True)
        log("sync: another step holds the job lease; trying again on the next run")
        return {"status": "busy"}
    if INVOICE_WATCH_ON:
        try:
            watch = watch_invoice_folder(store, drive, now, log)
        except Exception:
            lease.release()
            raise
        if not todo and not state.get("months_pending") and not (watch.get("folders_pending") or watch.get("to_match")):
            lease.release()
            return nothing
    imported, failed, pending = [], [], set()
    held_since, steps = time.time(), 0

    def turn():
        """Between two steps (files, months): False when the run stops here, out of time or because
        a step that was waiting took the lease while this run let it go."""
        nonlocal lease, held_since
        if time.time() - started > BANK_RUN_BUDGET:
            log("sync: out of time; the next run carries on")
            return False
        if time.time() - held_since >= LEASE_HOLD:
            lease.release()
            lease = None
            time.sleep(LEASE_GAP)                           # a dashboard step or the weekly reminder takes it now
            lease = take_lease("drive-sync", fail_open=False)
            if lease is None:
                log("sync: another step took the job lease; the next run carries on")
                return False
            held_since = time.time()
        return True

    try:
        update_sync_state(store, busy=False, running=True)
        ledger = {r["file_id"]: r for r in store.list_statement_sources()}
        todo, _ = bank_todo(files, ledger, now)              # again: an earlier run may have read some
        uploaded_by = "Drive: " + (folder.get("name") or "Bank Transactions")
        pending = {tuple(m) for m in (store.get_state("bank_sync") or {}).get("months_pending") or []}
        stopped = False
        for f in todo:
            if steps and not turn():
                stopped = True                              # files not reached keep their rows: read next time
                break
            steps += 1
            seen = ledger.get(f["id"])
            who = uploaded_by + (f"/{f['path']}" if f.get("path") else "")
            row, summary = import_bank_file(drive, f, seen, who, log)
            store.save_statement_source(row)
            (imported if summary else failed).append(row["name"])
            if summary:
                # A version read before needs only the months of lines it did not have. Anything
                # else (a first read, a retry) may have stored lines without its row saying so: all.
                if seen and seen.get("status") == "imported":
                    before = set(seen.get("transaction_ids") or [])
                    months = [m for tid, m in summary["line_months"].items() if tid not in before]
                else:
                    months = summary["months"]
                pending |= {tuple(m) for m in months}
                queue_months(store, add=pending)
                update_sync_state(store, last_import={"name": row["name"], "at": row["checked_at"], "new": row["new_count"]})
        # Invoices filed since the last run: a quick step per month folder, then one match step, before
        # the long full matches. The run's totals are what the dashboard's line reports.
        if INVOICE_WATCH_ON:
            gemini, totals = get_gemini(), {"read": 0, "matched": 0, "possible": 0}
            for folder in list((store.get_state(INVOICE_WATCH) or {}).get("folders_pending") or []):
                if stopped or (steps and not turn()):
                    stopped = True
                    break
                steps += 1
                try:
                    totals["read"] += read_invoice_folder(store, drive, gemini, folder, log)
                except Exception as exc:  # noqa: BLE001  (noted; the statement months still run)
                    watch_step_failed(store, folder, exc, log)
            if not stopped and (store.get_state(INVOICE_WATCH) or {}).get("to_match"):
                if steps and not turn():
                    stopped = True
                else:
                    steps += 1
                    counts = match_watched_invoices(store, gemini, log)
                    totals["matched"], totals["possible"] = counts["matched"], counts["possible"]
                    log(f"invoices: {totals['read']} read; {counts['matched']} matched, "
                        f"{counts['possible']} possible match(es)")
            if any(totals.values()) and lease is not None:
                update_watch_state(store, last=dict(totals, at=now_iso()))
        for year, month in sorted(pending, reverse=True):       # newest first
            if stopped or (steps and not turn()):
                log(f"sync: {len(pending)} months left for the next run")
                break
            steps += 1
            log(f"sync: reading invoices and matching {year}-{month:02d}")
            began = now_iso()
            for _ in range(MAX_INDEX_ROUNDS):
                if not index_month(year, month, log=log).get("remaining"):
                    break
            match_month(year, month, log=log)
            pending.discard((year, month))
            queue_months(store, done=[(year, month)], started=began)
    finally:
        if lease is not None:
            lease.release()
        update_sync_state(store, running=False, finished_at=now_iso())
    watch = store.get_state(INVOICE_WATCH) or {}
    return {"status": status, "imported": imported, "failed": failed, "settling": settling, "months_left": len(pending),
            "invoice_folders_left": len(watch.get("folders_pending") or [])}


# -- one-off: read again invoices read before card_charges existed ------------------------------
REREAD_BUDGET = 50 * 60         # seconds; the job may run an hour, the rest is left for the next run


def reread_card_charges(vendor="amazon", limit=None, log=print):
    """Read again the invoices of a vendor (folder or name containing `vendor`) that were read before
    the reading listed card charges (card_charges is null), so an order charged in parts (an Amazon
    order shipped in several boxes) can be matched charge by charge; then queue the months they serve
    for a full match by the import job (months_pending). A reading that fails keeps the old one. Run
    again to carry on: what is done is not read again. Returns counts."""
    store, drive, gemini = get_store(), get_drive(), get_gemini()
    if gemini is None:
        return {"skipped": "AI_DISABLED=1"}
    word = (vendor or "").lower()
    todo = [r for r in store.list_invoices()
            if r.get("card_charges") is None and not r.get("removed_at") and r.get("month_folder")
            and not r.get("extraction_error") and r.get("extracted_at")
            and word in f"{r.get('vendor_folder') or ''} {r.get('vendor') or ''}".lower()]
    todo.sort(key=lambda r: str(r.get("month_folder") or ""), reverse=True)     # recent months first
    if limit:
        todo = todo[:int(limit)]
    started, done, failed, months = time.time(), 0, 0, set()
    log(f"reread: {len(todo)} {vendor} invoice(s) to read again")
    for start in range(0, len(todo), INDEX_BATCH):
        if time.time() - started > REREAD_BUDGET:
            log("reread: out of time; run it again to carry on")
            break
        batch = todo[start:start + INDEX_BATCH]
        records = [dict({k: r.get(k) for k in ("id", "file_name", "mime_type", "web_view_link", "folder_path",
                                                 "month_folder", "vendor_folder", "modified_time")},
                        size=int(r.get("size") or 0),
                        _file={"id": r["id"], "mimeType": r.get("mime_type"), "name": r.get("file_name")})
                   for r in batch]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, INDEX_WORKERS)) as pool:
            rows = list(pool.map(lambda rec: invoice_index.extract_invoice(gemini, drive, rec), records))
        good = [row for row in rows if not row.get("extraction_error") and row.get("card_charges") is not None]
        failed += len(rows) - len(good)
        # Only the new column is written: the rest of each row may have changed since the list was
        # taken (a Remove… meanwhile), and the old reading of vendor, total and date stays as it was.
        now = {r["id"]: r for r in store.list_invoices(ids=[row["id"] for row in good])}
        batch_months = set()
        for row in good:
            current = now.get(row["id"])
            if not current or current.get("removed_at") or current.get("card_charges") is not None:
                continue
            store.update_invoices([row["id"]], {"card_charges": row["card_charges"]})
            done += 1
            current = dict(current, card_charges=row["card_charges"])
            if matching.shared(current):
                batch_months |= set(matching.months_to_match(current))
                # Linked whole before its charges were known: each link moves to the charge it is.
                matching.split_whole_links(store, current, log)
        if batch_months:
            queue_months(store, add=batch_months)       # now, not at the end: a run cut short keeps them
            months |= batch_months
        log(f"reread: {done} read again, {failed} failed")
    if months:
        log(f"reread: {len(months)} month(s) queued for a full match")
    return {"to_read": len(todo), "read": done, "failed": failed, "months_queued": len(months)}


def reread_documents(vendor, limit=None, log=print):
    """Read again a vendor's documents (folder or name containing `vendor`) read before the reading told
    payment confirmations from bills (paid_invoice_numbers is null): a UPS "Payment Confirmation" e-mail
    or a billing centre's receipt becomes document_type payment_confirmation with the invoice numbers it
    pays, and a statement that lists the card payments it records (Google Ads, an insurer's premium
    history) gets them as card_charges. Only those columns are written (the rest of the old reading
    stays; a document newly typed as a confirmation also gets its own number and is_invoice), and card
    charges only where fewer than two are stored and none were worked out by the matcher. The months the
    documents serve are queued for a full match. A reading that fails keeps the old one; run again to
    carry on. Returns counts."""
    word = (vendor or "").strip().lower()
    if not word:
        return {"skipped": "name a vendor (--vendor ups)"}
    store, drive, gemini = get_store(), get_drive(), get_gemini()
    if gemini is None:
        return {"skipped": "AI_DISABLED=1"}
    todo = [r for r in store.list_invoices()
            if r.get("paid_invoice_numbers") is None and not r.get("removed_at") and r.get("month_folder")
            and not r.get("extraction_error") and r.get("extracted_at")
            and word in f"{r.get('vendor_folder') or ''} {r.get('vendor') or ''}".lower()]
    todo.sort(key=lambda r: str(r.get("month_folder") or ""), reverse=True)     # recent months first
    if limit:
        todo = todo[:int(limit)]
    started, done, failed, typed, listed, months = time.time(), 0, 0, 0, 0, set()
    log(f"reread: {len(todo)} {vendor} document(s) to read again")
    for start in range(0, len(todo), INDEX_BATCH):
        if time.time() - started > REREAD_BUDGET:
            log("reread: out of time; run it again to carry on")
            break
        batch = todo[start:start + INDEX_BATCH]
        records = [dict({k: r.get(k) for k in ("id", "file_name", "mime_type", "web_view_link", "folder_path",
                                                 "month_folder", "vendor_folder", "modified_time")},
                        size=int(r.get("size") or 0),
                        _file={"id": r["id"], "mimeType": r.get("mime_type"), "name": r.get("file_name")})
                   for r in batch]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, INDEX_WORKERS)) as pool:
            rows = list(pool.map(lambda rec: invoice_index.extract_invoice(gemini, drive, rec), records))
        good = [row for row in rows if not row.get("extraction_error") and row.get("paid_invoice_numbers") is not None]
        failed += len(rows) - len(good)
        now = {r["id"]: r for r in store.list_invoices(ids=[row["id"] for row in good])}
        batch_months = set()
        for row in good:
            current = now.get(row["id"])
            if not current or current.get("removed_at") or current.get("paid_invoice_numbers") is not None:
                continue
            patch = {"paid_invoice_numbers": row["paid_invoice_numbers"]}
            if row.get("document_type") == matching.CONFIRMATION or current.get("document_type") == matching.CONFIRMATION:
                # Typed anew: a confirmation's own number replaces the number of the invoice it pays (a
                # Purolator receipt was read as that invoice's number), and it counts as a receipt.
                patch.update({"document_type": row.get("document_type"), "is_invoice": row.get("is_invoice"),
                              "invoice_number": row.get("invoice_number")})
            held = current.get("card_charges") or []
            inferred = bool(held) and all(isinstance(c, dict) and c.get("inferred") for c in held)
            if len(matching.listed_charges(row)) > 1 and not inferred and len(matching.listed_charges(current)) < 2:
                patch["card_charges"] = row["card_charges"]
                listed += 1
            store.update_invoices([row["id"]], patch)
            done += 1
            typed += int(patch.get("document_type") == matching.CONFIRMATION)
            current = dict(current, **patch)
            if patch.keys() - {"paid_invoice_numbers"}:
                batch_months |= set(matching.months_to_match(current))
                if matching.shared(current):
                    matching.split_whole_links(store, current, log)
        if batch_months:
            queue_months(store, add=batch_months)       # now, not at the end: a run cut short keeps them
            months |= batch_months
        log(f"reread: {done} read again ({typed} payment confirmation(s), {listed} statement(s) listing "
            f"payments), {failed} failed")
    return {"to_read": len(todo), "read": done, "confirmations": typed, "statements": listed, "failed": failed,
            "months_queued": len(months)}


def reread_charge_dates(vendor, limit=None, log=print):
    """Read again the invoices of a vendor (folder or name containing `vendor`) read before
    invoices.charge_date existed, to learn the date each says it is charged automatically (a
    pre-authorized bill: TELUS "will be charged to your credit card on October 09"); then pair them
    again. A purchase the matcher linked to one of them that falls more than CHARGE_DATE_DAYS from its
    charge date is let go (compare-and-set; links made by hand stay), and every purchase from that
    vendor around those dates that still needs an invoice is matched again with the rules, oldest
    first, as Rematch does. A reading that fails or states no charge date changes nothing. Run again to
    carry on. Returns counts."""
    word = (vendor or "").strip().lower()
    if not word:
        return {"skipped": "name a vendor (--vendor)"}
    store, drive, gemini = get_store(), get_drive(), get_gemini()
    if gemini is None:
        return {"skipped": "AI_DISABLED=1"}
    todo = [r for r in store.list_invoices()
            if r.get("charge_date") is None and not r.get("removed_at") and r.get("month_folder")
            and not r.get("extraction_error") and r.get("extracted_at") and r.get("total") is not None
            and word in f"{r.get('vendor_folder') or ''} {r.get('vendor') or ''}".lower()]
    todo.sort(key=lambda r: str(r.get("month_folder") or ""))
    if limit:
        todo = todo[:int(limit)]
    log(f"reread: {len(todo)} {vendor} invoice(s) to read for the date they are charged")
    started, dated, failed = time.time(), [], 0
    for start in range(0, len(todo), INDEX_BATCH):
        if time.time() - started > REREAD_BUDGET:
            log("reread: out of time; run it again to carry on")
            break
        batch = todo[start:start + INDEX_BATCH]
        records = [dict({k: r.get(k) for k in ("id", "file_name", "mime_type", "web_view_link", "folder_path",
                                                 "month_folder", "vendor_folder", "modified_time")},
                        size=int(r.get("size") or 0),
                        _file={"id": r["id"], "mimeType": r.get("mime_type"), "name": r.get("file_name")})
                   for r in batch]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, INDEX_WORKERS)) as pool:
            rows = list(pool.map(lambda rec: invoice_index.extract_invoice(gemini, drive, rec), records))
        failed += sum(1 for row in rows if row.get("extraction_error"))
        now = {r["id"]: r for r in store.list_invoices(ids=[row["id"] for row in rows if row.get("charge_date")])}
        for row in rows:
            current = now.get(row["id"])
            # Only the new column is written, and only while the stored reading is the one listed.
            if not row.get("charge_date") or not current or current.get("removed_at") or current.get("charge_date"):
                continue
            store.update_invoices([row["id"]], {"charge_date": row["charge_date"]})
            dated.append(dict(current, charge_date=row["charge_date"]))
            log(f"reread: {current.get('file_name')} is charged on {row['charge_date']}")
    # Pair from what is stored, not from this run's writes: a run cut short after dating bills (a
    # timeout) is finished by the next one.
    newly = len(dated)
    dated = [r for r in store.list_invoices()
             if r.get("charge_date") and not r.get("removed_at") and r.get("month_folder")
             and word in f"{r.get('vendor_folder') or ''} {r.get('vendor') or ''}".lower()]
    matching.refresh_card_aliases(store, force=True)
    released = 0
    for inv in dated:
        for t in store.transactions_for_invoice(inv["id"]):
            if t.get("invoice_id") != inv["id"] or t.get("match_method") == "manual" \
                    or not matching.beyond_charge_date(t, inv):
                continue
            expect = {"invoice_id": inv["id"], "invoice_part": t.get("invoice_part"),
                      "invoice_status": t.get("invoice_status"), "match_method": t.get("match_method")}
            if store.update_transaction(t["id"], {"invoice_status": "missing", "invoice_id": None, "invoice_part": None,
                                                  "match_confidence": None, "match_method": None, "match_note": None,
                                                  "updated_at": now_iso()}, expect=expect) is not None:
                released += 1
                log(f"reread: {t['txn_date']} {float(t['amount']):.2f} let go of {inv.get('file_name')} "
                    f"(charged {inv['charge_date']})")
    months = set()
    for inv in dated:
        months |= set(matching.months_to_match(inv))
        day = matching._day(inv["charge_date"])
        months |= {(day.year, day.month)} if day else set()
    candidates = [t for y, m in sorted(months) for t in store.list_transactions(y, m)
                  if t.get("type") == "purchase" and t.get("invoice_status") in ("missing", "possible")
                  and t.get("match_method") != "manual"
                  and any(matching.vendor_similarity(t, inv) >= matching.VENDOR_EVIDENCE for inv in dated)]
    found = 0
    for t in sorted(candidates, key=lambda t: (str(t.get("txn_date")), t["id"])):
        try:
            out = matching.match_transaction(store, None, t["id"], use_ai=False, log=log, updated_by="reread")
        except matching.RematchError:
            continue
        except StoreError as exc:           # one failed request costs that purchase; a rerun finishes it
            log(f"reread: {t['id']} not matched again: {exc}")
            continue
        found += out["outcome"] == "found"
    return {"to_read": len(todo), "dated": newly, "failed": failed, "released": released,
            "rematched": len(candidates), "found": found}


# ---------------------------------------------------------------------------
# Local CLI (and the Cloud Run jobs, which run `remind` and `sync`)
#   python main.py parse <file>                       print the parsed transactions
#   python main.py dry-run <file> [--no-ai]           full flow on an in-memory store, Chat cards printed
#   python main.py index [--months "2026 09,2026 10"] read new invoices into the configured store
#   python main.py match --year 2026 --month 9
#   python main.py notify --year 2026 --month 9 [--dry-run]
#   python main.py remind                             the weekly reminder (what the Cloud Run job runs)
#   python main.py sync                               import new statements from the Bank Transactions folder
#   python main.py reread [--vendor amazon]           read again invoices read before card_charges existed
#   python main.py reread-charge-dates --vendor telus  read a vendor's bills for the date they are charged, re-pair
#   python main.py serve [--port 8080]
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["parse", "dry-run", "index", "match", "notify", "remind", "sync", "reread",
                                        "reread-charge-dates", "reread-documents",
                                        "serve"])
    ap.add_argument("--vendor", default=None,
                    help="reread (default amazon) / reread-charge-dates, reread-documents (required): vendor folder or name")
    ap.add_argument("file", nargs="?")
    ap.add_argument("--year", type=int)
    ap.add_argument("--month", type=int)
    ap.add_argument("--card")
    ap.add_argument("--months", help="comma-separated 'YYYY MM' folders for index")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = ap.parse_args()
    if args.no_ai:
        AI_DISABLED = True
    if args.command != "serve":
        LEASE_HOLDER = f"job:{_RUN_ID}:{uuid.uuid4().hex[:8]}"     # never taken over by the service
    if args.command == "parse":
        with open(args.file, "rb") as fh:
            result = statements.parse_statement(os.path.basename(args.file), fh.read(), gemini=get_gemini())
        print(json.dumps({k: v for k, v in result.items() if k != "transactions"}, indent=2))
        for t in result["transactions"]:
            print(f"{t['id']} {t['card_last4']} {t['txn_date']} {t['type']:8} {t['amount']:>10.2f} {t['currency']} {t['description'][:40]}")
    elif args.command == "dry-run":
        set_clients(store=MemoryStore())
        with open(args.file, "rb") as fh:
            data = fh.read()
        summary = ingest_statement(os.path.basename(args.file), data, "", "cli")
        print(json.dumps({k: v for k, v in summary.items() if k != "cards"}, indent=2))
        for year, month in summary["months"]:
            while True:
                result = index_month(year, month, limit=args.limit)
                print(json.dumps({k: v for k, v in result.items() if k != "errors"}), f"errors={len(result.get('errors', []))}")
                for err in result.get("errors", []):
                    print("  error:", err)
                if not result.get("remaining"):
                    break
            print(json.dumps(match_month(year, month, args.card), indent=2))
            print(json.dumps(notify_month(year, month, args.card, sent_by="cli", dry_run=True), indent=2))
    elif args.command == "index":
        months = [m.strip() for m in (args.months or "").split(",") if m.strip()] or None
        print(json.dumps(index_all(months, args.limit), indent=2))
    elif args.command == "match":
        print(json.dumps(match_month(args.year, args.month, args.card), indent=2))
    elif args.command == "notify":
        print(json.dumps(notify_month(args.year, args.month, args.card, sent_by="cli", dry_run=args.dry_run), indent=2))
    elif args.command == "remind":
        print(json.dumps(remind_latest(), indent=2))
    elif args.command == "sync":
        print(json.dumps(sync_bank_folder(), indent=2))
    elif args.command == "reread":
        print(json.dumps(reread_card_charges(args.vendor or "amazon", args.limit), indent=2))
    elif args.command == "reread-charge-dates":
        print(json.dumps(reread_charge_dates(args.vendor, args.limit), indent=2))
    elif args.command == "reread-documents":
        print(json.dumps(reread_documents(args.vendor, args.limit), indent=2))
    else:
        app.run(host="127.0.0.1", port=args.port, debug=False)
