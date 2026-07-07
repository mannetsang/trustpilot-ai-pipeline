import os
import base64
import json
import time
import threading
import requests
from flask import Flask, request, jsonify
import google.auth
import google.auth.transport.requests
from googleapiclient.discovery import build

app = Flask(__name__)

GCHAT_WEBHOOK_URL = os.environ["GCHAT_WEBHOOK_URL"]
GOOGLE_SHEET_ID   = os.environ["GOOGLE_SHEET_ID"]
TP_API_KEY        = os.environ["TP_API_KEY"]
TP_SECRET         = os.environ["TP_SECRET"]
BU_ID                = "5e44f707d7d8c700011eaa10"
# Business user that company replies are attributed to. Trustpilot requires this when
# replying via Application identity (client_credentials grant). Currently Manne's user.
TP_AUTHOR_BUSINESS_USER_ID = "69e690a0b3b585ed447f64e3"
GCP_PROJECT          = "shp-ai-bot-2026"
VERTEX_LOCATION      = "us-central1"
VERTEX_MODEL         = "gemini-2.5-pro"
INVITATIONS_SHEET_ID = "193E74iZIvF1X3rvDfbEEObqaVOQFRTK8C0iRIReYTnw"

# Cache the Trustpilot access token so we don't re-fetch on every request
_tp_token = None
_tp_token_expiry = 0
_tp_lock = threading.Lock()

# Caches for the dashboard /api/* endpoints. Read-mostly, refreshed on TTL.
_bu_cache = None;        _bu_cache_expiry = 0       # /api/business-unit  (5 min TTL)
_reviews_cache = None;   _reviews_cache_expiry = 0  # /api/reviews        (5 min TTL)
_insights_daily_cache = {}                          # /api/insights-summary (24h TTL, keyed by source+date)
_INSIGHTS_CACHE_TTL = 24 * 60 * 60


@app.after_request
def add_cors(response):
    """Open CORS on /api/* endpoints so the dashboard (different origin) can call them."""
    if request.path.startswith("/api/"):
        response.headers["Access-Control-Allow-Origin"]  = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Max-Age"]       = "3600"
    return response


def get_tp_token():
    global _tp_token, _tp_token_expiry
    with _tp_lock:
        if _tp_token and time.time() < _tp_token_expiry:
            return _tp_token
        creds = base64.b64encode(f"{TP_API_KEY}:{TP_SECRET}".encode()).decode()
        r = requests.post(
            "https://api.trustpilot.com/v1/oauth/oauth-business-users-for-applications/accesstoken",
            headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials"},
            timeout=10,
        )
        data = r.json()
        _tp_token = data["access_token"]
        _tp_token_expiry = time.time() + int(data.get("expires_in", 3600)) - 60
        return _tp_token


def get_review_details(review_id):
    """Fetch email and createdAt date from the Trustpilot private review API.
    Returns (email, review_date) where review_date is formatted e.g. 'Jun 8, 2026'."""
    for attempt in range(1, 4):
        try:
            token = get_tp_token()
            r = requests.get(
                f"https://api.trustpilot.com/v1/private/reviews/{review_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"apikey": TP_API_KEY},
                timeout=15,
            )
            data = r.json()
            email       = data.get("referralEmail") or ""
            review_date = format_review_date(data.get("createdAt") or "")
            return email, review_date
        except Exception as e:
            print(f"Error fetching review details (attempt {attempt}/3): {e}")
            if attempt < 3:
                time.sleep(2)
    return "", ""


def get_sheets_service():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds)


def write_to_sheet(name, email, rating, review_type, comment, reply, suggestion, review_id="", review_date=""):
    try:
        service = get_sheets_service()
        # Column A: use the actual review date if available, otherwise today
        date_col = review_date if review_date else time.strftime("%Y-%m-%d", time.gmtime())
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="A:J",
            valueInputOption="USER_ENTERED",
            body={"values": [[
                date_col,    # A: Date the customer wrote the review
                name,        # B: Customer Name
                email,       # C: Customer Email
                rating,      # D: Star Rating
                review_type, # E: Type (AI-classified for 1-3 stars)
                comment,     # F: Comment
                reply,       # G: Reply Suggestion
                suggestion,  # H: Business Suggestion
                "",          # I: Remark (set manually)
                review_id,   # J: Trustpilot Review ID
            ]]},
        ).execute()
    except Exception as e:
        print(f"Sheet write error: {e}")


def format_review_date(iso_date):
    """Convert Trustpilot ISO date (2026-06-08T19:22:58Z) to readable format."""
    try:
        # Parse and reformat to e.g. "Jun 8, 2026"
        import datetime
        dt = datetime.datetime.strptime(iso_date[:10], "%Y-%m-%d")
        return dt.strftime("%b %-d, %Y")
    except Exception:
        return iso_date[:10] if iso_date else ""


