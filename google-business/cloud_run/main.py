"""Google Business Profile review pipeline - the Trustpilot pipeline's twin.

Cloud Scheduler calls /monitor every 30 minutes. It pulls the reviews updated
in the last few days across every salon listing, and for each one not yet in
the Google Sheet it asks Gemini (Vertex AI) for a public reply suggestion, an
internal business suggestion and, for 1-3 stars, a review type; then it posts
a card to the reviews Google Chat space and appends a row to the Sheet.

Business Profile offers no webhook we can register from this project, so
polling is the ingestion path (the Trustpilot service's /monitor does the
same job as a backstop for its webhook).

Endpoints
  GET  /                    health
  GET  /monitor             poll + process new reviews (synchronous, ?days=N)
  POST /backfill            seed the Sheet with older reviews, no Chat posts
                            (X-Api-Token; ?days=N|all &ai=1 &limit=100; repeat
                            until the response says remaining=0)
  GET  /api/locations       listings with nickname, address, Maps link
  GET  /api/reviews         newest reviews across all listings (5 min cache)
  GET  /api/sheet           every Sheet row as JSON, for the Reviews Dashboard (2 min cache)
  GET  /api/summary         all-time review count + average per listing, from Google (1 h cache)
  POST /api/reply           {"review": "<review resource name>", "message": ...}
                            posts the public reply on Google; no key, but only
                            accepted from the dashboard's origin (ALLOWED_ORIGINS)

Env (Cloud Run): GOOGLE_BUSINESS_PROFILE_CLIENT_ID / _CLIENT_SECRET /
_REFRESH_TOKEN, GCHAT_WEBHOOK_URL and API_TOKEN come from Secret Manager;
GOOGLE_SHEET_ID and GBP_ACCOUNT are plain env vars. Vertex AI and Sheets use
the runtime service account (Application Default Credentials), so the Sheet
must be shared with it.
"""

import datetime as dt
import json
import os
import sys
import threading
import time

import requests
from flask import Flask, jsonify, request

# Local runs: pull the repo's gitignored .env so no secrets are exported by hand.
# The container has no lib/ folder, so this silently does nothing there.
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    from lib.secrets import load_dotenv  # noqa: E402
    load_dotenv(os.path.dirname(os.path.abspath(__file__)))
except Exception:  # noqa: BLE001
    pass

app = Flask(__name__)

CLIENT_ID = os.environ["GOOGLE_BUSINESS_PROFILE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_BUSINESS_PROFILE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GOOGLE_BUSINESS_PROFILE_REFRESH_TOKEN"]
GCHAT_WEBHOOK_URL = os.environ.get("GCHAT_WEBHOOK_URL", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
API_TOKEN = os.environ.get("API_TOKEN", "")
GBP_ACCOUNT = os.environ.get("GBP_ACCOUNT", "accounts/111445610944292236883")
MONITOR_WINDOW_DAYS = int(os.environ.get("MONITOR_WINDOW_DAYS", "3"))
# /api/reply carries no key (the dashboard asked for none), so it only answers browser calls made
# from these origins. A speed bump against other websites and casual scripts, not authentication:
# the Origin header can be forged by anything that isn't a browser.
ALLOWED_ORIGINS = {o.strip().rstrip("/") for o in os.environ.get(
    "ALLOWED_ORIGINS",
    "https://reviews-dashboard-304363458561.us-central1.run.app,"
    "https://reviews-dashboard-onvg62bzra-uc.a.run.app,"
    "http://localhost:8765,http://127.0.0.1:8765").split(",") if o.strip()}

GCP_PROJECT = "shp-ai-bot-2026"
VERTEX_LOCATION = "us-central1"
VERTEX_MODEL = "gemini-2.5-pro"

TOKEN_URL = "https://oauth2.googleapis.com/token"
V4 = "https://mybusiness.googleapis.com/v4"
BUSINESS_INFO_API = "https://mybusinessbusinessinformation.googleapis.com/v1"
LOCATION_READ_MASK = "name,title,storefrontAddress,metadata"

STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}

# Salon nicknames from CLAUDE.md, keyed by location id. Ids are not secrets.
NICKNAMES = {
    "11580068753340767001": "Dufferin",
    "7223301939465497732": "Rapistan",
    "10905129003754891303": "STC",
    "685188567892497037": "Eglinton",
    "13689417356433350520": "Ridgeway",
    "9719921915312996452": "Consumer",
    "9984412715850155494": "Brampton",
    "16676111479862273432": "Pembroke Pines",
    "11640499775623985658": "New York",
    "8200285945777290212": "New York (Gen'C Hair Center)",
    "5108674717103047486": "Deerfield Beach",
    "8588506284493313246": "Sunrise FL",
    "10496215448163535265": "Madrid",
    "9083563788300132682": "Diemen NL",
    "2286575413498793421": "Eglinton (Gen'C Beauty)",
    "16570189707651721536": "Rapistan (Gen'C Beauty)",
    "6692803018046926984": "Dufferin (Gen'C Beauty)",
}

