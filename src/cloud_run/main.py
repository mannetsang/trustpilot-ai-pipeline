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
from google.cloud import firestore

app = Flask(__name__)

GCHAT_WEBHOOK_URL = os.environ["GCHAT_WEBHOOK_URL"]
GOOGLE_SHEET_ID   = os.environ["GOOGLE_SHEET_ID"]
TP_API_KEY        = os.environ["TP_API_KEY"]
TP_SECRET         = os.environ["TP_SECRET"]
BU_ID             = "5e44f707d7d8c700011eaa10"
GCP_PROJECT       = "shp-ai-bot-2026"
VERTEX_LOCATION   = "us-central1"
VERTEX_MODEL      = "gemini-2.5-pro"

_fs_client = None

def get_firestore():
    global _fs_client
    if _fs_client is None:
        _fs_client = firestore.Client(project=GCP_PROJECT)
    return _fs_client

def already_processed(review_id):
    """Returns True if this review_id was already processed. Marks it if not."""
    if not review_id:
        return False
    ref = get_firestore().collection("processed_reviews").document(review_id)
    if ref.get().exists:
        print(f"Duplicate skipped: {review_id}")
        return True
    ref.set({"ts": time.time()})
    return False

# Thread-safe Trustpilot token cache — multiple background threads share this
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
    try:
        token = get_tp_token()
        r = requests.get(
            f"https://api.trustpilot.com/v1/private/reviews/{review_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"apikey": TP_API_KEY},
            timeout=10,
        )
        return r.json().get("referralEmail") or ""
    except Exception as e:
        print(f"Error fetching review email: {e}")
        return ""


def get_sheets_service():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds)


def write_to_sheet(name, email, rating, review_type, comment, reply, suggestion, review_id=""):
    """
    Write one review row to the sheet.
    Column layout: A Timestamp | B Name | C Email | D Rating | E Type |
                   F Comment | G Reply | H Business Suggestion | I Remark | J Review ID
    Column J (review_id) is the deduplication key used by /monitor.
    """
    try:
        service = get_sheets_service()
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="A:J",
            valueInputOption="USER_ENTERED",
            body={"values": [[
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                name,
                email,
                rating,
                review_type,
                comment,
                reply,
                suggestion,
                "",          # I: Remark (filled manually)
                review_id,   # J: Trustpilot Review ID — deduplication key
            ]]},
        ).execute()
    except Exception as e:
        print(f"Sheet write error: {e}")


EVENT_MAP = {
    "service-review-created": "review.created",
    "service-review-updated": "review.updated",
    "service-review-deleted": "review.deleted",
}


def parse_payload(payload):
    """Normalise both Trustpilot payload formats into (event, data)."""
    if "events" in payload and isinstance(payload["events"], list) and payload["events"]:
        first = payload["events"][0]
        event = EVENT_MAP.get(first.get("eventName"), first.get("eventName"))
        data  = first.get("eventData", {})
    else:
        event = payload.get("eventType")
        data  = payload
    return event, data


def get_review_type(comment):
    """AI-classify a negative review (used for 1-3 star reviews only)."""
    prompt = (
        "Classify this hairpiece customer review into ONE of these categories: "
        "Product Quality, Shipping/Delivery, Customer Service, Fit/Sizing, "
        "Other. Reply with only the category name.\n\n"
        f"Review: \"{comment}\""
    )
    try:
        return vertex_call(prompt)
    except Exception as e:
        print(f"Vertex AI type error: {e}")
        return ""


