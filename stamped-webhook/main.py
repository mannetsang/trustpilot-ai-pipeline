import os
import re
import json
import time
import threading
from datetime import datetime, timedelta, timezone
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, Response, stream_with_context
import google.auth
import google.auth.transport.requests
from googleapiclient.discovery import build

app = Flask(__name__)

GCHAT_WEBHOOK_URL = os.environ["GCHAT_WEBHOOK_URL"]
GOOGLE_SHEET_ID   = os.environ["GOOGLE_SHEET_ID"]
GCP_PROJECT       = os.environ.get("GCP_PROJECT", "shp-ai-bot-2026")
VERTEX_LOCATION   = os.environ.get("VERTEX_LOCATION", "us-central1")
VERTEX_MODEL      = os.environ.get("VERTEX_MODEL", "gemini-2.5-pro")
STAMPED_PUBLIC_KEY  = os.environ.get("STAMPED_PUBLIC_KEY", "")
STAMPED_PRIVATE_KEY = os.environ.get("STAMPED_PRIVATE_KEY", "")
STAMPED_STORE_HASH  = os.environ.get("STAMPED_STORE_HASH", "")
VERTEX_FAST_MODEL = "gemini-2.0-flash-001"

# Common female first names used in women's hairpiece product titles
FEMALE_NAMES = {
    "aaliyah", "abigail", "ada", "adeline", "adriana", "agatha", "agnes", "alexa",
    "alexis", "alice", "alicia", "alison", "allison", "alyssa", "amanda", "amber",
    "amelia", "amira", "amy", "ana", "andrea", "angela", "angelina", "anna",
    "annabelle", "anne", "annette", "april", "aria", "ariana", "arianna", "ashley",
    "audrey", "aurora", "autumn", "ava", "avery", "barbara", "beatrice", "bella",
    "beverly", "bianca", "bonnie", "brenda", "brianna", "bridget", "brittany",
    "brooke", "brooklyn", "camila", "carol", "caroline", "carrie", "cassandra",
    "cassidy", "catherine", "cecilia", "charlene", "charlotte", "chelsea", "cheryl",
    "chloe", "christy", "cindy", "claire", "clara", "claudia", "cora", "courtney",
    "crystal", "daisy", "dana", "danielle", "daphne", "deborah", "delilah", "diana",
    "donna", "dorothy", "eileen", "eleanor", "elena", "eliana", "elise", "eliza",
    "elizabeth", "ella", "ellie", "emily", "emma", "erica", "eva", "evelyn",
    "everly", "faith", "fiona", "florence", "frances", "gabriella", "genesis",
    "georgia", "gianna", "gloria", "grace", "hailey", "hadley", "hannah", "harper",
    "hazel", "heather", "helen", "isabella", "isla", "ivy", "jade", "jasmine",
    "jean", "jennifer", "jessica", "josephine", "julia", "juliana", "julie",
    "juniper", "kayla", "kaylee", "kennedy", "kinsley", "kylie", "layla", "leah",
    "leila", "lillian", "liliana", "lily", "linda", "lisa", "london", "lucia",
    "lucy", "luna", "lydia", "mackenzie", "madeline", "madelyn", "madison", "maria",
    "marie", "marina", "margaret", "marilyn", "maya", "megan", "melanie", "melissa",
    "mia", "michelle", "mila", "molly", "monica", "morgan", "naomi", "natalia",
    "natalie", "nevaeh", "nicole", "nora", "nova", "olivia", "paige", "paisley",
    "patricia", "penelope", "peyton", "piper", "quinn", "rachel", "reagan", "rebecca",
    "riley", "rose", "ruby", "rylee", "samantha", "sandra", "sandy", "sarah",
    "savannah", "scarlett", "serenity", "skylar", "sofia", "sophia", "sophie",
    "stella", "stephanie", "stacy", "sydney", "taylor", "tiffany", "tracy",
    "valentina", "vanessa", "veronica", "victoria", "violet", "vivian", "willow",
    "zoe", "zoey",
}


