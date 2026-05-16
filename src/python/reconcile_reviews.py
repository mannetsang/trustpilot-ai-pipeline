"""
Backfills and monitors Trustpilot reviews against the Google Sheet.

Fetches reviews from the Trustpilot private API for the business unit,
diffs them against the Sheet (by ID in column H, falling back to a
content fingerprint for legacy rows that pre-date the ID column), and
replays the missing ones through the Cloud Run /webhook endpoint so
each one flows through Gemini -> Chat -> Sheet like a live event.

Run modes:
    # One-shot historical backfill (run locally)
    python src/python/reconcile_reviews.py --since 2020-01-01T00:00:00Z

    # Scheduled reconciler (Cloud Run Job invoked by Cloud Scheduler)
    python src/python/reconcile_reviews.py --since-hours 48

Required env vars:
    TP_API_KEY, TP_SECRET   - Trustpilot API credentials
    GOOGLE_SHEET_ID         - Sheet that the cloud_run service writes to
    WEBHOOK_URL             - https://<cloud-run-host>/webhook
    TP_BUSINESS_UNIT_ID     - (optional) defaults to superhairpieces.com
"""

import argparse
import base64
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import google.auth
import requests
from googleapiclient.discovery import build

TP_API_KEY  = os.environ["TP_API_KEY"]
TP_SECRET   = os.environ["TP_SECRET"]
BU_ID       = os.environ.get("TP_BUSINESS_UNIT_ID", "5e44f707d7d8c700011eaa10")
SHEET_ID    = os.environ["GOOGLE_SHEET_ID"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]


def get_tp_token():
    creds = base64.b64encode(f"{TP_API_KEY}:{TP_SECRET}".encode()).decode()
    r = requests.post(
        "https://api.trustpilot.com/v1/oauth/oauth-business-users-for-applications/accesstoken",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_reviews(token, since=None):
    reviews = []
    page = 1
    per_page = 100
    while True:
        params = {
            "apikey": TP_API_KEY,
            "perPage": per_page,
            "page": page,
            "orderBy": "createdat.desc",
        }
        if since:
            params["startDateTime"] = since
        r = requests.get(
            f"https://api.trustpilot.com/v1/private/business-units/{BU_ID}/reviews",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json().get("reviews", [])
        if not batch:
            break
        reviews.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return reviews


def _fingerprint(name, rating, comment):
    prefix = (comment or "").strip()[:80].lower()
    return (str(rating).strip(), (name or "").strip().lower(), prefix)


def review_fingerprint(review):
    consumer = review.get("consumer") or {}
    name = consumer.get("displayName") or "A customer"
    title = review.get("title") or ""
    text = review.get("text") or ""
    comment = f"{title}\n\n{text}".strip() if title else text.strip()
    return _fingerprint(name, review.get("stars"), comment)


def sheet_state():
    """Return (set of review IDs, set of content fingerprints) from the Sheet."""
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range="A:H",
    ).execute()
    ids = set()
    fingerprints = set()
    for row in result.get("values", []):
        row = (row + [""] * 8)[:8]
        _ts, name, _email, rating, comment, _reply, _suggestion, rid = row
        if rid:
            ids.add(rid)
        if name and rating:
            fingerprints.add(_fingerprint(name, rating, comment))
    return ids, fingerprints


def to_webhook_payload(review):
    consumer = review.get("consumer") or {}
    return {
        "eventType": "review.created",
        "reviewId": review.get("id"),
        "id": review.get("id"),
        "stars": review.get("stars"),
        "title": review.get("title"),
        "text": review.get("text"),
        "consumer": {"displayName": consumer.get("displayName") or "A customer"},
    }


def replay(review, dry_run=False):
    payload = to_webhook_payload(review)
    label = f"{payload['reviewId']} ({payload['stars']}*)"
    if dry_run:
        print(f"[dry-run] would replay {label}")
        return True
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=120)
        snippet = r.text.strip()[:100]
        print(f"replay {label}: HTTP {r.status_code} {snippet}")
        return r.ok
    except requests.RequestException as e:
        print(f"replay {label}: ERROR {e}")
        return False


def parse_since(since, since_hours):
    if since:
        return since
    if since_hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help="ISO8601 lower bound, e.g. 2020-01-01T00:00:00Z")
    ap.add_argument("--since-hours", type=int, help="Lower bound as 'now - N hours'")
    ap.add_argument("--max", type=int, default=None, help="Cap replays per run")
    ap.add_argument("--sleep", type=float, default=0.5, help="Delay between replays")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    since = parse_since(args.since, args.since_hours)

    print(f"Fetching reviews for BU {BU_ID}" + (f" since {since}" if since else ""))
    token = get_tp_token()
    reviews = fetch_reviews(token, since=since)
    print(f"  Trustpilot returned {len(reviews)} reviews")

    ids, fingerprints = sheet_state()
    print(f"  Sheet has {len(ids)} IDs and {len(fingerprints)} content fingerprints")

    missing = []
    for r in reviews:
        rid = r.get("id")
        if not rid:
            continue
        if rid in ids:
            continue
        if review_fingerprint(r) in fingerprints:
            continue
        missing.append(r)
    print(f"  {len(missing)} missing")

    if args.max:
        missing = missing[: args.max]
        print(f"  capped to first {len(missing)}")

    failed = 0
    for review in missing:
        if not replay(review, dry_run=args.dry_run):
            failed += 1
        if not args.dry_run:
            time.sleep(args.sleep)

    print(f"done. replayed={len(missing) - failed} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
