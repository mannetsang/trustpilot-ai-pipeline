"""
Reddit -> Google Chat notifier.

Reddit has no push webhooks for subreddit activity, so this service polls the
subreddit's public RSS (Atom) feeds on a schedule (Cloud Scheduler hitting
/poll) and posts a Google Chat card for every entry it hasn't seen before —
to the channel it looks like a webhook, just with up-to-poll-interval latency.

Feeds watched per subreddit:
  posts     https://www.reddit.com/r/<sub>/new/.rss      (new submissions)
  comments  https://www.reddit.com/r/<sub>/comments/.rss (new comments)

State (the newest entry timestamp already announced, plus a small set of
recently-notified entry IDs for de-duplication) lives in Firestore, one
document per (subreddit, feed kind), so it survives restarts and cold starts.

Env vars:
  REDDIT_SUBREDDITS        comma-separated subreddit names, e.g. "HairSystem"
                           (no "r/" prefix needed; it is stripped if present).
  REDDIT_FEEDS             which feeds to watch: "posts" (default) or
                           "posts,comments".
  REDDIT_GCHAT_WEBHOOK_URL incoming webhook URL of the target Chat space.
  POLL_TOKEN               (optional) shared secret; if set, /poll requires
                           ?token=...
  REDDIT_USER_AGENT        (optional) UA sent to reddit; a descriptive UA is
                           required or reddit throttles the default one hard.
  REDDIT_FEED_LIMIT        (optional) entries fetched per feed, default 25.
"""

import html
import os
import re
import time
from datetime import datetime, timezone

import feedparser
import requests
from flask import Flask, request, jsonify
from google.cloud import firestore

app = Flask(__name__)

SUBREDDITS = [
    s.strip().removeprefix("r/").removeprefix("/r/")
    for s in os.environ.get("REDDIT_SUBREDDITS", "").split(",")
    if s.strip()
]
FEED_KINDS = [
    k.strip().lower()
    for k in os.environ.get("REDDIT_FEEDS", "posts").split(",")
    if k.strip().lower() in ("posts", "comments")
]
GCHAT_WEBHOOK_URL = os.environ.get("REDDIT_GCHAT_WEBHOOK_URL", "")
POLL_TOKEN = os.environ.get("POLL_TOKEN", "")
USER_AGENT = os.environ.get(
    "REDDIT_USER_AGENT",
    "web:reddit-chat-notifier:1.0 (subreddit activity to Google Chat)",
)
FEED_LIMIT = int(os.environ.get("REDDIT_FEED_LIMIT", "25"))

FEED_URL = {
    "posts": "https://www.reddit.com/r/{sub}/new/.rss?limit={limit}",
    "comments": "https://www.reddit.com/r/{sub}/comments/.rss?limit={limit}",
}

# Firestore state, one doc per (subreddit, kind).
_db = firestore.Client()
STATE_COLLECTION = _db.collection("reddit_notifier")

# How many recently-notified ids to keep per feed for de-dup (safety net on
# top of the timestamp watermark, in case two entries share a timestamp).
DEDUP_KEEP = 100


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
def state_doc(sub, kind):
    return STATE_COLLECTION.document(f"{sub.lower()}__{kind}")


def load_state(sub, kind):
    snap = state_doc(sub, kind).get()
    if snap.exists:
        data = snap.to_dict() or {}
        return data.get("last_timestamp"), set(data.get("notified_ids", []))
    return None, set()