SHEET_HEADERS = [
    "Date", "Customer Name", "Location", "Star Rating", "Type", "Comment",
    "Reply Suggestion", "Business Suggestion", "Remark", "Review ID",
    "Reply Posted At", "Review Name", "Reply Text",
]
SHEET_RANGE = "A:M"
COL_REVIEW_ID = 9      # J, zero-based index in a row
COL_REPLY_AT = 10      # K
COL_REVIEW_NAME = 11   # L
COL_REPLY_TEXT = 12    # M  owner reply currently on Google, kept in sync by /monitor

# ---------------------------------------------------------------------------
# Business Profile auth + HTTP
# ---------------------------------------------------------------------------
_gbp_token = None
_gbp_token_expiry = 0.0
_gbp_lock = threading.Lock()


def get_gbp_token():
    """Mint an access token from the refresh token; cached until a minute before expiry."""
    global _gbp_token, _gbp_token_expiry
    with _gbp_lock:
        if _gbp_token and time.time() < _gbp_token_expiry:
            return _gbp_token
        r = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN,
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        }, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"token refresh failed: {r.status_code} {r.text[:200]}")
        data = r.json()
        _gbp_token = data["access_token"]
        _gbp_token_expiry = time.time() + int(data.get("expires_in", 3600)) - 60
        return _gbp_token


def gbp(method, url, params=None, payload=None, timeout=60):
    headers = {"Authorization": f"Bearer {get_gbp_token()}"}
    r = requests.request(method, url, params=params, json=payload, headers=headers, timeout=timeout)
    if r.status_code == 401:  # token revoked mid-run: mint again and retry once
        global _gbp_token
        _gbp_token = None
        headers["Authorization"] = f"Bearer {get_gbp_token()}"
        r = requests.request(method, url, params=params, json=payload, headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"GBP {method} {url.split('/v')[-1][:80]} -> {r.status_code} {r.text[:300]}")
    return r.json() if r.text else {}


# ---------------------------------------------------------------------------
# Locations (cached for an hour) and reviews
# ---------------------------------------------------------------------------
_locations = None
_locations_expiry = 0.0


def get_locations():
    """{location_id: {id, name, title, label, locality, maps_url}}"""
    global _locations, _locations_expiry
    if _locations and time.time() < _locations_expiry:
        return _locations
    out, params = {}, {"readMask": LOCATION_READ_MASK, "pageSize": 100}
    while True:
        body = gbp("GET", f"{BUSINESS_INFO_API}/{GBP_ACCOUNT}/locations", params=params)
        for loc in body.get("locations", []):
            loc_id = loc["name"].split("/")[-1]
            address = loc.get("storefrontAddress") or {}
            locality = address.get("locality", "")
            nickname = NICKNAMES.get(loc_id) or locality or loc_id
            out[loc_id] = {
                "id": loc_id,
                "name": f"{GBP_ACCOUNT}/locations/{loc_id}",   # v4 resource name
                "title": loc.get("title", ""),
                "locality": locality,
                "label": f"{nickname} - {loc.get('title', '')}",
                "maps_url": (loc.get("metadata") or {}).get("mapsUri", ""),
            }
        if not body.get("nextPageToken"):
            break
        params["pageToken"] = body["nextPageToken"]
    _locations, _locations_expiry = out, time.time() + 3600
    return out


def iter_reviews(newest_first=True):
    """Yield (location_info, review) across every listing, newest-updated first.
    batchGetReviews takes at most 50 locations per call; we have 17."""
    locations = get_locations()
    names = [loc["name"] for loc in locations.values()]
    for start in range(0, len(names), 50):
        payload = {"locationNames": names[start:start + 50], "pageSize": 50,
                   "orderBy": "updateTime desc" if newest_first else "rating"}
        while True:
            body = gbp("POST", f"{V4}/{GBP_ACCOUNT}/locations:batchGetReviews", payload=payload)
            for item in body.get("locationReviews", []):
                loc_id = item["name"].split("/")[-1]
                yield locations.get(loc_id, {"id": loc_id, "name": item["name"], "title": "", "label": loc_id, "maps_url": ""}), item["review"]
            token = body.get("nextPageToken")
            if not token:
                break
            payload["pageToken"] = token


def parse_time(value):
    try:
        return dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def format_review_date(value):
    """2026-09-30T19:30:51Z -> 'Sep 30, 2026' (portable, no %-d)."""
    t = parse_time(value)
    return f"{t.strftime('%b')} {t.day}, {t.year}" if t else (value or "")[:10]


def review_fields(review):
    reviewer = review.get("reviewer") or {}
    name = "Anonymous" if reviewer.get("isAnonymous") else (reviewer.get("displayName") or "A customer")
    rating = STARS.get(review.get("starRating"), 0)
    comment = (review.get("comment") or "").strip()
    reply = review.get("reviewReply") or {}
    return {
        "name": review["name"],
        "id": review.get("reviewId") or review["name"].split("/")[-1],
        "reviewer": name,
        "rating": rating,
        "comment": comment,
        "created": review.get("createTime", ""),
        "updated": review.get("updateTime", ""),
        "reply_url": review.get("reviewReplyUrl", ""),
        "replied_at": reply.get("updateTime", ""),
        "reply": reply.get("comment", ""),
    }