def process_review(payload):
    """Process a review event in a background thread."""
    event     = payload.get("eventType")
    name      = (payload.get("consumer") or {}).get("displayName") or "A customer"
    rating    = str(payload.get("stars", "Unknown"))
    title     = payload.get("title") or ""
    text      = payload.get("text") or ""
    review_id = payload.get("reviewId") or payload.get("id") or ""

    # Trustpilot customers often repeat the title as the first line of the body.
    # Only prepend the title if the body doesn't already start with it.
    if title and not text.lower().startswith(title.lower()):
        comment = f"{title}\n\n{text}".strip()
    else:
        comment = text.strip()

    if event == "review.deleted":
        send_to_gchat_deleted(name, rating)
        return

    # Single API call fetches both email and the actual review date from Trustpilot
    email, review_date = get_review_details(review_id) if review_id else ("", "")
    has_comment  = len(comment) > 5
    reply        = get_reply_suggestion(comment, rating, name) if has_comment else ""
    suggestion   = get_gemini_suggestion(comment, rating) if has_comment else ""
    review_type  = get_review_type(comment) if has_comment and rating.isdigit() and int(rating) <= 3 else ""

    send_to_gchat(name, email, rating, comment, reply, suggestion, event, review_id, review_date)
    write_to_sheet(name, email, rating, review_type, comment, reply, suggestion, review_id, review_date)

    if rating.isdigit() and int(rating) <= 3:
        create_service_request(email, comment)


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = json.loads(request.get_data())
    except json.JSONDecodeError:
        return jsonify({"status": "bad request"}), 400

    event = payload.get("eventType")
    if event not in ("review.created", "review.updated", "review.deleted"):
        return jsonify({"status": "ignored"})

    # Return 200 immediately — process in background so the request never times out.
    # daemon=False so the process won't exit while the thread is still running.
    thread = threading.Thread(target=process_review, args=(payload,), daemon=False)
    thread.start()

    return jsonify({"status": "ok"})


@app.route("/monitor", methods=["GET", "POST"])
def monitor():
    """
    Fetch the 50 most recent Trustpilot reviews, check which are absent from
    the sheet (by review ID stored in col J), and backfill any that are missing.
    Called by Cloud Scheduler every 30 minutes.
    """
    try:
        # ── fetch sheet review IDs (col J) and names (col B) ─────────────────
        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID, range="B:J"
        ).execute()
        rows = result.get("values", [])
        # Only deduplicate by review ID (column J) — name matching caused false
        # positives when two customers share the same first name (e.g. two "Steven"s).
        known_ids = {r[8].strip() for r in rows if len(r) > 8 and r[8].strip()}

        # ── fetch recent Trustpilot reviews ───────────────────────────────────
        tp_token = get_tp_token()
        r = requests.get(
            f"https://api.trustpilot.com/v1/private/business-units/{BU_ID}/reviews",
            headers={"Authorization": f"Bearer {tp_token}"},
            params={"apikey": TP_API_KEY, "perPage": 50, "orderBy": "createdat.desc"},
            timeout=15,
        )
        reviews = r.json().get("reviews", [])

        missing = []
        for rv in reviews:
            rid  = rv.get("id", "")
            name = (rv.get("consumer") or {}).get("displayName") or ""
            if rid in known_ids:
                continue
            missing.append(rv)

        print(f"Monitor: {len(reviews)} reviews checked, {len(missing)} missing")

        # ── backfill each missing review ──────────────────────────────────────
        for rv in missing:
            rid         = rv.get("id", "")
            name        = (rv.get("consumer") or {}).get("displayName") or "A customer"
            rating      = str(rv.get("stars", ""))
            title       = rv.get("title") or ""
            text        = rv.get("text") or ""
            review_date = format_review_date(rv.get("createdAt") or "")

            if title and not text.lower().startswith(title.lower()):
                comment = f"{title}\n\n{text}".strip()
            else:
                comment = text.strip()

            email, _ = get_review_details(rid) if rid else ("", "")
            has_comment  = len(comment) > 5
            reply        = get_reply_suggestion(comment, rating, name) if has_comment else ""
            suggestion   = get_gemini_suggestion(comment, rating) if has_comment else ""
            review_type  = get_review_type(comment) if has_comment and rating.isdigit() and int(rating) <= 3 else ""

            send_to_gchat(name, email, rating, comment, reply, suggestion, "review.created", rid, review_date)
            write_to_sheet(name, email, rating, review_type, comment, reply, suggestion, rid, review_date)

            if rating.isdigit() and int(rating) <= 3:
                create_service_request(email, comment)

            print(f"Monitor: backfilled {name} ({rating} stars, id={rid})")

        return jsonify({"status": "ok", "checked": len(reviews), "backfilled": len(missing)})

    except Exception as e:
        print(f"Monitor error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


TEAMDESK_URL = "https://www.teamdesk.net/secure/api/v2/56554/BEA2566590EF4D14AA8D35AD0E2CEA77/t_419099/upsert.json"


def create_service_request(email, comment):
    try:
        r = requests.post(
            TEAMDESK_URL,
            json=[{
                "Email": email,
                "SR Level": "Social Damage",
                "Client Request Notes": comment,
                "Agent Email": "geeta@superhairpieces.com",
                "Will the Client Send Products to Office?": "No",
            }],
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        print(f"TeamDesk SR created: {r.status_code} {r.text}")
    except Exception as e:
        print(f"TeamDesk error: {e}")


def get_vertex_token():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def vertex_call(prompt):
    url = (
        f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT}/locations/{VERTEX_LOCATION}/"
        f"publishers/google/models/{VERTEX_MODEL}:generateContent"
    )
    token = get_vertex_token()
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.4}},
        timeout=60,
    )
    parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    for part in parts:
        if part.get("text"):
            return part["text"].strip()
    return ""


REVIEW_TYPES = [
    "customer support",
    "shipping",
    "product defective - stock",
    "product defective - custom",
    "product defective - salon finished",
]


def get_review_type(comment):
    prompt = (
        "You are classifying a negative customer review for Superhairpieces, a hairpiece company.\n\n"
        "Based on the review comment below, pick the single most relevant category from this list:\n"
        + "\n".join(f"- {t}" for t in REVIEW_TYPES) +
        "\n\nDefinitions:\n"
        "- customer support: complaint about staff, communication, response time, or service attitude\n"
        "- shipping: complaint about delivery, courier, packaging damage in transit, or delays\n"
        "- product defective - stock: complaint about a ready-made/off-the-shelf hairpiece (quality, colour, size)\n"
        "- product defective - custom: complaint about a custom-ordered hairpiece that didn't meet specifications\n"
        "- product defective - salon finished: complaint about additional services such as haircut, base cut, trim, or spray applied to the product\n\n"
        f"Review:\n\"{comment}\"\n\n"
        "Reply with only the category name, exactly as written above. No explanation."
    )
    try:
        result = vertex_call(prompt).lower().strip()
        if result in REVIEW_TYPES:
            return result
    except Exception as e:
        print(f"Vertex AI type error: {e}")
    return ""


