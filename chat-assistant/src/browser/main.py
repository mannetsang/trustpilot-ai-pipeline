"""The web browser Man AI operates: a real Chromium on Cloud Run, driven one action at a time.

POST /act  {"session"?, "action", "url"?, "device"?, "target"?, "x"?, "y"?, "text"?, "key"?, "direction"?, "submit"?}
  -> {"session", "url", "title", "image" (base64 JPEG of what's on screen), "elements" (what can be clicked or
      typed into, with its position), "text" (the page's visible text, trimmed), "notes"} or {"error"}

Actions: open (a new tab; desktop or mobile), goto, click (by visible text, label or CSS selector, or at x,y on the
screenshot), type (into a field, optionally pressing Enter), press (a key), select (an option in a dropdown),
scroll (up or down), back, forward, reload, screenshot, close.

Sessions live in this instance's memory (the service runs one instance at most), are closed after IDLE_SECONDS
without use, and the oldest is closed when more than MAX_SESSIONS are open. Cloud Run IAM lets only Man AI's service
account call this service, and this service's own account has no permissions.

Guards that hold whatever the caller asks (so no web page can talk the model past them):
  - only http(s) to public addresses: localhost, private and link-local ranges (the metadata server), and
    *.internal / *.local names are blocked, for the page itself and every request it makes;
  - no typing into password or payment-card fields;
  - no downloads, no file pickers, no camera/location/notification permissions; dialogs are dismissed.
"""

import base64
import ipaddress
import os
import re
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from flask import Flask, jsonify, request

IDLE_SECONDS = int(os.environ.get("IDLE_SECONDS", "600"))
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "4"))
NAV_TIMEOUT_MS = 30000
ACTION_TIMEOUT_MS = 10000
JPEG_QUALITY = 70
TEXT_CHARS = 4000
MAX_ELEMENTS = 60
DEVICES = {
    "desktop": {"viewport": {"width": 1280, "height": 800}},
    "mobile": {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True,
               "device_scale_factor": 2,
               "user_agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
                              "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")},
}
BLOCKED_NAMES = re.compile(r"(^localhost$|\.localhost$|\.internal$|\.local$|^metadata$|^metadata\.google\.internal$)", re.I)
SECRET_FIELD = re.compile(r"(passw|pwd|card|cc-?num|ccnum|cvc|cvv|csc|security.?code|expir|iban|routing|account.?num)", re.I)

app = Flask(__name__)


# -- addresses ----------------------------------------------------------------------------

_dns_cache = {}


def _public_ip(text):
    ip = ipaddress.ip_address(text)
    return ip.is_global and not ip.is_multicast


def blocked_reason(url):
    """Why the browser may not load this URL, or "" if it may."""
    parts = urlsplit(url)
    if parts.scheme in ("data", "blob", "about"):
        return ""  # content the page itself made; never a request to another machine
    if parts.scheme not in ("http", "https"):
        return f"only http(s) pages ({parts.scheme or 'no scheme'})"
    host = (parts.hostname or "").rstrip(".")
    if not host:
        return "no host"
    if BLOCKED_NAMES.search(host):
        return f"{host} is an internal address"
    try:
        return "" if _public_ip(host) else f"{host} is not a public address"
    except ValueError:
        pass  # a name, not an IP
    cached = _dns_cache.get(host)
    if cached is None or time.time() - cached[1] > 300:
        try:
            addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
            ok = bool(addresses) and all(_public_ip(a.split("%")[0]) for a in addresses)
        except OSError:
            ok = True  # unresolvable here: the browser's own lookup fails the same way
        cached = (ok, time.time())
        _dns_cache[host] = cached
    return "" if cached[0] else f"{host} points to a private address"


# -- the browser (Playwright's sync API is single-threaded: every call runs on this one thread) ---------

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")
_state = {"playwright": None, "browser": None}
_sessions = {}  # id -> Session
_lock = threading.Lock()


class Session:
    def __init__(self, device):
        self.device = device
        self.context = _browser().new_context(**DEVICES[device], accept_downloads=False, service_workers="block",
                                              locale="en-US", timezone_id="America/Toronto")
        self.context.set_default_timeout(ACTION_TIMEOUT_MS)
        self.context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        self.context.route("**/*", _guard)
        self.notes = []
        self.context.on("page", self._watch)  # every tab, the first one included
        self.page = self.context.new_page()
        self.used = time.time()

    def _watch(self, page):
        page.on("dialog", self._dismiss)
        page.on("filechooser", lambda fc: self.notes.append("The page asked for a file; none was given."))

    def _dismiss(self, dialog):
        self.notes.append(f"The page showed a {dialog.type} dialog ('{dialog.message[:200]}'); it was dismissed.")
        try:
            dialog.dismiss()
        except Exception:  # noqa: BLE001 - already handled
            pass

    def close(self):
        try:
            self.context.close()
        except Exception:  # noqa: BLE001 - already gone
            pass