def save_state(sub, kind, last_timestamp, notified_ids):
    state_doc(sub, kind).set({
        "last_timestamp": last_timestamp,
        "notified_ids": list(notified_ids)[-DEDUP_KEEP:],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Reddit RSS
# ---------------------------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
# Reddit post bodies end with boilerplate: submitted by /u/x [link] [comments]
BOILERPLATE_RE = re.compile(r"submitted by\s+/u/\S+.*$", re.IGNORECASE | re.DOTALL)


def html_to_text(raw):
    text = TAG_RE.sub(" ", raw or "")
    text = html.unescape(text)
    text = BOILERPLATE_RE.sub("", text)
    return WS_RE.sub(" ", text).strip()


def entry_timestamp(entry):
    """Entry publish time as an aware UTC datetime, or None."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def fetch_feed(sub, kind):
    url = FEED_URL[kind].format(sub=sub, limit=FEED_LIMIT)
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    if r.status_code == 429:  # reddit rate limit: back off once, then give up
        time.sleep(10)
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    entries = []
    for e in parsed.entries:
        ts = entry_timestamp(e)
        eid = e.get("id")  # e.g. t3_1abcde (post) / t1_1abcde (comment)
        if not eid or ts is None:
            continue
        content = ""
        if e.get("content"):
            content = e.content[0].get("value", "")
        elif e.get("summary"):
            content = e.summary
        thumb = ""
        media = e.get("media_thumbnail") or []
        if media:
            thumb = media[0].get("url", "")
        entries.append({
            "id": eid,
            "title": html.unescape(e.get("title", "")).strip(),
            "link": e.get("link", f"https://www.reddit.com/r/{sub}"),
            "author": (e.get("author") or "").removeprefix("/u/"),
            "timestamp": ts,
            "text": html_to_text(content),
            "thumbnail": thumb,
        })
    return entries


# ---------------------------------------------------------------------------
# Google Chat
# ---------------------------------------------------------------------------
REDDIT_ICON = "https://www.redditstatic.com/desktop2x/img/favicon/apple-icon-76x76.png"
REDDIT_ORANGE = {"red": 1.0, "green": 0.27, "blue": 0.0, "alpha": 1.0}


def post_to_chat(sub, kind, entry):
    label = "\U0001f4dd New post" if kind == "posts" else "\U0001f4ac New comment"
    when = entry["timestamp"].astimezone().strftime("%b %d, %Y %I:%M %p")
    author = f"u/{entry['author']}" if entry["author"] else "unknown"

    snippet = entry["text"]
    if len(snippet) > 500:
        snippet = snippet[:500].rstrip() + "…"

    widgets = [{"textParagraph": {"text": f"<b>{html.escape(entry['title'])}</b>"}}]
    if snippet:
        widgets.append({"textParagraph": {"text": html.escape(snippet)}})
    if entry["thumbnail"]:
        widgets.append({"image": {
            "imageUrl": entry["thumbnail"],
            "onClick": {"openLink": {"url": entry["link"]}},
        }})
    widgets.append({"buttonList": {"buttons": [{
        "text": "View on Reddit",
        "onClick": {"openLink": {"url": entry["link"]}},
        "color": REDDIT_ORANGE,
    }]}})

    card = {
        "cardsV2": [{
            "cardId": entry["id"],
            "card": {
                "header": {
                    "title": f"{label} in r/{sub}",
                    "subtitle": f"{author} · {when}",
                    "imageUrl": REDDIT_ICON,
                    "imageType": "CIRCLE",
                },
                "sections": [{"widgets": widgets}],
            },
        }]
    }
    resp = requests.post(GCHAT_WEBHOOK_URL, json=card, timeout=10)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Poll logic
# ---------------------------------------------------------------------------
def poll_feed(sub, kind):
    """Poll one (subreddit, kind) feed. Returns a result dict."""
    entries = fetch_feed(sub, kind)
    last_iso, notified_ids = load_state(sub, kind)

    # First ever run: seed the watermark to the newest entry and send nothing,
    # so the channel isn't spammed with the back catalogue.
    if last_iso is None:
        newest = max((e["timestamp"] for e in entries), default=None)
        seed_ids = {e["id"] for e in entries}
        save_state(sub, kind, newest.isoformat() if newest else "", seed_ids)
        return {"feed": f"r/{sub}/{kind}", "status": "seeded",
                "known_entries": len(seed_ids)}

    last_dt = datetime.fromisoformat(last_iso) if last_iso else None

    new_items = [
        e for e in entries
        if e["id"] not in notified_ids
        and (last_dt is None or e["timestamp"] > last_dt)
    ]
    new_items.sort(key=lambda e: e["timestamp"])  # oldest first, chronological cards

    # The watermark only advances through the contiguous run of successful
    # sends (oldest-first). If a send fails, later successes are remembered in
    # notified_ids (so no duplicates), but the watermark stays behind the
    # failed entry so the next poll retries it.
    sent = []
    watermark_dt = last_dt
    advance = True
    for e in new_items:
        try:
            post_to_chat(sub, kind, e)
            notified_ids.add(e["id"])
            sent.append(e["id"])
            if advance and (watermark_dt is None or e["timestamp"] > watermark_dt):
                watermark_dt = e["timestamp"]
        except Exception as exc:
            print(f"Chat post error for {e['id']}: {exc}")
            advance = False

    new_watermark = watermark_dt.isoformat() if watermark_dt else (last_iso or "")
    save_state(sub, kind, new_watermark, notified_ids)

    return {"feed": f"r/{sub}/{kind}", "status": "ok", "new": len(sent),
            "sent_ids": sent}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def health():
    return "reddit-chat-notifier ok", 200


@app.route("/poll", methods=["GET", "POST"])
def poll():
    if POLL_TOKEN and request.args.get("token") != POLL_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    if not SUBREDDITS:
        return jsonify({"error": "REDDIT_SUBREDDITS is not set"}), 500
    if not GCHAT_WEBHOOK_URL:
        return jsonify({"error": "REDDIT_GCHAT_WEBHOOK_URL is not set"}), 500

    results, errors = [], []
    first = True
    for sub in SUBREDDITS:
        for kind in FEED_KINDS:
            if not first:
                time.sleep(3)  # courtesy gap between feeds; reddit 429s bursts
            first = False
            try:
                results.append(poll_feed(sub, kind))
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else "?"
                # 429 = reddit rate limit; just skip this cycle, next poll catches up.
                print(f"Reddit fetch error r/{sub}/{kind}: HTTP {code}")
                errors.append({"feed": f"r/{sub}/{kind}", "error": f"HTTP {code}"})
            except Exception as e:
                print(f"Error polling r/{sub}/{kind}: {e}")
                errors.append({"feed": f"r/{sub}/{kind}", "error": str(e)})

    status = 200 if results or not errors else 502
    return jsonify({"results": results, "errors": errors}), status


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