def get_reply_suggestion(comment, rating, name):
    prompt = (
        f"A customer named {name} left a {rating}-star review for Superhairpieces "
        f"with the following comment:\n\"{comment}\"\n\n"
        "Write a warm, professional public reply from Superhairpieces to post on Trustpilot. "
        "Keep it to 2-3 sentences. Thank them, address their specific feedback, and invite "
        "them to reach out if needed. Do not use generic filler phrases."
    )
    try:
        return vertex_call(prompt)
    except Exception as e:
        print(f"Vertex AI reply error: {e}")
    return ""


def get_gemini_suggestion(comment, rating):
    prompt = (
        f"A customer left a {rating}-star review for our hairpiece company "
        f"(Superhairpieces) with the following comment:\n\"{comment}\"\n\n"
        "Provide a brief 1-2 sentence actionable improvement suggestion for our "
        "internal team. If the review is fully positive, suggest a quick way to "
        "capitalise on it. Be concise and professional."
    )
    try:
        return vertex_call(prompt)
    except Exception as e:
        print(f"Vertex AI error: {e}")
    return "AI suggestion unavailable."


def send_to_gchat(name, email, rating, comment, reply, suggestion, event, review_id="", review_date=""):
    stars = "⭐" * min(int(rating) if rating.isdigit() else 0, 5)
    label = "New Trustpilot Review" if event == "review.created" else "Trustpilot Review Updated"
    email_line = f"<br><b>Email:</b> {email}" if email else ""
    date_line  = f"<br><b>Date:</b> {review_date}" if review_date else ""
    review_url = f"https://businessapp.b2b.trustpilot.com/reviews/{review_id}" if review_id else "https://businessapp.b2b.trustpilot.com/reviews"

    card = {
        "cardsV2": [{
            "cardId": review_id or "review",
            "card": {
                "sections": [
                    {
                        "widgets": [{
                            "textParagraph": {
                                "text": (
                                    f"{stars} <b>{label}</b> {stars}<br><br>"
                                    f"<b>Customer:</b> {name}{email_line}<br>"
                                    f"<b>Rating:</b> {rating}-star{date_line}"
                                )
                            }
                        }]
                    },
                    {
                        "header": "Comment",
                        "widgets": [{"textParagraph": {"text": comment or "—"}}]
                    },
                    {
                        "header": "\U0001f4ac Reply Suggestion",
                        "widgets": [{"textParagraph": {"text": reply or "—"}}]
                    },
                    {
                        "header": "\U0001f4a1 Business Suggestion",
                        "widgets": [
                            {"textParagraph": {"text": suggestion or "—"}},
                            {"buttonList": {"buttons": [{
                                "text": "Reply on Trustpilot",
                                "onClick": {"openLink": {"url": review_url}},
                                "color": {"red": 0.0, "green": 0.478, "blue": 1.0, "alpha": 1.0}
                            }]}}
                        ]
                    }
                ]
            }
        }]
    }
    try:
        requests.post(GCHAT_WEBHOOK_URL, json=card, timeout=10)
    except Exception as e:
        print(f"Google Chat error: {e}")


def send_to_gchat_deleted(name, rating):
    stars = "⭐" * min(int(rating) if rating.isdigit() else 0, 5)
    text = (
        f"\U0001f5d1️ *Trustpilot Review Deleted*\n\n"
        f"*Customer:* {name}\n"
        f"*Was rated:* {rating}-star {stars}"
    )
    try:
        requests.post(GCHAT_WEBHOOK_URL, json={"text": text}, timeout=10)
    except Exception as e:
        print(f"Google Chat error: {e}")


INVITATION_HEADERS = [
    "Invitation ID",
    "Order / Reference #",
    "Customer Name",
    "Customer Email",
    "Status",
    "Created At (UTC)",
    "Sent At (UTC)",
    "Source",
    "Tags",
]

INVITATIONS_API = "https://invitations-api.trustpilot.com/v1/private/business-units"


def fetch_all_invitations():
    """Fetch every invitation from Trustpilot (paginates via page param)."""
    token = get_tp_token()
    page, per_page = 1, 100
    all_invitations = []
    while True:
        r = requests.get(
            f"{INVITATIONS_API}/{BU_ID}/invitations",
            headers={"Authorization": f"Bearer {token}"},
            params={"apikey": TP_API_KEY, "perPage": per_page, "page": page},
            timeout=30,
        )
        batch = r.json().get("invitations", [])
        if not batch:
            break
        all_invitations.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return all_invitations


def invitation_to_row(inv):
    recipient = inv.get("recipient") or {}
    tags      = ", ".join(inv.get("tags", []) or [])
    return [
        inv.get("id", ""),
        inv.get("referenceId", ""),
        recipient.get("name", ""),
        recipient.get("email", ""),
        inv.get("status", ""),
        (inv.get("createdTime", "") or "")[:19].replace("T", " "),
        (inv.get("sentTime", "") or "")[:19].replace("T", " "),
        inv.get("source", ""),
        tags,
    ]