# ---------------------------------------------------------------------------
# Google Sheet (same shape as the Trustpilot sheet, Location in place of Email)
# ---------------------------------------------------------------------------
def get_sheets_service():
    import google.auth
    from googleapiclient.discovery import build
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_sheet():
    """Returns (rows, {review_id: 1-based row number}, {review_id: (reply posted at, reply text)}).
    Writes the header row when the sheet is empty or predates a newly added column."""
    service = get_sheets_service()
    rows = service.spreadsheets().values().get(spreadsheetId=GOOGLE_SHEET_ID, range=SHEET_RANGE).execute().get("values", [])
    if not rows or len(rows[0]) < len(SHEET_HEADERS):
        service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID, range="A1", valueInputOption="RAW",
            body={"values": [SHEET_HEADERS]}).execute()
        rows = [SHEET_HEADERS] + rows[1:]
    cell = lambda row, col: row[col].strip() if len(row) > col else ""  # noqa: E731
    index, replied = {}, {}
    for i, row in enumerate(rows[1:], start=2):
        rid = cell(row, COL_REVIEW_ID)
        if rid:
            index[rid] = i
            replied[rid] = (cell(row, COL_REPLY_AT), cell(row, COL_REPLY_TEXT))
    return rows, index, replied


def append_rows(rows):
    if not rows:
        return
    get_sheets_service().spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID, range=SHEET_RANGE, valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS", body={"values": rows}).execute()


def write_reply_cells(updates):
    """updates: [(row_number, replied_at, reply_text)] -> columns K and M, 200 rows per call.
    RAW so reply text that starts with = or + is stored as text, not a formula."""
    for start in range(0, len(updates), 200):
        data = []
        for row_number, replied_at, text in updates[start:start + 200]:
            data.append({"range": f"K{row_number}", "values": [[replied_at]]})
            data.append({"range": f"M{row_number}", "values": [[text]]})
        get_sheets_service().spreadsheets().values().batchUpdate(
            spreadsheetId=GOOGLE_SHEET_ID, body={"valueInputOption": "RAW", "data": data}).execute()


def stamp_reply(row_number, replied_at, text=""):
    write_reply_cells([(row_number, replied_at, text)])


def sheet_row(f, location, review_type, reply, suggestion):
    return [
        format_review_date(f["created"]),   # A Date the customer wrote the review
        f["reviewer"],                      # B Customer Name
        location["label"],                  # C Location (Trustpilot keeps Email here)
        f["rating"],                        # D Star Rating
        review_type,                        # E Type (AI, 1-3 stars only)
        f["comment"],                       # F Comment
        reply,                              # G Reply Suggestion
        suggestion,                         # H Business Suggestion
        "",                                 # I Remark (manual)
        f["id"],                            # J Review ID (dedup key)
        format_review_date(f["replied_at"]) if f["replied_at"] else "",  # K Reply Posted At
        f["name"],                          # L Review resource name (for /api/reply)
        f["reply"],                         # M Reply Text (owner reply on Google)
    ]


# ---------------------------------------------------------------------------
# Gemini on Vertex AI - same prompts as the Trustpilot service, Google wording
# ---------------------------------------------------------------------------
def get_vertex_token():
    import google.auth
    import google.auth.transport.requests
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def vertex_call(prompt, temperature=0.4):
    url = (f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT}"
           f"/locations/{VERTEX_LOCATION}/publishers/google/models/{VERTEX_MODEL}:generateContent")
    r = requests.post(url, headers={"Authorization": f"Bearer {get_vertex_token()}"},
                      json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                            "generationConfig": {"temperature": temperature}}, timeout=120)
    parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    for part in parts:
        if part.get("text"):
            return part["text"].strip()
    return ""


# Same categories, spelled exactly the same, as the Trustpilot service's ISSUE_TYPE_TAXONOMY, so
# Trustpilot and Google reviews can be compared. Every review gets one, whatever its rating.
ISSUE_TYPES = [
    "Positive Experience",
    "Product Defective - Stock",
    "Product Defective - Custom Made",
    "Product Defective - Salon Finished",
    "Product Quality",
    "Color & Appearance",
    "Fit & Comfort",
    "Adhesive & Hold",
    "Shipping & Delivery",
    "Customer Support",
    "Customer Expectation",
    "Value for Money",
    "Returns & Refunds",
    "Other",
]
RATING_ONLY = "Rating only"   # stars with no text: nothing to classify, shown as its own slice
# Labels the first version of this service wrote; /maintenance/classify may replace them. Any other
# value in column E is treated as a manual edit and never overwritten.
LEGACY_AI_TYPES = {"customer support", "shipping", "product defective - stock",
                   "product defective - custom", "product defective - salon finished"}

