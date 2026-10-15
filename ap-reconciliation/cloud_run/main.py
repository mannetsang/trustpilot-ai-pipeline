"""AP credit-card reconciliation: statement upload, invoice matching, Chat reminders, dashboard.

Accounts payable uploads a credit-card statement or card-transaction export
(.xlsx/.csv parsed directly, .pdf/images read by Gemini). The service stores
each line once (deterministic ids), walks the AP mailbox's Drive folder
Invoice/YYYY MM/<vendor>/ to read every new invoice with Gemini, pairs
purchases with invoices (rules first, Gemini judge for the rest), posts one
"invoices needed" card to each credit card's Google Chat space, and serves the
dashboard where card owners add the cost centre and usage of their charges,
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
  GET  /api/months               year/month navigation with per-card counts
  GET  /api/transactions?year&month[&card]
  GET  /api/transactions/<id>    one row (the page polls it while an uploaded receipt is being read)
  PATCH /api/transactions/<id>   cost_center, usage, note, action=waive|unwaive|confirm|link|unlink
  POST /api/transactions/<id>/rematch  read new invoices, then look for this purchase's invoice again (202, a job)
  GET  /api/transactions/<id>/receipt-target  where Add receipt… would save: month and vendor folder, folders
  POST /api/transactions/<id>/receipt   multipart file (1-10 pages), vendor_folder, upload_id: save the
                                 receipt in Drive and link it (201; 200 for a retried upload_id)
  DELETE /api/transactions/<id>/receipt  Remove…: unlink a receipt added from the dashboard, trash its file
  GET  /receipts/<last4>/<YYYY>/<MM>  the Chat card's Add receipts: redirects to that card's receipts list
  POST /api/ask?year&month[&card]  voice mode: {"question"} -> a short answer to read aloud (read only)
  GET  /api/invoices?year&month[&all=1]  invoices of the surrounding month folders (link picker)
  GET  /api/cards  POST /api/cards (AP)     card labels, owners, Chat space names
  GET  /api/statements?year&month           uploads
  POST /api/statements (AP, multipart file; ?sync=1 also indexes, matches and notifies)
  DELETE /api/statements/<id> (AP)          remove an upload and its untouched lines
  POST /api/index?year&month&limit (AP)     read new invoices for month-1..month+1, one batch
  POST /api/match?year&month[&card] (AP)    pair purchases with invoices
  POST /api/notify?year&month[&card] (AP)   post the "invoices needed" Chat cards
  POST /api/jobs?kind=index|match|notify&year&month[&card] (AP)  the same steps in the background (202)
  GET  /api/jobs/current                    the running or last background job: progress, then result
The weekly reminder is a Cloud Run job running `python main.py remind` (see the deploy workflow).

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, INVOICE_FOLDER_ID, STATEMENTS_FOLDER_ID (optional),
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
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict

from flask import Flask, abort, g, jsonify, redirect, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Local runs: read the repo's gitignored .env. The container has no lib/, so this is a no-op there.
try:
    sys.path.insert(0, os.path.join(HERE, "..", ".."))
    from lib.secrets import load_dotenv  # noqa: E402
    load_dotenv(HERE)
except Exception:  # noqa: BLE001
    pass

import assistant  # noqa: E402
import chat  # noqa: E402
import invoices as invoice_index  # noqa: E402
import matching  # noqa: E402
import receipts  # noqa: E402
import statements  # noqa: E402
from drive import FOLDER, Drive, DriveError  # noqa: E402
from gemini import Gemini, GeminiError  # noqa: E402
from store import MemoryStore, StoreError, SupabaseStore, month_folder_name, neighbouring_months, now_iso  # noqa: E402

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
AP_CLIENT_ID = os.environ.get("GOOGLE_WORKSPACE_AP_CLIENT_ID", "")
AP_CLIENT_SECRET = os.environ.get("GOOGLE_WORKSPACE_AP_CLIENT_SECRET", "")
AP_REFRESH_TOKEN = os.environ.get("GOOGLE_WORKSPACE_AP_REFRESH_TOKEN", "")
CARD_WEBHOOKS = chat.load_webhooks(os.environ.get("CARD_WEBHOOKS_JSON", ""), os.environ.get("GCHAT_WEBHOOK_URL", ""))
# Identity-Aware Proxy signs every request it lets through; the JWT's audience names this
# service: /projects/<project number>/locations/<region>/services/<service>.
IAP_AUDIENCE = os.environ.get("IAP_AUDIENCE", "")
IAP_HEADER = "X-Goog-IAP-JWT-Assertion"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
DASHBOARD_USER_HEADER = "X-Dashboard-User"     # open mode: the name typed into the page
ALLOWED_DOMAINS = set(_csv("ALLOWED_DOMAINS", "superhairpieces.com"))
AP_EMAILS = set(_csv("AP_EMAILS", "ap@superhairpieces.com,manne@superhairpieces.com"))
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


def receipt_upload_enabled(last4):
    return bool(RECEIPT_UPLOAD_CARDS & {"all", "*"}) or str(last4) in RECEIPT_UPLOAD_CARDS


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


# ---------------------------------------------------------------------------
# Jobs (one at a time per instance, run inside the request like the review services)
# ---------------------------------------------------------------------------
_job_lock = threading.Lock()
_last_job = {"name": None, "started": None, "finished": None, "result": None, "error": None, "progress": {}}


def run_locked(name, fn, *args, **kwargs):
    if not _job_lock.acquire(blocking=False):
        return 409, {"status": "busy", "last_job": _last_job}
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
def ingest_statement(file_name, data, mime_type, uploaded_by):
    """Parse the upload and store what is new. Returns the summary shown to AP."""
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

    existing_ids = store.existing_transaction_ids([t["id"] for t in txns])
    new = [t for t in txns if t["id"] not in existing_ids]
    months = sorted({month_folder_name(t["year"], t["month"]) for t in txns})
    stamp = now_iso()
    drive_file = {}
    if STATEMENTS_FOLDER_ID and new:
        try:
            drive = get_drive()
            folder = drive.ensure_folder(STATEMENTS_FOLDER_ID, months[-1])
            drive_file = drive.upload(folder, file_name, data, mime_type)
        except Exception as exc:  # noqa: BLE001
            print(f"statement copy to Drive failed: {exc}")
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
    store.insert_transactions(rows)
    return {
        "statement_id": statement["id"], "file_name": file_name, "format": parsed["format"],
        "period_start": parsed["period_start"], "period_end": parsed["period_end"],
        "transactions": len(txns), "new": len(new), "already_stored": len(txns) - len(new),
        "cards": {l4: {"holder_name": c.get("holder_name", ""), "count": c["count"]} for l4, c in parsed["cards"].items()},
        "months": [[int(m[:4]), int(m[5:])] for m in months],
        "warnings": parsed["warnings"], "drive_link": drive_file.get("webViewLink"),
    }


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
        if not (t.get("cost_center") or ""):
            bucket["uncoded"] += 1
    results, folder = [], month_folder_url(year, month) if not dry_run else ""
    for last4, bucket in sorted(by_card.items()):
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
                        if base and receipt_upload_enabled(last4) else "")
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


def with_invoices(store, txns):
    ids = sorted({t["invoice_id"] for t in txns if t.get("invoice_id")})
    invoices = {i["id"]: i for i in store.list_invoices(ids=ids)} if ids else {}
    with _reading_lock:
        reading = set(_reading)
    out = []
    for t in txns:
        row = {k: v for k, v in t.items() if k != "raw"}
        inv = invoices.get(t.get("invoice_id"))
        row["invoice"] = ({k: inv.get(k) for k in ("id", "file_name", "web_view_link", "vendor", "invoice_number",
                                                   "invoice_date", "total", "currency", "vendor_folder", "month_folder",
                                                   "card_last4", "is_invoice", "extraction_error", "extracted_at",
                                                   "summary")}
                          if inv else None)
        if inv:
            row["invoice"]["reading"] = inv["id"] in reading
        # What the receipt says against the charge, worked out each time from the stored facts: a
        # disagreement is shown, never turned into a status (the link stays as the person made it).
        row["checks"] = (matching.invoice_checks(t, inv, reading=inv["id"] in reading)
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


def card_view(c):
    return {"last4": c["last4"], "label": c.get("label") or f"Card ending {c['last4']}",
            "holder_name": c.get("holder_name") or "", "owner_email": c.get("owner_email") or "",
            "chat_space": c.get("chat_space") or "", "active": c.get("active", True) is not False,
            "webhook_configured": bool(CARD_WEBHOOKS.get(c["last4"])),
            "webhook_default": bool(chat.webhook_for(CARD_WEBHOOKS, c["last4"])),
            "receipt_upload": receipt_upload_enabled(c["last4"])}


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


def receipt_access(user, txn, need_enabled=True):
    """AuthError(403) unless the card has receipt upload (when need_enabled) and the user may edit it."""
    last4 = txn["card_last4"]
    if need_enabled and not receipt_upload_enabled(last4):
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
        if (upload_id and txn.get("invoice_id") and txn.get("match_note") == f"receipt uploaded by {email}"
                and _uploaded_with(drive, txn["invoice_id"]) == upload_id):
            # The same receipt again: the first save went through but its answer was lost.
            inv = store.get_invoice(txn["invoice_id"]) or {}
            with _reading_lock:
                reading = txn["invoice_id"] in _reading
            file_info = {"id": txn["invoice_id"], "name": inv.get("file_name"),
                         "folder": str(inv.get("folder_path") or "").strip("/"), "folder_created": False,
                         "web_view_link": inv.get("web_view_link"), "pages": pages, "size": len(data),
                         "mime_type": inv.get("mime_type") or mime}
            return 200, _saved_body(store, txn, file_info, reading, True)
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
    patch = {"invoice_status": "matched", "invoice_id": meta["id"], "match_confidence": 1.0,
             "match_method": "manual", "match_note": f"receipt uploaded by {email}",
             "rejected_invoice_ids": rejected, "updated_by": email, "updated_at": now_iso()}
    expect = {"invoice_status": status, "invoice_id": txn.get("invoice_id"), "match_method": txn.get("match_method")}
    try:
        linked = store.update_transaction(txn_id, patch, expect=expect)
    except StoreError as exc:                   # the unique index: that id is linked elsewhere
        print(f"receipt: link of {meta['id']} to {txn_id} refused: {exc}")
        linked = None
    if linked is None:
        # The purchase changed while the file went up (a match job, a Link, a waive): take the
        # upload back so no stray copy joins the matching pool.
        _trash_quietly(drive, meta["id"], "the purchase changed while it was saving")
        store.update_invoices([meta["id"]], {"removed_at": now_iso(), "extraction_error": WITHDRAWN})
        current = store.get_transaction(txn_id)
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

    Writes only the invoice row, never the transaction, so it races with nothing. removed_at is left
    out of the row, so a Remove made during the read stands. On any failure the index retries the
    file (modified_time unset, or the stored extraction_error)."""
    try:
        row = invoice_index.extract_invoice(get_gemini(), get_drive(), record, content=(data, mime))
        row.pop("removed_at", None)
        get_store().upsert_invoices([row])
        print(f"receipt read {record['id']}: " + (f"error {row['extraction_error']}" if row.get("extraction_error")
              else f"{row.get('vendor')} {row.get('total')} {row.get('currency')} {row.get('invoice_date')}"))
    except Exception as exc:  # noqa: BLE001
        print(f"receipt read {record.get('id')} failed: {exc}")
    finally:
        with _reading_lock:
            _reading.discard(record.get("id"))