@app.route("/sync-invitations", methods=["GET", "POST"])
def sync_invitations():
    """
    Full refresh: fetch all Trustpilot invitations and overwrite the
    invitations sheet (INVITATIONS_SHEET_ID).
    Safe to call repeatedly — always writes a fresh snapshot.
    """
    try:
        invitations = fetch_all_invitations()
        rows = [INVITATION_HEADERS] + [invitation_to_row(inv) for inv in invitations]

        service = get_sheets_service()
        sheet   = service.spreadsheets()

        # Clear existing content then write fresh rows
        sheet.values().clear(
            spreadsheetId=INVITATIONS_SHEET_ID,
            range="A:I",
        ).execute()

        sheet.values().update(
            spreadsheetId=INVITATIONS_SHEET_ID,
            range="A1",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        ).execute()

        print(f"sync-invitations: wrote {len(invitations)} rows")
        return jsonify({"status": "ok", "total": len(invitations)})

    except Exception as e:
        print(f"sync-invitations error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
# Dashboard /api/* endpoints — power the Reviews Dashboard frontend
# ============================================================


@app.route("/api/business-unit", methods=["GET", "OPTIONS"])
def api_business_unit():
    """Proxy to Trustpilot Business Units endpoint for the dashboard.
    Returns trustScore, stars, and numberOfReviews. Cached 5 minutes."""
    global _bu_cache, _bu_cache_expiry
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        if _bu_cache and time.time() < _bu_cache_expiry:
            return jsonify(_bu_cache), 200
        r = requests.get(
            f"https://api.trustpilot.com/v1/business-units/{BU_ID}",
            params={"apikey": TP_API_KEY},
            timeout=10,
        )
        if r.status_code != 200:
            return jsonify({"error": f"Trustpilot API returned {r.status_code}", "body": r.text}), 502
        _bu_cache = r.json()
        _bu_cache_expiry = time.time() + 300
        return jsonify(_bu_cache), 200
    except Exception as e:
        print(f"business-unit error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/reviews", methods=["GET", "OPTIONS"])
def api_reviews():
    """Most recent reviews from the Trustpilot API. Includes companyReply when posted."""
    global _reviews_cache, _reviews_cache_expiry
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        if _reviews_cache and time.time() < _reviews_cache_expiry:
            return jsonify(_reviews_cache), 200
        r = requests.get(
            f"https://api.trustpilot.com/v1/business-units/{BU_ID}/reviews",
            params={"apikey": TP_API_KEY, "perPage": 100, "orderBy": "createdat.desc"},
            timeout=15,
        )
        if r.status_code != 200:
            return jsonify({"error": f"Trustpilot API returned {r.status_code}", "body": r.text}), 502
        _reviews_cache = r.json()
        _reviews_cache_expiry = time.time() + 300
        return jsonify(_reviews_cache), 200
    except Exception as e:
        print(f"reviews error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/business-users", methods=["GET", "OPTIONS"])
def api_business_users():
    """Lists business users for the BU (used for authorBusinessUserId on replies)."""
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        token = get_tp_token()
        r = requests.get(
            f"https://api.trustpilot.com/v1/private/business-units/{BU_ID}/business-users",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code != 200:
            return jsonify({"error": f"TP {r.status_code}", "body": r.text[:800]}), 502
        return jsonify(r.json()), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reply/<review_id>", methods=["POST", "OPTIONS"])
def api_reply(review_id):
    """Post a company reply to a Trustpilot review. Invalidates the reviews cache."""
    global _reviews_cache, _reviews_cache_expiry
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        body = request.get_json(silent=True) or {}
        message = (body.get("message") or "").strip()
        if not message:
            return jsonify({"error": "Reply can't be empty"}), 400
        if len(message) > 5000:
            return jsonify({"error": "Reply too long (5000 char max)"}), 400
        token = get_tp_token()
        r = requests.post(
            f"https://api.trustpilot.com/v1/private/reviews/{review_id}/reply",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"message": message, "authorBusinessUserId": TP_AUTHOR_BUSINESS_USER_ID},
            timeout=15,
        )
        if r.status_code not in (200, 201, 204):
            print(f"TP reply error: {r.status_code} {r.text[:400]}")
            return jsonify({
                "error": f"Trustpilot rejected the reply (HTTP {r.status_code})",
                "details": r.text[:500],
            }), 502
        _reviews_cache = None
        _reviews_cache_expiry = 0
        return jsonify({"ok": True}), 200
    except Exception as e:
        print(f"reply error: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# Website-feedback email reply (Gmail API + domain-wide delegation)
# ============================================================
# The dashboard's Website tab calls /api/send-website-reply to send a real email from a
# Workspace mailbox (default: manne@superhairpieces.com). Cloud Run's runtime service account
# has no Gmail scope of its own; instead we use IAM Credentials' signJwt to mint a domain-wide
# delegation JWT for the impersonated user, exchange it for an access token, and call Gmail.
#
# Prereqs (one-time, done in admin.google.com by a Workspace super-admin):
#   1. Security → Access and data control → API controls → Manage Domain Wide Delegation
#   2. Add a new API client:
#        Client ID: the runtime service account's OAuth client ID
#                   (find with: `gcloud iam service-accounts describe <SA_EMAIL> --format='value(oauth2ClientId)'`)
#        Scopes:    https://www.googleapis.com/auth/gmail.send
#   3. Grant the runtime SA the `roles/iam.serviceAccountTokenCreator` role on ITSELF
#        (so it can call signJwt against its own identity).
#   4. Enable the Gmail API + IAM Credentials API in the GCP project.

import base64
import json
from email.mime.text import MIMEText
from email.utils import formataddr
from google.oauth2.credentials import Credentials as UserCredentials

WEBSITE_SHEET_ID = "1cp_9ktvmzTSVRAzve6UJWDKnN66cmcDKvALaFyDJM3o"
DEFAULT_REPLY_SENDER = os.environ.get("REPLY_SENDER_EMAIL", "sales@superhairpieces.com")
REPLY_FROM_NAME = os.environ.get("REPLY_FROM_NAME", "Superhairpieces Sales")

_gmail_token_cache = {}  # impersonated_email → {access_token, exp_ts}


def _mint_dwd_access_token(impersonated_email, scope):
    """Use IAM Credentials signJwt to mint a Gmail-scoped access token for a Workspace user."""
    cached = _gmail_token_cache.get(impersonated_email)
    now = int(time.time())
    if cached and cached.get("exp_ts", 0) - 60 > now:
        return cached["access_token"]

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    sa_email = getattr(creds, "service_account_email", None) or os.environ.get("RUNTIME_SA_EMAIL")
    if not sa_email:
        raise RuntimeError("Couldn't determine runtime service account email (set RUNTIME_SA_EMAIL env var?)")

    iat = now
    payload = {
        "iss": sa_email,
        "sub": impersonated_email,
        "scope": scope,
        "aud": "https://oauth2.googleapis.com/token",
        "iat": iat,
        "exp": iat + 3600,
    }
    iam = build("iamcredentials", "v1", credentials=creds, cache_discovery=False)
    signed = iam.projects().serviceAccounts().signJwt(
        name=f"projects/-/serviceAccounts/{sa_email}",
        body={"payload": json.dumps(payload)},
    ).execute()
    token_resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": signed["signedJwt"],
        },
        timeout=30,
    )
    if token_resp.status_code != 200:
        print(f"[dwd] token exchange failed: HTTP {token_resp.status_code} · body={token_resp.text} · sa={sa_email} · sub={impersonated_email} · scope={scope}")
        raise RuntimeError(f"DWD token exchange failed ({token_resp.status_code}): {token_resp.text}")
    tok = token_resp.json()
    _gmail_token_cache[impersonated_email] = {
        "access_token": tok["access_token"],
        "exp_ts": iat + int(tok.get("expires_in", 3600)),
    }
    return tok["access_token"]