ISSUE_GUIDANCE = (
    "Definitions (these are Google reviews of hair-replacement salons as well as online orders):\n"
    "- Product Defective - Stock: a ready-made hairpiece arrived faulty or not as described\n"
    "- Product Defective - Custom Made: a custom-ordered hairpiece did not match the order, including "
    "being sent a stock unit instead\n"
    "- Product Defective - Salon Finished: problem with salon work: haircut, base cut, installation, "
    "maintenance, styling\n"
    "- Product Quality: hair quality, lifespan, shedding or frizz in general, not one faulty unit\n"
    "- Color & Appearance: colour match or an unnatural look\n"
    "- Fit & Comfort: size, fit, comfort of the hairpiece\n"
    "- Adhesive & Hold: tapes, glues, how long the bond holds\n"
    "- Shipping & Delivery: delays, tracking, lost or misdelivered parcels\n"
    "- Customer Support: staff attitude, communication, responsiveness, bookings, opening hours, "
    "in-store experience, billing or payment practices\n"
    "- Customer Expectation: the experience fell short of what was promised, without a clear defect\n"
    "- Value for Money: prices, price increases, not worth the cost\n"
    "- Returns & Refunds: return, exchange or refund problems\n"
    "- Positive Experience: the review is positive and names no problem, whatever it praises "
    "(staff, stylist, support, product, price); this fits most 4-5 star reviews\n"
    "Every category except Positive Experience describes a PROBLEM. Never use Customer Support, "
    "Product Quality, Value for Money or any other problem category for praise; use one only when "
    "the review complains about that area, at any star rating.\n"
    "- Other: none of the above\n"
)


def canonical_issue_type(raw):
    v = (raw or "").strip().strip("\"' `*\n\r\t.")
    for t in ISSUE_TYPES:
        if v.lower() == t.lower():
            return t
    return ""


def review_text_for_ai(comment):
    """Google appends '(Translated by Google) ... (Original) ...' to non-English reviews; the
    translation is what the classifier should read."""
    c = comment or ""
    i = c.find("(Translated by Google)")
    if i >= 0:
        c = c[i + len("(Translated by Google)"):]
        j = c.find("(Original)")
        if j >= 0:
            c = c[:j]
    return c.strip()


def classify_reviews(items, batch=25):
    """items: [(rating, comment)] -> [category] in the same order, one Gemini call per `batch` reviews.
    Anything the model returns off-list is retried on its own, then falls back to Other."""
    out = []
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        numbered = "\n".join(f"{i + 1}. [{rating}★] {review_text_for_ai(c)[:900]}" for i, (rating, c) in enumerate(chunk))
        prompt = (
            "You are classifying Google reviews (1-5 stars) of Superhairpieces, a hairpiece "
            "company with its own hair-replacement salons.\n\n"
            "For each numbered review pick the ONE most fitting category, exactly as written:\n"
            + "\n".join(f"- {t}" for t in ISSUE_TYPES) + "\n\n" + ISSUE_GUIDANCE +
            "\nPick the most specific category that applies.\n\n"
            f"Reviews:\n{numbered}\n\n"
            "Return STRICT JSON: an array of category strings with exactly the same order and length "
            "as the reviews. No markdown, no explanation."
        )
        labels = []
        try:
            raw = (vertex_call(prompt, temperature=0) or "").strip()
            first, last = raw.find("["), raw.rfind("]")
            parsed = json.loads(raw[first:last + 1]) if first >= 0 and last > first else []
            if isinstance(parsed, list) and len(parsed) == len(chunk):
                labels = [canonical_issue_type(str(x)) for x in parsed]
            else:
                print(f"classify: batch returned {len(parsed) if isinstance(parsed, list) else 'non-list'} for {len(chunk)}")
        except Exception as e:  # noqa: BLE001
            print(f"classify: batch error {e}")
        if len(labels) != len(chunk):
            labels = [""] * len(chunk)
        for i, label in enumerate(labels):
            if not label:
                labels[i] = get_review_type(chunk[i][1], chunk[i][0]) or "Other"
        out.extend(labels)
    return out


def get_review_type(comment, rating=1):
    """Single review -> category ("" on a Vertex error). Used for new reviews in /monitor."""
    prompt = (
        f"A customer left a {rating}-star Google review of Superhairpieces, a hairpiece company with "
        "its own hair-replacement salons.\n"
        f"Review: \"{review_text_for_ai(comment)[:1500]}\"\n\n"
        "Classify into ONE category. Output ONLY the category name exactly as written below, "
        "no quotes, no punctuation, no explanation.\n\n"
        "Categories:\n" + "\n".join(f"- {t}" for t in ISSUE_TYPES) + "\n\n" + ISSUE_GUIDANCE
    )
    try:
        return canonical_issue_type(vertex_call(prompt, temperature=0)) or "Other"
    except Exception as e:  # noqa: BLE001
        print(f"Vertex AI type error: {e}")
    return ""


def get_reply_suggestion(comment, rating, name, location):
    prompt = (
        f"A customer named {name} left a {rating}-star Google review of the Superhairpieces "
        f"salon at {location['label']} with the following comment:\n\"{comment}\"\n\n"
        "Write a warm, professional public reply from Superhairpieces to post on Google. "
        "Keep it to 2-3 sentences. Thank them, address their specific feedback, and invite "
        "them to reach out if needed. If a staff member is named, acknowledge them. "
        "Do not use generic filler phrases."
    )
    try:
        return vertex_call(prompt)
    except Exception as e:  # noqa: BLE001
        print(f"Vertex AI reply error: {e}")
    return ""