def _browser():
    if _state["browser"] is None or not _state["browser"].is_connected():
        from playwright.sync_api import sync_playwright

        if _state["playwright"] is None:
            _state["playwright"] = sync_playwright().start()
        try:  # full Chromium in its new headless mode: the same browser people use, not the stripped-down shell
            _state["browser"] = _state["playwright"].chromium.launch(channel="chromium", args=["--disable-dev-shm-usage"])
        except Exception:  # noqa: BLE001 - only the headless shell is installed
            _state["browser"] = _state["playwright"].chromium.launch(args=["--disable-dev-shm-usage"])
    return _state["browser"]


def _guard(route):
    reason = blocked_reason(route.request.url)
    if reason:
        route.abort("blockedbyclient")
    else:
        route.continue_()


def _sweep():
    now = time.time()
    for sid, s in list(_sessions.items()):
        if now - s.used > IDLE_SECONDS:
            s.close()
            _sessions.pop(sid, None)
    while len(_sessions) > MAX_SESSIONS:
        oldest = min(_sessions, key=lambda k: _sessions[k].used)
        _sessions.pop(oldest).close()


ELEMENTS_JS = """(max) => {
  const out = [];
  const seen = new Set();
  const nodes = document.querySelectorAll('a[href], button, input, select, textarea, summary, [role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox], [role=option], [onclick], label');
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity) === 0) continue;
    if (el.tagName === 'INPUT' && el.type === 'hidden') continue;
    const tag = el.tagName.toLowerCase();
    let label = (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('placeholder') ||
                 el.getAttribute('title') || el.getAttribute('alt') || el.getAttribute('name') || '').trim().replace(/\\s+/g, ' ');
    if (!label && el.querySelector('img[alt]')) label = el.querySelector('img[alt]').getAttribute('alt');
    const kind = tag === 'input' ? 'input:' + (el.type || 'text') : (el.getAttribute('role') || tag);
    const x = Math.round(r.left + r.width / 2), y = Math.round(r.top + r.height / 2);
    const key = kind + label + x + y;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({kind, label: label.slice(0, 80), x, y});
    if (out.length >= max) break;
  }
  return out;
}"""

FIELD_JS = """(el) => [el.type || '', el.name || '', el.id || '', el.getAttribute('autocomplete') || '',
  el.getAttribute('aria-label') || '', el.getAttribute('placeholder') || ''].join(' ')"""


CHECK_TITLES = ("just a moment", "attention required", "checking your browser", "security check")
CHECK_SECONDS = 20


def _bot_check(page):
    """True while the page is a "checking you're not a bot" interstitial (Cloudflare and the like)."""
    try:
        return any(t in (page.title() or "").lower() for t in CHECK_TITLES)
    except Exception:  # noqa: BLE001 - navigating
        return True


def _settle(page):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:  # noqa: BLE001 - busy pages never go idle; what's there is what we show
        pass
    # A bot check usually finishes on its own in a real browser and moves to the page: give it that time.
    # (Nothing here solves or disguises anything; if it doesn't clear, the model sees the check page.)
    deadline = time.time() + CHECK_SECONDS
    while _bot_check(page) and time.time() < deadline:
        page.wait_for_timeout(1000)
    if _bot_check(page):
        return ["The site is still showing a bot check, so the real page isn't visible."]
    return []


def _snapshot(sid, session, notes=()):
    page = session.page
    notes = list(notes) + _settle(page)
    shot = page.screenshot(type="jpeg", quality=JPEG_QUALITY, scale="css", timeout=15000)
    try:
        elements = page.evaluate(ELEMENTS_JS, MAX_ELEMENTS)
    except Exception:  # noqa: BLE001
        elements = []
    try:
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:  # noqa: BLE001
        text = ""
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    all_notes = list(notes) + session.notes
    session.notes = []
    return {"session": sid, "device": session.device, "url": page.url, "title": page.title(),
            "viewport": DEVICES[session.device]["viewport"],
            "scroll": page.evaluate("() => ({y: Math.round(scrollY), height: document.documentElement.scrollHeight})"),
            "image": base64.b64encode(shot).decode("ascii"), "elements": elements,
            "text": text[:TEXT_CHARS] + ("…" if len(text) > TEXT_CHARS else ""), "notes": all_notes}