def _send_gmail_as(sender_email, to_email, subject, body, from_name=None):
    access_token = _mint_dwd_access_token(sender_email, "https://www.googleapis.com/auth/gmail.send")
    svc = build("gmail", "v1", credentials=UserCredentials(token=access_token), cache_discovery=False)
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to_email
    msg["From"] = formataddr((from_name or REPLY_FROM_NAME, sender_email))
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return svc.users().messages().send(userId="me", body={"raw": raw}).execute()


@app.route("/api/send-website-reply", methods=["POST", "OPTIONS"])
def api_send_website_reply():
    """Send a real email reply to a website-feedback customer via Gmail API."""
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        data = request.get_json(silent=True) or {}
        to_email = (data.get("to") or "").strip()
        body = (data.get("body") or "").strip()
        subject = (data.get("subject") or "").strip() or "Re: your feedback on Superhairpieces"
        sender = (data.get("from") or DEFAULT_REPLY_SENDER).strip()
        sheet_row = data.get("sheetRow")  # optional 1-based row in website feedback sheet
        if not to_email or "@" not in to_email:
            return jsonify({"error": "Valid 'to' email required"}), 400
        if not body:
            return jsonify({"error": "Reply body can't be empty"}), 400
        if len(body) > 10000:
            return jsonify({"error": "Reply too long (10000 char max)"}), 400

        result = _send_gmail_as(sender, to_email, subject, body)
        sent_at = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

        # Stamp column J ("Replied At") in the website feedback sheet so the row shows as replied.
        if sheet_row:
            try:
                svc = get_sheets_service()
                svc.spreadsheets().values().update(
                    spreadsheetId=WEBSITE_SHEET_ID,
                    range=f"J{int(sheet_row)}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[sent_at]]},
                ).execute()
            except Exception as stamp_err:
                print(f"[send-website-reply] sheet stamp failed (email still sent): {stamp_err}")

        return jsonify({"ok": True, "messageId": result.get("id"), "sentAt": sent_at}), 200
    except Exception as e:
        print(f"[send-website-reply] error: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# Weekly EU website-feedback summary → Google Chat (EU SEO/AEO/GEO space)
# ============================================================
# Cloud Scheduler hits /weekly-eu-feedback every Monday morning. It reads the
# website feedback sheet, keeps rows from the EU storefronts logged in the last
# 7 days, and posts a summary card (stats + comments + AI insights) to the
# EU SEO/AEO/GEO Chat space via the EU_GCHAT_WEBHOOK_URL env var.

EU_GCHAT_WEBHOOK_URL = os.environ.get("EU_GCHAT_WEBHOOK_URL", "")
EU_FEEDBACK_DOMAINS  = ".es,.nl,.fr,.de"


def _parse_logged_date(value):
    """Parse the sheet's Date Logged values, e.g. '2025-09-04T17:26:45.000Z'."""
    import datetime
    for fmt, length in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.datetime.strptime(value[:length], fmt)
        except Exception:
            continue
    return None


def translate_notes_to_english(notes):
    """Batch-translate feedback notes to English in one Gemini call.
    Returns a list the same length as `notes`; an entry is '' when the note is
    already English or translation failed (caller shows the original only)."""
    numbered = "\n".join(f"{i + 1}. {n[:300]}" for i, n in enumerate(notes))
    prompt = (
        "Translate each numbered customer feedback note below into English. The notes "
        "come from Spanish, Dutch, French, or German storefronts of a hairpiece retailer.\n\n"
        f"{numbered}\n\n"
        "Return STRICT JSON: an array of strings with exactly the same order and length "
        "as the input, no markdown fences. If a note is already in English, return an "
        "empty string \"\" for that position."
    )
    try:
        raw = (vertex_call(prompt) or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].lstrip()
        first, last = raw.find("["), raw.rfind("]")
        parsed = json.loads(raw[first:last + 1])
        if isinstance(parsed, list) and len(parsed) == len(notes):
            return [str(t or "").strip() for t in parsed]
        print(f"translate_notes: length mismatch ({len(parsed) if isinstance(parsed, list) else 'non-list'} vs {len(notes)})")
    except Exception as e:
        print(f"Vertex AI translation error: {e}")
    return [""] * len(notes)


def get_eu_feedback_summary(noted_entries):
    lines = "\n".join(
        f"- {e['score']}★ ({e['domain']}, {e['url']}) {e['note'][:300]}"
        for e in noted_entries[:60]
    )
    prompt = (
        "You are summarizing on-site customer feedback for the EU storefronts of "
        "Superhairpieces (hairpiece / wig retailer) for the EU market manager.\n"
        f"Feedback from the past week:\n{lines}\n\n"
        "Write 2-3 short sentences: the main theme(s), any site/UX problem worth "
        "fixing, and one suggested action. Plain text, no greeting, no markdown."
    )
    try:
        return vertex_call(prompt)
    except Exception as e:
        print(f"Vertex AI EU summary error: {e}")
        return ""


@app.route("/weekly-eu-feedback", methods=["GET", "POST"])
def weekly_eu_feedback():
    """
    Weekly digest of EU website feedback for the EU SEO/AEO/GEO Chat space.
    Query params: domains (default '.es,.nl,.fr,.de'), days (default 7),
    dry_run=1 to preview the message without posting.
    """
    import datetime
    try:
        domains = [d.strip() for d in (request.args.get("domains") or EU_FEEDBACK_DOMAINS).split(",") if d.strip()]
        days    = int(request.args.get("days", 7))
        dry_run = request.args.get("dry_run") in ("1", "true", "yes")

        if not EU_GCHAT_WEBHOOK_URL and not dry_run:
            return jsonify({"error": "EU_GCHAT_WEBHOOK_URL env var not set"}), 503

        now    = datetime.datetime.utcnow()
        cutoff = now - datetime.timedelta(days=days)

        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=WEBSITE_SHEET_ID, range="A:I"
        ).execute()
        values = result.get("values", [])

        entries = []
        for i, row in enumerate(values):
            if i == 0:
                continue
            row = (row + [""] * 9)[:9]
            domain, url, score, logged, ip, note, client_email, contact, logged_email = [c.strip() for c in row]
            if domain not in domains:
                continue
            dt = _parse_logged_date(logged)
            if not dt or dt < cutoff:
                continue
            entries.append({
                "domain": domain,
                "url":    url,
                "score":  int(score) if score.isdigit() else 0,
                "note":   note,
                "email":  client_email,
            })

        date_range = f"{cutoff.strftime('%b %d')} – {now.strftime('%b %d, %Y')}"
        domains_label = ", ".join(f"superhairpieces{d}" for d in domains if any(e["domain"] == d for e in entries)) \
                        or ", ".join(f"superhairpieces{d}" for d in domains)

        if not entries:
            message = {"text": (
                f"📊 *Weekly Website Feedback — EU* ({date_range})\n\n"
                f"No feedback was submitted on {domains_label} this week."
            )}
        else:
            scored  = [e for e in entries if e["score"]]
            avg     = round(sum(e["score"] for e in scored) / len(scored), 2) if scored else 0
            dist    = " · ".join(f"{s}★ ×{sum(1 for e in scored if e['score'] == s)}" for s in range(5, 0, -1))
            noted   = sorted([e for e in entries if e["note"]], key=lambda e: e["score"])
            ai_text = get_eu_feedback_summary(noted) if noted else ""

            stats_html = (
                f"📊 <b>Weekly Website Feedback — EU</b><br>"
                f"<i>{date_range} · {domains_label}</i><br><br>"
                f"<b>Responses:</b> {len(entries)}<br>"
                f"<b>Average score:</b> {avg} / 5<br>"
                f"<b>Distribution:</b> {dist}<br>"
                f"<b>With comments:</b> {len(noted)}"
            )
            sections = [{"widgets": [{"textParagraph": {"text": stats_html}}]}]

            if noted:
                shown = noted[:10]
                translations = translate_notes_to_english([e["note"] for e in shown])
                note_widgets = []
                for e, translated in zip(shown, translations):
                    page  = e["url"].split("?")[0]
                    email = f" · {e['email']}" if e["email"] else ""
                    en_line = f"<br>→ <i>{translated[:400]}</i>" if translated else ""
                    note_widgets.append({"textParagraph": {"text": (
                        f"<b>{e['score']}★</b> — {e['note'][:400]}{en_line}"
                        f"<br><i>{page}{email}</i>"
                    )}})
                if len(noted) > 10:
                    note_widgets.append({"textParagraph": {"text": f"<i>…and {len(noted) - 10} more comments in the sheet</i>"}})
                sections.append({"header": "💬 Comments (lowest scores first)", "widgets": note_widgets})

            if ai_text:
                sections.append({"header": "💡 AI Insights", "widgets": [{"textParagraph": {"text": ai_text}}]})

            sections.append({"widgets": [{"buttonList": {"buttons": [{
                "text": "Open feedback sheet",
                "onClick": {"openLink": {"url": f"https://docs.google.com/spreadsheets/d/{WEBSITE_SHEET_ID}/edit"}},
                "color": {"red": 0.0, "green": 0.478, "blue": 1.0, "alpha": 1.0}
            }]}}]})

            message = {"cardsV2": [{"cardId": f"eu-feedback-{now.strftime('%Y-%m-%d')}", "card": {"sections": sections}}]}

        posted = False
        if not dry_run:
            r = requests.post(EU_GCHAT_WEBHOOK_URL, json=message, timeout=10)
            posted = r.status_code == 200
            if not posted:
                print(f"weekly-eu-feedback: Chat webhook returned {r.status_code} {r.text[:300]}")

        return jsonify({
            "status": "ok", "days": days, "domains": domains,
            "total": len(entries), "posted": posted, "dry_run": dry_run,
            **({"message": message} if dry_run else {}),
        })

    except Exception as e:
        print(f"weekly-eu-feedback error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# Issue-type taxonomy used by the backfill endpoint. (The live webhook keeps using
# get_review_type() for backwards compat with its existing classifier output.)
ISSUE_TYPE_TAXONOMY = [
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


def get_issue_type(comment, rating, name=""):
    if not (comment or "").strip():
        return ""
    taxonomy_str = "\n".join(f"- {t}" for t in ISSUE_TYPE_TAXONOMY)
    prompt = (
        f"A customer left a {rating}-star review for Superhairpieces (hairpiece / wig retailer).\n"
        f'Review: "{comment}"\n\n'
        "Classify into ONE category. Output ONLY the category name exactly as written below — "
        "no quotes, no punctuation, no explanation.\n\n"
        f"Categories:\n{taxonomy_str}\n\n"
        "Guidance:\n"
        "- 4–5★ with no specific complaint → \"Positive Experience\".\n"
        "- Pick the most specific defect when applicable.\n"
        "- If it doesn't clearly fit, return \"Other\"."
    )
    try:
        raw = (vertex_call(prompt) or "").strip().strip("\"' `*\n\r\t.")
        for t in ISSUE_TYPE_TAXONOMY:
            if raw.lower() == t.lower():
                return t
        print(f"[issue-type] off-taxonomy value {raw!r}; coercing to Other")
        return "Other"
    except Exception as e:
        print(f"Vertex AI issue-type error: {e}")
        return ""


@app.route("/api/backfill-suggestions", methods=["POST", "GET", "OPTIONS"])
def api_backfill_suggestions():
    """One-shot backfill of empty G (Reply Suggestion) / H (Business Suggestion) using Gemini."""
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        limit = min(int(request.args.get("limit", 25)), 100)
        dry_run = request.args.get("dry_run") in ("1", "true", "yes")
        fill_set = set((request.args.get("fill") or "reply,biz").split(","))
        do_reply = "reply" in fill_set
        do_biz   = "biz"   in fill_set

        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID, range="A:I"
        ).execute()
        values = result.get("values", [])

        processed, updates, scanned = [], [], 0
        for i, row in enumerate(values):
            scanned += 1
            if i == 0:
                continue
            row = (row + [""] * 9)[:9]
            ts, name, email, stars, type_, comment, reply, biz, remark = row
            comment_s = (comment or "").strip()
            if not comment_s or len(comment_s) < 5:
                continue
            need_reply = do_reply and not (reply or "").strip()
            need_biz   = do_biz   and not (biz   or "").strip()
            if not need_reply and not need_biz:
                continue
            if len(processed) >= limit:
                break

            rating_str = str(stars) if stars else "Unknown"
            row_info = {"row": i + 1, "name": name, "ts": ts, "stars": stars,
                        "filled_reply": False, "filled_biz": False}
            if not dry_run:
                new_reply, new_biz = reply, biz
                if need_reply:
                    try:
                        gen = get_reply_suggestion(comment_s, rating_str, name) or ""
                        if gen: new_reply, row_info["filled_reply"] = gen, True
                    except Exception as e:
                        row_info["reply_error"] = str(e)[:200]
                if need_biz:
                    try:
                        gen = get_gemini_suggestion(comment_s, rating_str) or ""
                        if gen and gen != "AI suggestion unavailable.":
                            new_biz, row_info["filled_biz"] = gen, True
                    except Exception as e:
                        row_info["biz_error"] = str(e)[:200]
                if row_info["filled_reply"] or row_info["filled_biz"]:
                    updates.append({"range": f"G{i+1}:H{i+1}", "values": [[new_reply or "", new_biz or ""]]})
            else:
                row_info["filled_reply"] = need_reply
                row_info["filled_biz"]   = need_biz
            processed.append(row_info)

        if updates and not dry_run:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=GOOGLE_SHEET_ID,
                body={"valueInputOption": "USER_ENTERED", "data": updates},
            ).execute()

        remaining = 0
        for i, row in enumerate(values):
            if i == 0: continue
            row = (row + [""] * 9)[:9]
            comment_s = (row[5] or "").strip()
            if not comment_s or len(comment_s) < 5: continue
            need = (do_reply and not (row[6] or "").strip()) or (do_biz and not (row[7] or "").strip())
            if need: remaining += 1

        return jsonify({
            "scanned": scanned, "processed": len(processed), "wrote": len(updates),
            "remaining_after": max(0, remaining - len(updates)),
            "dry_run": dry_run, "items": processed,
        }), 200
    except Exception as e:
        print(f"backfill error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/backfill-issue-types", methods=["POST", "GET", "OPTIONS"])