def get_business_suggestion(comment, rating, location):
    prompt = (
        f"A customer left a {rating}-star Google review of our hairpiece salon "
        f"({location['label']}, Superhairpieces) with the following comment:\n\"{comment}\"\n\n"
        "Provide a brief 1-2 sentence actionable improvement suggestion for our internal team. "
        "If the review is fully positive, suggest a quick way to capitalise on it. "
        "Be concise and professional."
    )
    try:
        return vertex_call(prompt)
    except Exception as e:  # noqa: BLE001
        print(f"Vertex AI suggestion error: {e}")
    return "AI suggestion unavailable."


# ---------------------------------------------------------------------------
# Google Chat card - same layout as the Trustpilot card
# ---------------------------------------------------------------------------
def send_to_gchat(f, location, reply, suggestion):
    if not GCHAT_WEBHOOK_URL:
        print("Chat: no webhook configured, skipping")
        return
    stars = "⭐" * min(f["rating"], 5)
    buttons = []
    if f["reply_url"]:
        buttons.append({"text": "Reply on Google", "onClick": {"openLink": {"url": f["reply_url"]}},
                        "color": {"red": 0.0, "green": 0.478, "blue": 1.0, "alpha": 1.0}})
    if location.get("maps_url"):
        buttons.append({"text": "View on Maps", "onClick": {"openLink": {"url": location["maps_url"]}}})
    card = {"cardsV2": [{"cardId": f["id"], "card": {"sections": [
        {"widgets": [{"textParagraph": {"text": (
            f"{stars} <b>New Google Review</b> {stars}<br><br>"
            f"<b>Customer:</b> {f['reviewer']}<br>"
            f"<b>Location:</b> {location['label']}<br>"
            f"<b>Rating:</b> {f['rating']}-star<br>"
            f"<b>Date:</b> {format_review_date(f['created'])}")}}]},
        {"header": "Comment", "widgets": [{"textParagraph": {"text": f["comment"] or "—"}}]},
        {"header": "\U0001f4ac Reply Suggestion", "widgets": [{"textParagraph": {"text": reply or "—"}}]},
        {"header": "\U0001f4a1 Business Suggestion", "widgets": [
            {"textParagraph": {"text": suggestion or "—"}},
            *([{"buttonList": {"buttons": buttons}}] if buttons else []),
        ]},
    ]}}]}
    try:
        r = requests.post(GCHAT_WEBHOOK_URL, json=card, timeout=10)
        if r.status_code != 200:
            print(f"Chat webhook returned {r.status_code} {r.text[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"Google Chat error: {e}")


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
def enrich(f, location, with_ai=True):
    has_comment = len(f["comment"]) > 5 and with_ai
    reply = get_reply_suggestion(f["comment"], f["rating"], f["reviewer"], location) if has_comment else ""
    suggestion = get_business_suggestion(f["comment"], f["rating"], location) if has_comment else ""
    review_type = ""
    if f["rating"] and with_ai:
        review_type = get_review_type(f["comment"], f["rating"]) if len(f["comment"]) > 5 else RATING_ONLY
    return reply, suggestion, review_type


_monitor_lock = threading.Lock()
_last_monitor = {"started": None, "finished": None, "result": None}


def run_monitor(window_days, notify=True):
    """One pass over every review on every listing (~22 API calls for ~1,000 reviews):
    - not in the Sheet and updated within the window: AI suggestions, Chat card, new row;
    - not in the Sheet and older (missed earlier): appended quietly, no AI, no Chat;
    - already in the Sheet: columns K and M follow the owner reply on Google (posted,
      edited or deleted). Google does not bump a review's updateTime when the owner
      replies, so reply state has to be compared across all reviews, not the window."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)
    _, index, replied = read_sheet()
    checked = processed = quiet = 0
    new_rows, reply_updates = [], []
    for location, review in iter_reviews(newest_first=True):
        f = review_fields(review)
        checked += 1
        if f["id"] in index:
            row_number = index[f["id"]]
            if row_number is True:      # appended earlier in this same pass
                continue
            want_at = format_review_date(f["replied_at"]) if f["replied_at"] else ""
            want_text = (f["reply"] or "").strip()
            have_at, have_text = replied.get(f["id"], ("", ""))
            if parse_sheet_date(have_at) != parse_sheet_date(want_at) or have_text != want_text:
                reply_updates.append((row_number, want_at, want_text))
            continue
        updated = parse_time(f["updated"])
        if not updated or updated >= cutoff:
            reply, suggestion, review_type = enrich(f, location)
            if notify:
                send_to_gchat(f, location, reply, suggestion)
            processed += 1
            print(f"Monitor: new {f['rating']}-star review by {f['reviewer']} at {location['label']} ({f['id'][:12]})")
        else:
            reply = suggestion = review_type = ""
            quiet += 1
        new_rows.append(sheet_row(f, location, review_type, reply, suggestion))
        index[f["id"]] = True
    append_rows(new_rows)
    write_reply_cells(reply_updates)
    if reply_updates:
        print(f"Monitor: synced reply state on {len(reply_updates)} rows")
    return {"checked": checked, "new": processed, "appended_quietly": quiet,
            "replies_synced": len(reply_updates), "window_days": window_days}


def run_backfill(days, with_ai, limit=100):
    """Seed the Sheet with reviews it doesn't have (oldest first), never posting to
    Chat. Processes at most `limit` reviews per call so each request stays well
    inside the Cloud Run timeout; the response says how many remain."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days) if days else None
    _, index, _ = read_sheet()
    pending = []
    for location, review in iter_reviews(newest_first=True):
        f = review_fields(review)
        created = parse_time(f["created"])
        if cutoff and created and created < cutoff:
            continue
        if f["id"] in index:
            continue
        pending.append((location, f))
    pending.sort(key=lambda p: p[1]["created"])
    batch, remaining = pending[:limit], max(len(pending) - limit, 0)
    rows = []
    for location, f in batch:
        reply, suggestion, review_type = enrich(f, location, with_ai=with_ai)
        rows.append(sheet_row(f, location, review_type, reply, suggestion))
        if len(rows) >= 25:
            append_rows(rows)
            rows = []
    append_rows(rows)
    return {"added": len(batch), "remaining": remaining, "ai": with_ai, "days": days or "all"}


def run_dedupe(apply=False):
    """Remove duplicate rows for the same review, keeping the LAST copy (the one /monitor keeps in
    sync). A copy is only removed when its manual columns (E Type, I Remark) add nothing the kept
    row lacks; otherwise it is reported and left alone. Dry run unless apply=True."""
    service = get_sheets_service()
    meta = service.spreadsheets().get(spreadsheetId=GOOGLE_SHEET_ID,
                                      fields="sheets(properties(sheetId,index))").execute()
    sheet_id = sorted(meta["sheets"], key=lambda x: x["properties"]["index"])[0]["properties"]["sheetId"]
    rows, _, _ = read_sheet()
    cell = lambda row, col: row[col].strip() if len(row) > col else ""  # noqa: E731
    last = {}
    for i, row in enumerate(rows[1:], start=2):
        if cell(row, COL_REVIEW_ID):
            last[cell(row, COL_REVIEW_ID)] = i
    drop, kept_for_edits = [], []
    for i, row in enumerate(rows[1:], start=2):
        rid = cell(row, COL_REVIEW_ID)
        if not rid or last[rid] == i:
            continue
        keep = rows[last[rid] - 1]
        manual = [c for c in (4, 8) if cell(row, c) and cell(row, c) != cell(keep, c)]
        (kept_for_edits if manual else drop).append(i)
    if apply and drop:
        requests = [{"deleteDimension": {"range": {"sheetId": sheet_id, "dimension": "ROWS",
                                                   "startIndex": r - 1, "endIndex": r}}}
                    for r in sorted(drop, reverse=True)]
        service.spreadsheets().batchUpdate(spreadsheetId=GOOGLE_SHEET_ID, body={"requests": requests}).execute()
    return {"duplicates": len(drop), "rows": drop, "left_because_of_manual_edits": kept_for_edits,
            "applied": bool(apply)}


def run_classify(limit=200, reclassify=False):
    """Fill column E (Type) for every rated row: written reviews get an ISSUE_TYPES category from
    Gemini, star-only ones get RATING_ONLY. Only empty cells and labels this service wrote earlier
    (LEGACY_AI_TYPES, or any ISSUE_TYPES value when reclassify=True) are written; anything else in
    E is a manual edit and is left alone."""
    rows, _, _ = read_sheet()
    cell = lambda row, col: row[col].strip() if len(row) > col else ""  # noqa: E731
    todo = []
    for i, row in enumerate(rows[1:], start=2):
        stars = cell(row, 3)
        if not stars.isdigit() or not 1 <= int(stars) <= 5:
            continue
        current = cell(row, 4)
        # Exact match: the old labels were lowercase, and their lowercased forms collide with
        # current ISSUE_TYPES values ("customer support" vs "Customer Support").
        replaceable = (not current or current in LEGACY_AI_TYPES
                       or (reclassify and (current in ISSUE_TYPES or current == RATING_ONLY)))
        if replaceable:
            todo.append((i, int(stars), cell(row, 5), current))
    batch, remaining = todo[:limit], max(len(todo) - limit, 0)
    written = [(i, RATING_ONLY) for i, _, c, _ in batch if len(c) <= 5]
    texts = [(i, stars, c) for i, stars, c, _ in batch if len(c) > 5]
    labels = classify_reviews([(stars, c) for _, stars, c in texts]) if texts else []
    written += [(i, label) for (i, _, _), label in zip(texts, labels)]
    data = [{"range": f"E{i}", "values": [[label]]} for i, label in written]
    for start in range(0, len(data), 200):
        get_sheets_service().spreadsheets().values().batchUpdate(
            spreadsheetId=GOOGLE_SHEET_ID,
            body={"valueInputOption": "RAW", "data": data[start:start + 200]}).execute()
    counts = {}
    for _, label in written:
        counts[label] = counts.get(label, 0) + 1
    return {"classified": len(texts), "rating_only": len(written) - len(texts), "remaining": remaining,
            "by_type": dict(sorted(counts.items(), key=lambda kv: -kv[1]))}


def run_locked(fn, *args):
    """Run a job inside the request (Cloud Run only guarantees CPU while a request
    is open), one at a time. Returns (http_status, payload)."""
    if not _monitor_lock.acquire(blocking=False):
        return 409, {"status": "busy", "last_run": _last_monitor}
    _last_monitor["started"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    try:
        result = fn(*args)
        _last_monitor["result"] = result
        return 200, {"status": "ok", **result}
    except Exception as e:  # noqa: BLE001
        _last_monitor["result"] = {"error": str(e)}
        print(f"{fn.__name__} error: {e}")
        return 500, {"status": "error", "message": str(e)}
    finally:
        _last_monitor["finished"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        _monitor_lock.release()


def authorized():
    return bool(API_TOKEN) and request.headers.get("X-Api-Token", "") == API_TOKEN


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.after_request
def add_cors(response):
    if request.path.startswith("/api/"):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Api-Token"
    return response


@app.route("/")
def health():
    return jsonify({"service": "gbp-reviews", "account": GBP_ACCOUNT, "last_run": _last_monitor})


@app.route("/monitor", methods=["GET", "POST"])
def monitor():
    days = int(request.args.get("days", MONITOR_WINDOW_DAYS))
    status, payload = run_locked(run_monitor, days)
    return jsonify(payload), status


@app.route("/backfill", methods=["POST"])
def backfill():
    """Seed older reviews into the Sheet in batches; call until `remaining` is 0."""
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401
    days_arg = request.args.get("days", "all")
    days = 0 if days_arg == "all" else int(days_arg)
    with_ai = request.args.get("ai") == "1"
    limit = int(request.args.get("limit", 100))
    status, payload = run_locked(run_backfill, days, with_ai, limit)
    return jsonify(payload), status


@app.route("/maintenance/classify", methods=["POST"])
def maintenance_classify():
    """Classify rows into column E (X-Api-Token). ?limit=200, ?reclassify=1 to redo AI labels."""
    global _sheet_cache
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401
    status, payload = run_locked(run_classify, int(request.args.get("limit", 200)),
                                 request.args.get("reclassify") == "1")
    _sheet_cache = None
    return jsonify(payload), status


@app.route("/maintenance/dedupe", methods=["POST"])
def maintenance_dedupe():
    """Duplicate-row cleanup (X-Api-Token). Dry run by default; ?apply=1 deletes."""
    global _sheet_cache
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401
    status, payload = run_locked(run_dedupe, request.args.get("apply") == "1")
    _sheet_cache = None
    return jsonify(payload), status


@app.route("/api/locations", methods=["GET", "OPTIONS"])
def api_locations():
    if request.method == "OPTIONS":
        return ("", 204)
    return jsonify(list(get_locations().values()))


_reviews_cache = None
_reviews_cache_expiry = 0.0


@app.route("/api/reviews", methods=["GET", "OPTIONS"])
def api_reviews():
    """Newest 50 reviews across all listings, with the location label. Cached 5 minutes."""
    global _reviews_cache, _reviews_cache_expiry
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        if _reviews_cache and time.time() < _reviews_cache_expiry:
            return jsonify(_reviews_cache)
        out = []
        for location, review in iter_reviews(newest_first=True):
            f = review_fields(review)
            f["location"] = location["label"]
            f["location_id"] = location["id"]
            f["maps_url"] = location.get("maps_url", "")
            out.append(f)
            if len(out) >= 50:
                break
        _reviews_cache, _reviews_cache_expiry = {"reviews": out}, time.time() + 300
        return jsonify(_reviews_cache)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


_sheet_cache = None
_sheet_cache_expiry = 0.0


def parse_sheet_date(value):
    """'Sep 30, 2026' (what sheet_row writes) -> '2026-09-30'; anything else passes through."""
    for fmt in ("%b %d, %Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            continue
    return value


@app.route("/api/sheet", methods=["GET", "OPTIONS"])
def api_sheet():
    """The Google Business Profile Reviews sheet as JSON. The dashboard reads the sheet through
    this (the runtime identity is a writer on it) instead of the public gviz endpoint, so the
    sheet never has to be shared with anyone-with-the-link."""
    global _sheet_cache, _sheet_cache_expiry
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        if _sheet_cache and time.time() < _sheet_cache_expiry:
            return jsonify(_sheet_cache)
        rows, _, _ = read_sheet()
        out = []
        for i, row in enumerate(rows[1:], start=2):
            row = (row + [""] * len(SHEET_HEADERS))[:len(SHEET_HEADERS)]
            date, name, location, stars, rtype, comment, reply, biz, remark, rid, replied_at, rname, reply_text = row
            if not rid:
                continue
            out.append({
                "id": rid, "sheet_row": i, "created": parse_sheet_date(date), "name": name,
                "location": location, "stars": int(stars) if str(stars).strip().isdigit() else 0,
                "type": rtype, "comment": comment, "reply_suggestion": reply, "business_suggestion": biz,
                "remark": remark, "replied_at": parse_sheet_date(replied_at) if replied_at else "",
                "review_name": rname, "reply_text": reply_text,
            })
        _sheet_cache, _sheet_cache_expiry = {"rows": out, "sheet_id": GOOGLE_SHEET_ID}, time.time() + 120
        return jsonify(_sheet_cache)
    except Exception as e:  # noqa: BLE001
        print(f"api/sheet error: {e}")
        return jsonify({"error": str(e)}), 500


_summary_cache = None
_summary_cache_expiry = 0.0


@app.route("/api/summary", methods=["GET", "OPTIONS"])
def api_summary():
    """All-time review count and average rating per listing, straight from Google (the sheet
    only holds what we've ingested). One reviews.list call per listing; cached an hour."""
    global _summary_cache, _summary_cache_expiry
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        if _summary_cache and time.time() < _summary_cache_expiry:
            return jsonify(_summary_cache)
        locations, total, weighted = [], 0, 0.0
        for loc in get_locations().values():
            body = gbp("GET", f"{V4}/{loc['name']}/reviews", params={"pageSize": 1})
            count = int(body.get("totalReviewCount") or 0)
            avg = float(body.get("averageRating") or 0)
            total += count
            weighted += avg * count
            locations.append({"id": loc["id"], "label": loc["label"], "title": loc["title"],
                              "locality": loc.get("locality", ""), "maps_url": loc.get("maps_url", ""),
                              "total": count, "average": round(avg, 2) if count else None})
        locations.sort(key=lambda l: -l["total"])
        _summary_cache = {"total": total, "average": round(weighted / total, 2) if total else None,
                          "locations": locations, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        _summary_cache_expiry = time.time() + 3600
        return jsonify(_summary_cache)
    except Exception as e:  # noqa: BLE001
        print(f"api/summary error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/reply", methods=["POST", "OPTIONS"])
def api_reply():
    """Post (or replace) the public company reply on a Google review."""
    global _reviews_cache, _sheet_cache
    if request.method == "OPTIONS":
        return ("", 204)
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if origin not in ALLOWED_ORIGINS:
        print(f"reply refused: origin {origin or '(none)'!r} not allowed")
        return jsonify({"error": "replies are only accepted from the Reviews Dashboard"}), 403
    body = request.get_json(silent=True) or {}
    review_name = (body.get("review") or "").strip()
    message = (body.get("message") or "").strip()
    if not review_name.startswith(f"{GBP_ACCOUNT}/locations/") or "/reviews/" not in review_name:
        return jsonify({"error": "review must be the full resource name (column L of the sheet)"}), 400
    if not message:
        return jsonify({"error": "Reply can't be empty"}), 400
    if len(message) > 4096:
        return jsonify({"error": "Reply too long (4096 char max)"}), 400
    try:
        result = gbp("PUT", f"{V4}/{review_name}/reply", payload={"comment": message})
        _reviews_cache = None
        _sheet_cache = None
        posted = result.get("updateTime", "")
        try:
            _, index, _ = read_sheet()
            rid = review_name.split("/")[-1]
            if rid in index:
                stamp_reply(index[rid], format_review_date(posted) if posted else "posted", message)
        except Exception as e:  # noqa: BLE001
            print(f"reply stamped on Google but not in sheet: {e}")
        return jsonify({"ok": True, "reply": result})
    except Exception as e:  # noqa: BLE001
        print(f"reply error: {e}")
        return jsonify({"error": str(e)}), 502


# ---------------------------------------------------------------------------
# Local CLI: python main.py monitor [--days N] [--dry-run]   (no Chat, no Sheet)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["monitor", "locations", "reviews"])
    ap.add_argument("--days", type=int, default=MONITOR_WINDOW_DAYS)
    ap.add_argument("--dry-run", action="store_true", help="skip Chat and Sheet; print what would be processed")
    ap.add_argument("--ai", action="store_true", help="with --dry-run, still call Gemini for the first review")
    args = ap.parse_args()
    if args.command == "locations":
        for loc in get_locations().values():
            print(f"{loc['id']:<22} {loc['label']}")
    elif args.command == "reviews":
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
        for location, review in iter_reviews():
            f = review_fields(review)
            if parse_time(f["updated"]) < cutoff:
                break
            print(f"{format_review_date(f['created']):<13} {f['rating']}* {location['label']:<40} {f['reviewer']:<25} replied={'y' if f['replied_at'] else 'n'}  {f['comment'][:60]!r}")
    elif args.dry_run:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
        shown = 0
        for location, review in iter_reviews():
            f = review_fields(review)
            if parse_time(f["updated"]) < cutoff:
                break
            print(f"would process: {f['rating']}* {f['reviewer']} at {location['label']}: {f['comment'][:80]!r}")
            if args.ai and shown == 0 and f["comment"]:
                reply, suggestion, review_type = enrich(f, location)
                print("  reply      :", reply)
                print("  suggestion :", suggestion)
                print("  type       :", review_type or "(n/a)")
                print("  sheet row  :", sheet_row(f, location, review_type, reply, suggestion)[:5])
            shown += 1
    else:
        print(json.dumps(run_monitor(args.days), indent=2))
