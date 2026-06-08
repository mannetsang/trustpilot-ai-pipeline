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
GCP_PROJECT          = "shp-ai-bot-2026"
VERTEX_LOCATION      = "us-central1"
VERTEX_MODEL         = "gemini-2.5-pro"
INVITATIONS_SHEET_ID = "193E74iZIvF1X3rvDfbEEObqaVOQFRTK8C0iRIReYTnw"

# Cache the Trustpilot access token so we don't re-fetch on every request
_tp_token = None
_tp_token_expiry = 0
_tp_lock = threading.Lock()


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


def get_review_email(review_id):
    for attempt in range(1, 4):  # up to 3 attempts
        try:
            token = get_tp_token()
            r = requests.get(
                f"https://api.trustpilot.com/v1/private/reviews/{review_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"apikey": TP_API_KEY},
                timeout=15,
            )
            return r.json().get("referralEmail") or ""
        except Exception as e:
            print(f"Error fetching review email (attempt {attempt}/3): {e}")
            if attempt < 3:
                time.sleep(2)
    return ""


def get_sheets_service():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds)


def write_to_sheet(name, email, rating, review_type, comment, reply, suggestion, review_id=""):
    try:
        service = get_sheets_service()
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="A:J",
            valueInputOption="USER_ENTERED",
            body={"values": [[
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),  # A: Timestamp
                name,                                                 # B: Customer Name
                email,                                                # C: Customer Email
                rating,                                               # D: Star Rating
                review_type,                                          # E: Type (AI-classified for 1-3 stars)
                comment,                                              # F: Comment
                reply,                                                # G: Reply Suggestion
                suggestion,                                           # H: Business Suggestion
                "",                                                   # I: Remark (set manually)
                review_id,                                            # J: Trustpilot Review ID
            ]]},
        ).execute()
    except Exception as e:
        print(f"Sheet write error: {e}")


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

    email        = get_review_email(review_id) if review_id else ""
    has_comment  = len(comment) > 5
    reply        = get_reply_suggestion(comment, rating, name) if has_comment else ""
    suggestion   = get_gemini_suggestion(comment, rating) if has_comment else ""
    review_type  = get_review_type(comment) if has_comment and rating.isdigit() and int(rating) <= 3 else ""

    send_to_gchat(name, email, rating, comment, reply, suggestion, event, review_id)
    write_to_sheet(name, email, rating, review_type, comment, reply, suggestion, review_id)

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
            rid    = rv.get("id", "")
            name   = (rv.get("consumer") or {}).get("displayName") or "A customer"
            rating = str(rv.get("stars", ""))
            title  = rv.get("title") or ""
            text   = rv.get("text") or ""

            if title and not text.lower().startswith(title.lower()):
                comment = f"{title}\n\n{text}".strip()
            else:
                comment = text.strip()

            email = get_review_email(rid) if rid else ""
            has_comment = len(comment) > 5
            reply        = get_reply_suggestion(comment, rating, name) if has_comment else ""
            suggestion   = get_gemini_suggestion(comment, rating) if has_comment else ""
            review_type  = get_review_type(comment) if has_comment and rating.isdigit() and int(rating) <= 3 else ""

            send_to_gchat(name, email, rating, comment, reply, suggestion, "review.created", rid)
            write_to_sheet(name, email, rating, review_type, comment, reply, suggestion, rid)

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


def send_to_gchat(name, email, rating, comment, reply, suggestion, event, review_id=""):
    stars = "⭐" * min(int(rating) if rating.isdigit() else 0, 5)
    label = "New Trustpilot Review" if event == "review.created" else "Trustpilot Review Updated"
    email_line = f"<br><b>Email:</b> {email}" if email else ""
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
                                    f"<b>Rating:</b> {rating}-star"
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