def get_product_info(title: str):
    """Returns (alias, product_type) for a product title.

    Extension: title contains the word 'extension'
    Men:       short alphanumeric code e.g. NM102V, HS12BSC
    Women:     title contains a known female first name
    Supplies:  everything else
    """
    if not title:
        return title, "Supplies"

    # Extension products
    if re.search(r'\bextension', title, re.IGNORECASE):
        return title, "Extension"

    # Men's products: letter(s) + digits + optional suffix
    men_match = re.search(r'\b([A-Za-z]{1,3}\d{2,3}(?:bsc|v)?)\b', title, re.IGNORECASE)
    if men_match:
        return men_match.group(1).upper(), "Men"

    # Women's products: contains a known female name
    for word in re.findall(r'\b[A-Za-z]+\b', title):
        if word.lower() in FEMALE_NAMES:
            return word.capitalize(), "Women"

    return title, "Supplies"


def get_sheets_service():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds)


def write_to_sheet(row: list):
    try:
        service = get_sheets_service()
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Reviews!A:X",
            valueInputOption="USER_ENTERED",
            body={"values": [row]},
        ).execute()
    except Exception as e:
        print(f"Sheet write error: {e}")


def get_vertex_token():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def vertex_call(prompt: str, model: str = None) -> str:
    model = model or VERTEX_MODEL
    url = (
        f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT}/locations/{VERTEX_LOCATION}/"
        f"publishers/google/models/{model}:generateContent"
    )
    token = get_vertex_token()
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        },
        timeout=120,
    )
    candidates = r.json().get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    for part in parts:
        if part.get("text"):
            return part["text"].strip()
    return ""


def get_reply_suggestion(author: str, rating: int, title: str, message: str, product: str) -> str:
    named = author and author.strip().lower() not in ("anonymous", "a customer")
    who = f"A customer named {author}" if named else "An anonymous customer"
    prompt = (
        f"{who} left a {rating}-star review for the product "
        f'"{product}" at Superhairpieces with the following:\n'
        f'Title: "{title}"\nReview: "{message}"\n\n'
        "Write a warm, professional public reply from Superhairpieces to post on Stamped.io. "
        "Keep it to 2-3 sentences. Thank them, address their specific feedback, and invite "
        "them to reach out if needed. Do not use generic filler phrases. "
        + ("" if named else "Do not address them by name. ")
        + "Respond with only the reply text itself: no preamble, no options, no quotation marks, no sign-off."
    )
    try:
        return vertex_call(prompt)
    except Exception as e:
        print(f"Vertex AI reply error: {e}")
        return ""


def get_business_suggestion(rating: int, title: str, message: str, product: str) -> str:
    prompt = (
        f"A customer left a {rating}-star review for the product \"{product}\" "
        f"at our hairpiece company Superhairpieces:\n"
        f'Title: "{title}"\nReview: "{message}"\n\n'
        "Provide a brief 1-2 sentence actionable improvement suggestion for our internal team. "
        "If the review is fully positive, suggest a quick way to capitalise on it. "
        "Be concise and professional."
    )
    try:
        return vertex_call(prompt)
    except Exception as e:
        print(f"Vertex AI suggestion error: {e}")
        return ""


def get_issue_type(rating: int, title: str, message: str) -> str:
    """Extract ALL specific issue labels from the review, comma-separated."""
    content = (title + " " + message).strip()
    if not content:
        return ""
    prompt = (
        f"A customer left a {rating}-star review for a hairpiece/hair product:\n"
        f'"{content}"\n\n'
        "List ALL specific issues or experiences mentioned in this review.\n\n"
        "RULES:\n"
        "- Return multiple labels if multiple issues are mentioned — comma-separated.\n"
        "- NEVER use 'Product Quality' as a label — always name the exact root cause.\n"
        "- Dryness: dry, brittle, stiff, damaged feel, hay-like, rough\n"
        "- Shedding: losing hair, shedding, hair falling out\n"
        "- Tangles: tangled, hard to detangle, knotting up\n"
        "- Knotting: knotted, matted\n"
        "- Color Fading: color changed, faded, washed out, off color\n"
        "- Lace Tearing: lace ripped, tore, damaged lace\n"
        "- Knots Showing: knots visible, see-through knots\n"
        "- Thin Hair: thin, sparse, not enough density\n"
        "- Short Lifespan: didn't last, wore out quickly\n"
        "- Smell: bad smell, odor\n"
        "- Positive Experience: happy, great, love it, satisfied (only if no complaints)\n"
        "- Shipping & Delivery: late, lost, damaged in transit\n"
        "- Customer Service: support, staff, response\n"
        "- Fit & Comfort: too tight, loose, uncomfortable\n"
        "- Adhesive & Hold: not sticking, fell off, poor hold\n"
        "- Wrong Item: wrong color, wrong size, not as described\n"
        "- Returns & Refunds: return, refund, exchange\n"
        "- Value for Money: too expensive, not worth it, good value\n"
        "Only use a generic label if the review has zero specific details.\n\n"
        "Reply with ONLY the comma-separated labels, nothing else. Example: Dryness, Tangles, Color Fading"
    )
    try:
        result = vertex_call(prompt, model=VERTEX_FAST_MODEL)
        if not result:
            result = vertex_call(prompt, model=VERTEX_MODEL)
        if not result:
            return ""
        # Take first line, clean up
        line = result.splitlines()[0].strip().strip('"').strip("'").strip(".")
        # Normalise spacing around commas
        return ", ".join(p.strip() for p in line.split(",") if p.strip())
    except Exception as e:
        print(f"Vertex AI issue type error: {e}")
        return ""