def remove_receipt(user, txn_id):
    """Remove…: unlink a receipt added from the dashboard and move its file to the Drive trash.
    -> (HTTP status, body). Not gated on RECEIPT_UPLOAD_CARDS, so an upload can still be taken back
    after a card is switched off. The id is not added to rejected_invoice_ids: the file is gone."""
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
    patch = {"invoice_status": "missing", "invoice_id": None, "match_confidence": None, "match_method": None,
             "match_note": None, "updated_by": user["email"], "updated_at": now_iso()}
    updated = store.update_transaction(txn_id, patch, expect={"invoice_id": invoice_id, "match_method": "manual"})
    if updated is None:
        return 409, {"error": "This purchase changed meanwhile; reload."}
    trashed, error = _trash_quietly(drive, invoice_id, "Remove") if meta is not None else (True, "")
    store.update_invoices([invoice_id], {"removed_at": now_iso()})
    print(f"receipt: {user['email']} removed {invoice_id} from {txn_id}"
          + ("" if trashed else f" (not trashed: {error})"))
    body = {"transaction": transaction_row(store, updated), "trashed": trashed}
    if not trashed:
        body["warning"] = f"Unlinked, but the file could not be moved to the Drive trash: {error}"
    return 200, body


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.errorhandler(AuthError)
def _auth_error(exc):
    return jsonify({"error": exc.message}), exc.status