def process_event(event, data):
    """Run the full pipeline. Called in a background thread."""
    name      = (data.get("consumer") or {}).get("displayName") or (data.get("consumer") or {}).get("name") or "A customer"
    rating    = str(data.get("stars", "Unknown"))
    title     = data.get("title") or ""
    text      = data.get("text") or ""
    review_id = data.get("reviewId") or data.get("id") or ""

    # Avoid duplicating the title when it's already the first line of the body
    if title and not text.lower().startswith(title.lower()):
        comment = f"{title}\n\n{text}".strip()
    else:
        comment = text.strip()

    if already_processed(review_id):
        return

    if event == "review.deleted":
        send_to_gchat_deleted(name, rating)
        return

    has_comment = len(comment) > 5
    email       = get_review_email(review_id) if review_id else ""
    reply       = get_reply_suggestion(comment, rating, name) if has_comment else ""
    suggestion  = get_gemini_suggestion(comment, rating) if has_comment else ""
    review_type = (
        get_review_type(comment)
        if has_comment and rating.isdigit() and int(rating) <= 3
        else ""
    )

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

    event, data = parse_payload(payload)
    print(f"Received event: {event} | format: {'B' if 'events' in payload else 'A'}")

    if event not in ("review.created", "review.updated", "review.deleted"):
        print(f"Ignored event type: {event}")
        return jsonify({"status": "ignored"})

    # Respond immediately so Trustpilot doesn't retry, then process in background
    threading.Thread(target=process_event, args=(event, data), daemon=True).start()
    return jsonify({"status": "ok"})


TEAMDESK_URL = "https://www.teamdesk.net/secure/api/v2/56554/5DFF4B9332254B81B81993A8E55B59F8/t_419099/upsert.json"


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
    url = (
        f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT}/locations/{VERTEX_LOCATION}/"
        f"publishers/google/models/{VERTEX_MODEL}:generateContent"
    )
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
                        "widgets": [{"textParagraph": {"text": comment}}]
                    },
                    {
                        "header": "💬 Reply Suggestion",
                        "widgets": [{"textParagraph": {"text": reply or "—"}}]
                    },
                    {
                        "header": "💡 Business Suggestion",
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
        f"🗑️ *Trustpilot Review Deleted*\n\n"
        f"*Customer:* {name}\n"
        f"*Was rated:* {rating}-star {stars}"
    )
    try:
        requests.post(GCHAT_WEBHOOK_URL, json={"text": text}, timeout=10)
    except Exception as e:
        print(f"Google Chat error: {e}")


@app.route("/monitor", methods=["GET", "POST"])
def monitor():
    """
    Gap-filling safety net — Cloud Scheduler calls this every 30 minutes.
    1. Read all review IDs already in sheet column J.
    2. Fetch the 50 most recent reviews from Trustpilot.
    3. Backfill anything not already in the sheet.
    """
    try:
        service = get_sheets_service()
        result  = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="B:J",   # B = name (legacy fallback), J = review ID
        ).execute()
        rows = result.get("values", [])

        # Column J is index 8 within the B:J slice
        known_ids   = {r[8].strip() for r in rows if len(r) > 8 and r[8].strip()}
        known_names = {r[0].lower().strip() for r in rows if r}

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
            if name.lower().strip() in known_names:
                continue
            missing.append(rv)

        print(f"Monitor: {len(reviews)} checked, {len(missing)} missing")

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

            has_comment = len(comment) > 5
            email       = get_review_email(rid) if rid else ""
            reply       = get_reply_suggestion(comment, rating, name) if has_comment else ""
            suggestion  = get_gemini_suggestion(comment, rating) if has_comment else ""
            review_type = (
                get_review_type(comment)
                if has_comment and rating.isdigit() and int(rating) <= 3
                else ""
            )

            send_to_gchat(name, email, rating, comment, reply, suggestion, "review.created", rid)
            write_to_sheet(name, email, rating, review_type, comment, reply, suggestion, rid)

            if rating.isdigit() and int(rating) <= 3:
                create_service_request(email, comment)

            print(f"Monitor: backfilled {name} ({rating} stars, id={rid})")

        return jsonify({"status": "ok", "checked": len(reviews), "backfilled": len(missing)})

    except Exception as e:
        print(f"Monitor error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