VERIFIED_MAP = {0: "Unverified", 1: "Verified Purchase", 2: "Verified"}
RATING_EMOJI = {5: "🟢", 4: "🟢", 3: "🟡", 2: "🟠", 1: "🔴"}


REPLY_SIGNATURE = "Geeta - Superhairpieces"
STAMPED_REVIEWS_API = f"https://stamped.io/api/v2/{STAMPED_STORE_HASH}/dashboard/reviews"


def auto_reply_five_star(review_id, rating, reply: str) -> str:
    """Post the AI reply on a 5-star Stamped review. Returns a short status for the Chat card."""
    if int(rating or 0) != 5 or not review_id or not reply:
        return ""
    if re.search(r"[\[\]{}<>]", reply):
        print(f"Auto-reply skipped for {review_id}: reply contains a placeholder")
        return "skipped: AI reply looked like a template"
    if "\n" in reply.strip() or re.match(r"\s*(of course|sure|certainly|absolutely|okay|here( is| are|'s|’s))\b", reply, re.I) \
            or re.search(r"\breply\b[^.!?]{0,60}:", reply[:120], re.I):
        print(f"Auto-reply skipped for {review_id}: reply includes model preamble")
        return "skipped: AI reply included a preamble"
    auth = (STAMPED_PUBLIC_KEY, STAMPED_PRIVATE_KEY)
    try:
        r = requests.get(f"{STAMPED_REVIEWS_API}/{review_id}", auth=auth, timeout=30)
        if r.status_code != 200:
            print(f"Auto-reply skipped for {review_id}: review lookup HTTP {r.status_code}")
            return f"failed: review lookup HTTP {r.status_code}"
        if (r.json().get("reply") or "").strip():
            return "skipped: already replied"

        message = reply.strip()
        if "superhairpieces" not in message[-40:].lower():
            message = f"{message} {REPLY_SIGNATURE}"
        r = requests.post(f"{STAMPED_REVIEWS_API}/{review_id}/reply", auth=auth,
                          json={"reply": message, "isPublicReply": True}, timeout=30)
        if r.status_code >= 300:
            print(f"Auto-reply failed for {review_id}: HTTP {r.status_code} {r.text[:300]}")
            return f"failed: Stamped HTTP {r.status_code}"
        print(f"Auto-reply posted for {review_id}")
        return "posted"
    except Exception as e:
        print(f"Auto-reply error for {review_id}: {e}")
        return "failed: error"


