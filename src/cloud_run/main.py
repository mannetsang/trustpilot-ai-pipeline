import os
import hmac
import hashlib
import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

GCHAT_WEBHOOK_URL  = os.environ["GCHAT_WEBHOOK_URL"]
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]
TRUSTPILOT_SECRET  = os.environ.get("TRUSTPILOT_SECRET", "")


@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_data()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return jsonify({"status": "bad request"}), 400

    event = payload.get("eventType")
    if event not in ("review.created", "review.updated", "review.deleted"):
        return jsonify({"status": "ignored"})

    name    = (payload.get("consumer") or {}).get("displayName") or "A customer"
    rating  = str(payload.get("stars", "Unknown"))
    title   = payload.get("title") or ""
    text    = payload.get("text") or ""
    comment = f"{title}\n\n{text}".strip() if title else text.strip()

    if event == "review.deleted":
        send_to_gchat_deleted(name, rating)
        return jsonify({"status": "ok"})

    suggestion = get_gemini_suggestion(comment, rating) if len(comment) > 5 else ""
    send_to_gchat(name, rating, comment, suggestion, event)

    return jsonify({"status": "ok"})


def get_gemini_suggestion(comment, rating):
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-pro:generateContent?key={GEMINI_API_KEY}"
    )
    prompt = (
        f"A customer left a {rating}-star review for our hairpiece company "
        f"(Superhairpieces) with the following comment:\n\"{comment}\"\n\n"
        "Provide a brief 1-2 sentence actionable improvement suggestion for our "
        "internal team. If the review is fully positive, suggest a quick way to "
        "capitalise on it. Be concise and professional."
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4},
    }
    try:
        r = requests.post(url, json=body, timeout=30)
        data = r.json()
        parts = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [])
        )
        for part in parts:
            if part.get("text"):
                return part["text"].strip()
    except Exception as e:
        print(f"Gemini error: {e}")
    return "AI suggestion unavailable."


def send_to_gchat(name, rating, comment, suggestion, event):
    stars = "⭐" * min(int(rating) if rating.isdigit() else 0, 5)
    label = "New Trustpilot Review" if event == "review.created" else "Trustpilot Review Updated"
    text = (
        f"{stars} *{label}* {stars}\n\n"
        f"*Customer:* {name}\n"
        f"*Rating:* {rating}-star\n\n"
        f"*Comment:*\n\"{comment}\"\n\n"
        f"💡 *AI Suggestion:*\n{suggestion}"
    )
    try:
        requests.post(GCHAT_WEBHOOK_URL, json={"text": text}, timeout=10)
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
