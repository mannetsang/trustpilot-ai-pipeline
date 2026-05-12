import os
import base64
import json
import time
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
BU_ID             = "5e44f707d7d8c700011eaa10"
GCP_PROJECT       = "shp-ai-bot-2026"
VERTEX_LOCATION   = "us-central1"
VERTEX_MODEL      = "gemini-2.5-pro"

# Cache the Trustpilot access token so we don't re-fetch on every request
_tp_token = None
_tp_token_expiry = 0


def get_tp_token():
    global _tp_token, _tp_token_expiry
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


def write_to_sheet(name, email, rating, comment, reply, suggestion):
    try:
        service = get_sheets_service()
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="A:I",
            valueInputOption="USER_ENTERED",
            body={"values": [[
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),  # A: Timestamp
                name,                                                 # B: Customer Name
                email,                                                # C: Customer Email
                rating,                                               # D: Star Rating
                "",                                                   # E: Type (set manually)
                comment,                                              # F: Comment
                reply,                                                # G: Reply Suggestion
                suggestion,                                           # H: Business Suggestion
                "",                                                   # I: Remark (set manually)
            ]]},
        ).execute()
    except Exception as e:
        print(f"Sheet write error: {e}")


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = json.loads(request.get_data())
    except json.JSONDecodeError:
        return jsonify({"status": "bad request"}), 400

    event = payload.get("eventType")
    if event not in ("review.created", "review.updated", "review.deleted"):
        return jsonify({"status": "ignored"})

    name      = (payload.get("consumer") or {}).get("displayName") or "A customer"
    rating    = str(payload.get("stars", "Unknown"))
    title     = payload.get("title") or ""
    text      = payload.get("text") or ""
    review_id = payload.get("reviewId") or payload.get("id") or ""
    comment   = f"{title}\n\n{text}".strip() if title else text.strip()

    if event == "review.deleted":
        send_to_gchat_deleted(name, rating)
        return jsonify({"status": "ok"})

    email      = get_review_email(review_id) if review_id else ""
    reply      = get_reply_suggestion(comment, rating, name) if len(comment) > 5 else ""
    suggestion = get_gemini_suggestion(comment, rating) if len(comment) > 5 else ""

    send_to_gchat(name, email, rating, comment, reply, suggestion, event, review_id)
    write_to_sheet(name, email, rating, comment, reply, suggestion)

    if rating.isdigit() and int(rating) <= 3:
        create_service_request(email, comment)

    return jsonify({"status": "ok"})


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