def _locate(page, target):
    """The element meant by `target`: a CSS selector, or visible text, a label, a placeholder."""
    target = target.strip()
    candidates = []
    if re.match(r"^[#.\[]|^[a-z]+[#.\[:]|>", target):
        candidates.append(page.locator(target))
    candidates += [page.get_by_role("button", name=target), page.get_by_role("link", name=target),
                   page.get_by_label(target), page.get_by_placeholder(target), page.get_by_role("option", name=target),
                   page.get_by_text(target, exact=True), page.get_by_text(target)]
    for loc in candidates:
        try:
            count = loc.count()
        except Exception:  # noqa: BLE001 - not a valid selector
            continue
        for i in range(min(count, 5)):
            item = loc.nth(i)
            if item.is_visible():
                return item
    return None


def _check_field(handle_or_locator):
    what = handle_or_locator.evaluate(FIELD_JS)
    if SECRET_FIELD.search(what):
        raise PermissionError("That field looks like a password or payment detail; the browser won't type into it.")


def _goto(page, url):
    reason = blocked_reason(url)
    if reason:
        raise PermissionError(f"Can't open that: {reason}.")
    response = page.goto(url, wait_until="domcontentloaded")
    if response is not None and response.status >= 400:  # many sites still show a page; the screenshot tells
        return [f"The server answered HTTP {response.status}; check the screenshot for what actually loaded."]
    return []


def _follow_new_tab(session, before):
    pages = session.context.pages
    if len(pages) > before:
        session.page = pages[-1]
        return ["That opened a new tab; you're on it now."]
    return []


def act(body):
    action = str(body.get("action") or "").lower()
    sid = body.get("session") or ""
    with _lock:
        _sweep()
        if action == "open" or (action == "goto" and sid not in _sessions):
            device = body.get("device") if body.get("device") in DEVICES else "desktop"
            session, sid = Session(device), uuid.uuid4().hex[:12]
            _sessions[sid] = session
        else:
            session = _sessions.get(sid)
            if session is None:
                return {"error": "That browser session has ended (closed or idle too long). Use action=open again."}
    session.used = time.time()
    page = session.page
    notes = []

    if action == "close":
        _sessions.pop(sid, None)
        session.close()
        return {"session": sid, "closed": True}
    if action in ("open", "goto"):
        url = str(body.get("url") or "")
        if not url:
            return {"error": "open needs a url", "session": sid}
        if not re.match(r"^[a-z]+://", url, re.I):
            url = "https://" + url
        notes += _goto(page, url)
    elif action == "click":
        before = len(session.context.pages)
        if body.get("x") is not None and body.get("y") is not None:
            page.mouse.click(float(body["x"]), float(body["y"]))
        else:
            element = _locate(page, str(body.get("target") or ""))
            if element is None:
                return {"error": f"Nothing visible matches '{body.get('target')}'. Use a label from elements, or x,y.",
                        **_snapshot(sid, session)}
            element.click()
        page.wait_for_timeout(600)
        notes += _follow_new_tab(session, before)
    elif action == "type":
        text = str(body.get("text") or "")
        if body.get("target"):
            element = _locate(page, str(body["target"]))
            if element is None:
                return {"error": f"No field matches '{body['target']}'.", **_snapshot(sid, session)}
            _check_field(element)
            element.fill(text)
        else:
            focused = page.evaluate_handle("() => document.activeElement")
            _check_field(focused)
            page.keyboard.type(text, delay=20)
        if body.get("submit"):
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
    elif action == "press":
        page.keyboard.press(str(body.get("key") or "Enter"))
        page.wait_for_timeout(500)
    elif action == "select":
        element = _locate(page, str(body.get("target") or "select"))
        if element is None:
            return {"error": f"No dropdown matches '{body.get('target')}'.", **_snapshot(sid, session)}
        element.select_option(label=str(body.get("text") or ""))
    elif action == "scroll":
        sign = -1 if str(body.get("direction") or "down").lower() == "up" else 1
        if body.get("target"):
            element = _locate(page, str(body["target"]))
            if element is not None:
                element.scroll_into_view_if_needed()
        else:
            page.mouse.wheel(0, sign * DEVICES[session.device]["viewport"]["height"] * 0.8)
        page.wait_for_timeout(400)
    elif action == "back":
        page.go_back()
    elif action == "forward":
        page.go_forward()
    elif action == "reload":
        page.reload()
    elif action != "screenshot":
        return {"error": f"Unknown action '{action}'.", "session": sid}
    return _snapshot(sid, session, notes)


@app.post("/act")
def act_route():
    body = request.get_json(force=True, silent=True) or {}
    try:
        result = _pool.submit(act, body).result(timeout=110)
    except PermissionError as exc:
        return jsonify(error=str(exc), session=body.get("session")), 200
    except Exception as exc:  # noqa: BLE001 - the caller (a model) reads this and tries another way
        message = str(exc).split("\n")[0][:400]
        return jsonify(error=f"{type(exc).__name__}: {message}", session=body.get("session")), 200
    return jsonify(result)


@app.get("/")
def health():
    return jsonify(ok=True, sessions=len(_sessions))