def api_backfill_issue_types():
    """One-shot backfill: classifies each row's Issue Type (column E) using Gemini."""
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        limit = min(int(request.args.get("limit", 25)), 100)
        dry_run = request.args.get("dry_run") in ("1", "true", "yes")
        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID, range="A:I"
        ).execute()
        values = result.get("values", [])

        processed, updates, scanned = [], [], 0
        for i, row in enumerate(values):
            scanned += 1
            if i == 0:
                continue
            row = (row + [""] * 9)[:9]
            ts, name, email, stars, type_, comment, reply, biz, remark = row
            comment_s = (comment or "").strip()
            if not comment_s or len(comment_s) < 5:
                continue
            if (type_ or "").strip():
                continue
            if len(processed) >= limit:
                break

            rating_str = str(stars) if stars else "Unknown"
            row_info = {"row": i + 1, "name": name, "ts": ts, "stars": stars, "issue": None}
            if not dry_run:
                try:
                    issue = get_issue_type(comment_s, rating_str, name) or ""
                    if issue:
                        row_info["issue"] = issue
                        updates.append({"range": f"E{i+1}", "values": [[issue]]})
                except Exception as e:
                    row_info["error"] = str(e)[:200]
            else:
                row_info["issue"] = "(would classify)"
            processed.append(row_info)

        if updates and not dry_run:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=GOOGLE_SHEET_ID,
                body={"valueInputOption": "USER_ENTERED", "data": updates},
            ).execute()

        remaining = 0
        for i, row in enumerate(values):
            if i == 0: continue
            row = (row + [""] * 9)[:9]
            comment_s = (row[5] or "").strip()
            if not comment_s or len(comment_s) < 5: continue
            if not (row[4] or "").strip(): remaining += 1

        return jsonify({
            "scanned": scanned, "processed": len(processed), "wrote": len(updates),
            "remaining_after": max(0, remaining - len(updates)),
            "dry_run": dry_run, "items": processed,
        }), 200
    except Exception as e:
        print(f"backfill issue-types error: {e}")
        return jsonify({"error": str(e)}), 500