def send_to_gchat(data: dict, reply: str, suggestion: str, topic: str, auto_reply: str = ""):
    author  = data.get("author", "A customer")
    email   = data.get("customerEmail", "")
    rating  = data.get("reviewRating", 0)
    title   = data.get("reviewTitle", "")
    message = data.get("reviewMessage", "")
    product = data.get("productName", "")
    product_url = data.get("productUrl", "")
    location = data.get("location", "")
    verified = VERIFIED_MAP.get(data.get("reviewVerifiedType", 0), "Unknown")

    stars = "⭐" * min(int(rating), 5)
    dot   = RATING_EMOJI.get(rating, "⚪")
    label = "New Stamped.io Review" if "creat" in topic.lower() else "Stamped.io Review Updated"

    email_line    = f"<br><b>Email:</b> {email}" if email else ""
    location_line = f"<br><b>Location:</b> {location}" if location else ""

    card = {
        "cardsV2": [{
            "cardId": str(data.get("id", "review")),
            "card": {
                "sections": [
                    {
                        "widgets": [{
                            "textParagraph": {
                                "text": (
                                    f"{dot} <b>{label}</b><br><br>"
                                    f"<b>Customer:</b> {author}{email_line}{location_line}<br>"
                                    f"<b>Rating:</b> {stars} ({rating}/5)<br>"
                                    f"<b>Product:</b> {product}<br>"
                                    f"<b>Verified:</b> {verified}"
                                )
                            }
                        }]
                    },
                    {
                        "header": f"💬 {title}" if title else "💬 Review",
                        "widgets": [{"textParagraph": {"text": message or "—"}}]
                    },
                    {
                        "header": "✍️ Reply (auto-posted on Stamped.io)" if auto_reply == "posted"
                                  else f"✍️ Reply Suggestion (auto-reply {auto_reply})" if auto_reply
                                  else "✍️ Reply Suggestion",
                        "widgets": [{"textParagraph": {"text": reply or "—"}}]
                    },
                    {
                        "header": "💡 Business Suggestion",
                        "widgets": [
                            {"textParagraph": {"text": suggestion or "—"}},
                            {"buttonList": {"buttons": [{
                                "text": "View Product",
                                "onClick": {"openLink": {"url": product_url or "https://superhairpieces.com"}},
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


def process_review(data: dict, topic: str):
    author   = data.get("author", "A customer")
    rating   = data.get("reviewRating", 0)
    title    = data.get("reviewTitle", "") or ""
    message  = data.get("reviewMessage", "") or ""
    product  = data.get("productName", "") or ""
    email    = data.get("customerEmail", "") or ""
    location = data.get("location", "") or ""
    verified = VERIFIED_MAP.get(data.get("reviewVerifiedType", 0), "Unknown")

    alias, product_type = get_product_info(product)

    has_content = len(title + message) > 5
    reply       = get_reply_suggestion(author, rating, title, message, product) if has_content else ""
    suggestion  = get_business_suggestion(rating, title, message, product) if has_content else ""
    issue_type  = get_issue_type(rating, title, message) if has_content else ""

    auto_reply = auto_reply_five_star(data.get("id"), rating, reply) if "creat" in topic else ""
    send_to_gchat(data, reply, suggestion, topic, auto_reply)

    date_created = (data.get("dateCreated") or "")[:10]

    row = [
        str(data.get("id", "")),                          # A: Review ID
        date_created,                                      # B: Date Created
        alias,                                             # C: Product Alias
        str(rating),                                       # D: Rating
        title,                                             # E: Title
        message,                                           # F: Body
        author,                                            # G: Author
        "",                                                # H: First Name (not in webhook)
        "",                                                # I: Last Name (not in webhook)
        email,                                             # J: Email
        product_type,                                      # K: Product Type
        location,                                          # L: Location
        "",                                                # M: Country ISO (not in webhook)
        data.get("productUrl", "") or "",                  # N: Product URL
        "",                                                # O: Order ID (not in webhook)
        data.get("widgetType", "") or "stamped-webhook",   # P: Review Source
        verified,                                          # Q: Verified Type
        "Published",                                       # R: State
        data.get("reviewReply", "") or "",                 # S: Reply (existing Stamped reply)
        "",                                                # T: Is Featured
        "",                                                # U: Is Recommend
        reply,                                             # V: AI Reply Suggestion
        suggestion,                                        # W: Business Suggestion
        issue_type,                                        # X: Issue Type
    ]
    write_to_sheet(row)


WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET:
        incoming = request.headers.get("X-Stamped-Webhook-Secret", "")
        if incoming != WEBHOOK_SECRET:
            print(f"Rejected webhook: invalid secret")
            return jsonify({"status": "unauthorized"}), 401

    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"status": "bad request"}), 400

    topic = request.headers.get("X-Stamped-Topic", "").lower()
    print(f"Received Stamped.io webhook | topic: {topic}")

    # Only process new review creation events; ignore updates, rewards, etc.
    if topic != "review/created":
        print(f"Ignored topic: {topic}")
        return jsonify({"status": "ignored"})

    # Payload can be a single object or wrapped in a list
    reviews = payload if isinstance(payload, list) else [payload]

    for review in reviews:
        threading.Thread(target=process_review, args=(review, topic), daemon=True).start()

    return jsonify({"status": "ok"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})


MONITOR_DAYS = 21  # unpublished reviews auto-publish after 14 days; look back past that
_monitor_lock = threading.Lock()


def stamped_review_to_data(rev: dict, source: str) -> dict:
    """Map a Stamped dashboard API review to the webhook field names process_review expects."""
    return {
        "id":                 rev.get("id"),
        "author":             rev.get("author", "A customer"),
        "customerEmail":      rev.get("email", ""),
        "reviewRating":       rev.get("rating", 0),
        "reviewTitle":        rev.get("title", "") or "",
        "reviewMessage":      rev.get("body", "") or "",
        "productName":        rev.get("productTitle", "") or "",
        "productUrl":         rev.get("productUrl", "") or "",
        "location":           rev.get("location", "") or "",
        "reviewVerifiedType": rev.get("verifiedType", 0),
        "dateCreated":        rev.get("dateCreated", ""),
        "widgetType":         source,
        "reviewReply":        rev.get("reply", "") or "",
    }


@app.route("/monitor", methods=["GET", "POST"])
def monitor():
    """Process recent Stamped reviews that are not yet in the sheet (by review ID, column A).
    Called by Cloud Scheduler every 30 minutes; Stamped no longer sends review/created webhooks."""
    if not _monitor_lock.acquire(blocking=False):
        return jsonify({"status": "busy"}), 409
    try:
        known = get_sheets_service().spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID, range="Reviews!A:A"
        ).execute().get("values", [])
        known_ids = {row[0].strip() for row in known if row and row[0].strip()}

        r = requests.get(STAMPED_REVIEWS_API, auth=(STAMPED_PUBLIC_KEY, STAMPED_PRIVATE_KEY),
                         params={"page": 1, "take": 100}, timeout=30)
        if r.status_code != 200:
            print(f"Monitor: Stamped HTTP {r.status_code} {r.text[:200]}")
            return jsonify({"status": "error", "stamped_status": r.status_code}), 502
        cutoff = (datetime.now(timezone.utc) - timedelta(days=MONITOR_DAYS)).strftime("%Y-%m-%d")
        recent = [item.get("review", {}) for item in r.json().get("results", [])]
        missing = [rev for rev in recent
                   if (rev.get("dateCreated") or "")[:10] >= cutoff and str(rev.get("id")) not in known_ids]
        print(f"Monitor: {len(recent)} reviews checked, {len(missing)} missing")

        for rev in sorted(missing, key=lambda x: x.get("dateCreated", "")):
            process_review(stamped_review_to_data(rev, "stamped-monitor"), "review/created")
            print(f"Monitor: processed {rev.get('author')} ({rev.get('rating')} stars, id={rev.get('id')})")
        return jsonify({"status": "ok", "checked": len(recent), "processed": len(missing)})
    except Exception as e:
        print(f"Monitor error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        _monitor_lock.release()


@app.route("/backfill-since", methods=["GET", "POST"])
def backfill_since():
    secret = request.args.get("secret", "")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        return jsonify({"status": "unauthorized"}), 401

    since = request.args.get("since", "2026-06-18")

    # Collect all published reviews since the given date from Stamped API
    all_reviews = []
    for page in range(1, 20):
        try:
            r = requests.get(
                f"https://stamped.io/api/v2/{STAMPED_STORE_HASH}/dashboard/reviews",
                auth=(STAMPED_PUBLIC_KEY, STAMPED_PRIVATE_KEY),
                params={"page": page, "take": 100},
                timeout=30,
            )
            items = r.json().get("results", [])
        except Exception as e:
            break
        done = False
        for item in items:
            rev = item.get("review", {})
            dc = (rev.get("dateCreated") or "")[:10]
            if dc >= since:
                all_reviews.append(rev)
            else:
                done = True
                break
        if done or not items:
            break

    def generate():
        yield f"Found {len(all_reviews)} reviews since {since}\n"
        for rev in sorted(all_reviews, key=lambda x: x.get("dateCreated", "")):
            rev_id = rev.get("id")
            date   = (rev.get("dateCreated") or "")[:10]
            rating = rev.get("rating", 0)
            email  = rev.get("email", "")
            yield f"Processing ID={rev_id} | {date} | {rating}star | {email}\n"
            data = stamped_review_to_data(rev, "stamped-api-backfill")
            try:
                process_review(data, "review/created")
                yield f"  OK: {(rev.get('title') or '')[:50]}\n"
            except Exception as e:
                yield f"  ERROR: {e}\n"
        yield "Backfill complete.\n"

    return Response(stream_with_context(generate()), mimetype="text/plain")











if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