@app.errorhandler(statements.StatementError)
def _statement_error(exc):
    return jsonify({"error": str(exc)}), 400


@app.errorhandler(receipts.ReceiptError)
def _receipt_error(exc):
    return jsonify({"error": exc.message}), exc.status


@app.errorhandler(StoreError)
def _store_error(exc):
    print(f"store error: {exc}")
    return jsonify({"error": f"database error: {exc}"}), 502


@app.errorhandler(DriveError)
def _drive_error(exc):
    print(f"drive error: {exc}")
    return jsonify({"error": f"Google Drive error: {exc}"}), 502


@app.errorhandler(GeminiError)
def _gemini_error(exc):
    print(f"gemini error: {exc}")
    return jsonify({"error": f"Gemini error: {exc}"}), 502


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
    if request.path in ("/", "/index.html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


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
    return jsonify({"months": month_list(store), "cards": [card_view(c) for c in store.list_cards()],
                    "cost_centers": store.list_cost_centers()})


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
    return jsonify({"year": year, "month": month, "transactions": rows,
                    "cards": [card_view(c) for c in cards], "cost_centers": store.list_cost_centers(),
                    "statements": store.list_statements(year, month),
                    "notifications": store.list_notifications(year, month)[:20]})


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
    if not may_edit(user, txn["card_last4"]):
        return jsonify({"error": "you can only edit transactions of cards you own"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "the body must be a JSON object"}), 400
    patch = {}
    for field, limit in EDITABLE_TEXT.items():
        if field in body:
            value = str(body.get(field) or "").strip()
            patch[field] = value[:limit] or None
    action = str(body.get("action") or "").strip().lower()
    status = txn.get("invoice_status")
    rejected = list(txn.get("rejected_invoice_ids") or [])
    if action == "waive":
        if status in ("missing", "possible"):
            patch.update({"invoice_status": "waived", "invoice_id": None, "match_confidence": None,
                          "match_method": "manual", "match_note": "waived: " + (body.get("reason") or "no invoice available")[:200]})
    elif action == "unwaive":
        if status == "waived":
            patch.update({"invoice_status": "missing", "match_method": None, "match_note": None})
    elif action == "confirm":
        if status == "possible" and txn.get("invoice_id"):
            patch.update({"invoice_status": "matched", "match_confidence": 1.0, "match_method": "manual",
                          "match_note": "confirmed by " + user["email"]})
    elif action == "link":
        invoice_id = str(body.get("invoice_id") or "").strip()
        inv = store.get_invoice(invoice_id) if invoice_id else None
        if not inv:
            return jsonify({"error": "invoice not found"}), 404
        if user["role"] != "ap" and inv.get("card_last4") and inv["card_last4"] != txn["card_last4"]:
            return jsonify({"error": f"that invoice shows card ending {inv['card_last4']}; ask accounts payable to link it"}), 403
        holder = store.used_invoice_ids()
        if invoice_id in holder and txn.get("invoice_id") != invoice_id:
            return jsonify({"error": "that invoice is already linked to another transaction"}), 409
        patch.update({"invoice_status": "matched", "invoice_id": invoice_id, "match_confidence": 1.0,
                      "match_method": "manual", "match_note": "linked by " + user["email"],
                      "rejected_invoice_ids": [i for i in rejected if i != invoice_id]})
    elif action == "unlink":
        if txn.get("invoice_id") or status == "matched":
            # Remember the refusal so the matcher does not propose the same pair again.
            if txn.get("invoice_id") and txn["invoice_id"] not in rejected:
                rejected.append(txn["invoice_id"])
            patch.update({"invoice_status": "missing", "invoice_id": None, "match_confidence": None,
                          "match_method": None, "match_note": None, "rejected_invoice_ids": rejected})
    elif action:
        return jsonify({"error": f"unknown action '{action}'"}), 400
    if not patch:
        return jsonify({"error": "nothing to update"}), 400
    patch.update({"updated_by": user["email"], "updated_at": now_iso()})
    updated = store.update_transaction(txn_id, patch)
    row = with_invoices(store, [updated])[0]
    row["editable"] = True  # may_edit passed above; the dashboard re-renders the row from this
    return jsonify({"transaction": row})


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
    used = store.used_invoice_ids()
    out = []
    for inv in store.list_invoices(month_folders=None if everything else folders):
        if inv.get("removed_at") and inv["id"] not in used:
            continue
        row = {k: inv.get(k) for k in ("id", "file_name", "web_view_link", "month_folder", "vendor_folder", "vendor",
                                       "invoice_number", "invoice_date", "total", "currency", "card_last4",
                                       "summary", "is_invoice", "extraction_error", "document_type", "removed_at")}
        row["linked"] = inv["id"] in used
        out.append(row)
    return jsonify({"invoices": out, "month_folders": "all" if everything else folders})


@app.route("/api/cards", methods=["GET"])
def api_cards():
    require_user()
    return jsonify({"cards": [card_view(c) for c in get_store().list_cards()]})


@app.route("/api/cards", methods=["POST"])
def api_cards_update():
    user = require_user(ap_only=True)
    body = request.get_json(silent=True) or {}
    last4 = statements.last4(body.get("last4"))
    if not last4:
        return jsonify({"error": "last4 must be four digits"}), 400
    row = {"last4": last4, "updated_at": now_iso()}
    for field in ("label", "holder_name", "chat_space"):
        if field in body:
            row[field] = (body.get(field) or "").strip()[:200] or None
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
    get_store().upsert_cards([row])
    print(f"card {last4} updated by {user['email']}")
    return jsonify({"cards": [card_view(c) for c in get_store().list_cards()]})


@app.route("/api/statements", methods=["GET"])
def api_statements():
    require_user()
    year, month = _year_month()
    return jsonify({"statements": get_store().list_statements(year, month)})


@app.route("/api/statements/<statement_id>", methods=["DELETE"])
def api_statement_delete(statement_id):
    """Remove a wrong upload. Lines someone already coded or decided on are kept (detached)."""
    user = require_user(ap_only=True)
    result = get_store().delete_statement(statement_id)
    if not result.get("deleted"):
        return jsonify({"error": "upload not found"}), 404
    print(f"statement {statement_id} removed by {user['email']}: {result}")
    return jsonify({"status": "ok", **result})


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
    return jsonify(job_view())


def remind_latest():
    """Re-index, re-match and re-notify the newest month per card that still has missing invoices.

    This is what the weekly Cloud Run job runs (`python main.py remind`, started by Cloud Scheduler).
    """
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
# Local CLI (and the weekly Cloud Run job, which runs `remind`)
#   python main.py parse <file>                       print the parsed transactions
#   python main.py dry-run <file> [--no-ai]           full flow on an in-memory store, Chat cards printed
#   python main.py index [--months "2026 09,2026 10"] read new invoices into the configured store
#   python main.py match --year 2026 --month 9
#   python main.py notify --year 2026 --month 9 [--dry-run]
#   python main.py remind                             the weekly reminder (what the Cloud Run job runs)
#   python main.py serve [--port 8080]
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["parse", "dry-run", "index", "match", "notify", "remind", "serve"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--year", type=int)
    ap.add_argument("--month", type=int)
    ap.add_argument("--card")
    ap.add_argument("--months", help="comma-separated 'YYYY MM' folders for index")
    ap.add_argument("--limit", type=int, default=INDEX_BATCH)
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = ap.parse_args()
    if args.no_ai:
        AI_DISABLED = True
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
    else:
        app.run(host="127.0.0.1", port=args.port, debug=False)