def _insights_cache_key(source, review_count, date_label):
    today_utc = time.strftime("%Y-%m-%d", time.gmtime())
    return f"{source}|{today_utc}|{date_label}|{review_count}"


@app.route("/api/insights-summary", methods=["POST", "OPTIONS"])
def api_insights_summary():
    """Synthesize a daily-cached AI summary of a set of reviews. First user of the day per
    source pays Gemini latency; every subsequent call returns from cache in <100ms."""
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        body = request.get_json(silent=True) or {}
        source     = (body.get("source") or "all").lower()
        date_label = body.get("dateLabel") or "this period"
        reviews    = body.get("reviews") or []
        force      = bool(body.get("force"))
        if not isinstance(reviews, list) or not reviews:
            return jsonify({"error": "No reviews provided"}), 400

        cache_key = _insights_cache_key(source, len(reviews), date_label)
        cached = _insights_daily_cache.get(cache_key)
        if cached and not force and (time.time() - cached["ts"]) < _INSIGHTS_CACHE_TTL:
            payload = dict(cached["payload"])
            payload["cached"]      = True
            payload["cacheAgeSec"] = int(time.time() - cached["ts"])
            return jsonify(payload), 200

        reviews_sorted = sorted(reviews, key=lambda r: (r.get("ts") or ""), reverse=True)[:120]
        lines = []
        for r in reviews_sorted:
            stars = r.get("stars") or "?"
            comment = (r.get("comment") or "").strip().replace("\n", " ")
            if len(comment) > 320:
                comment = comment[:320] + "…"
            issue = (r.get("issueType") or "").strip()
            product = (r.get("productTitle") or "").strip()
            tag_parts = []
            if issue:   tag_parts.append(f"issue={issue}")
            if product: tag_parts.append(f"product={product[:48]}")
            tag = (" [" + ", ".join(tag_parts) + "]") if tag_parts else ""
            lines.append(f"- {stars}★{tag} {comment}")
        context_text = "\n".join(lines)

        SOURCE_LABEL = {
            "trustpilot": "Trustpilot business reviews",
            "stamped":    "Stamped.io product reviews",
            "website":    "on-site Superhairpieces feedback",
            "all":        "reviews from every connected source (Trustpilot, Stamped, website feedback)",
        }.get(source, source)

        prompt = (
            f"You are an analyst summarizing {SOURCE_LABEL} for Superhairpieces (hairpiece / wig retailer).\n"
            f"Date range: {date_label}. {len(reviews_sorted)} reviews (most recent first):\n\n"
            f"{context_text}\n\n"
            "Produce a concise executive summary as STRICT JSON with this exact shape, no markdown fences:\n"
            "{\n"
            '  "headline": "<one sentence, <= 18 words, no greeting>",\n'
            '  "bullets": ["<3 to 5 short observations, each 10-22 words, factual, no fluff>"],\n'
            '  "suggestedActions": ["<1 to 3 imperative actions for the operations team, 8-16 words each>"]\n'
            "}\n\n"
            "Rules:\n"
            "- Be specific. Reference categories, products, or trends actually present.\n"
            "- Quantify where possible (e.g., 'roughly a third').\n"
            "- Skip greetings, disclaimers, and the word 'reviews' in the headline.\n"
            "- Output JSON only. No prose outside the JSON object."
        )
        raw = (vertex_call(prompt) or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].lstrip()
        first, last = raw.find("{"), raw.rfind("}")
        if first == -1 or last == -1:
            print(f"[insights-summary] non-JSON model output: {raw[:300]}")
            return jsonify({"error": "Model returned non-JSON output", "raw": raw[:500]}), 502
        try:
            parsed = json.loads(raw[first:last+1])
        except Exception as e:
            print(f"[insights-summary] JSON parse error: {e}; raw={raw[:300]}")
            return jsonify({"error": "Couldn't parse model output as JSON", "raw": raw[:500]}), 502

        result = {
            "headline":         (parsed.get("headline") or "").strip(),
            "bullets":          parsed.get("bullets") or [],
            "suggestedActions": parsed.get("suggestedActions") or [],
            "reviewsAnalyzed":  len(reviews_sorted),
            "source":           source,
        }
        _insights_daily_cache[cache_key] = {"ts": time.time(), "payload": result}
        return jsonify({**result, "cached": False, "cacheAgeSec": 0}), 200
    except Exception as e:
        print(f"insights-summary error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
