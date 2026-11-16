"""One-off: bring the Stamped.io Reviews sheet up to date with every Stamped store.

1. Copies each review's Stamped reply into column S wherever the sheet differs,
   so the reviews dashboard stops showing replied reviews as "Needs reply".
2. Appends every review the sheet doesn't have yet (mostly .com history), with the
   same Gemini fields the live pipeline writes. No Chat cards, no auto-replies.

Run as the Cloud Run job `stamped-history-import` (see deploy-stamped-webhook.yml).
Safe to re-run: rows are matched by review ID, so it resumes where it stopped.
Env: DRY_RUN=1 only counts; LIMIT=n imports at most n reviews; WORKERS (default 8).
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

os.environ.setdefault("GCHAT_WEBHOOK_URL", "unused-by-import")
import main as m  # noqa: E402

DRY_RUN = os.environ.get("DRY_RUN") == "1"
LIMIT = int(os.environ.get("LIMIT", "0") or 0)
WORKERS = int(os.environ.get("WORKERS", "8"))
BATCH = 40


def fetch_all(store: dict) -> list:
    reviews, page = [], 1
    while True:
        for attempt in range(4):
            try:
                r = requests.get(store["api"], auth=store["auth"], params={"page": page, "take": 100}, timeout=60)
                r.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt == 3:
                    raise
                print(f"  {store['name']} page {page}: {e}; retrying")
                time.sleep(5 * (attempt + 1))
        items = [x.get("review", {}) for x in r.json().get("results", [])]
        if not items:
            return reviews
        reviews += items
        page += 1


def row_for(rev: dict) -> list:
    data = m.stamped_review_to_data(rev, rev.get("reviewSource") or "stamped-history")
    ai = m.ai_fields(data["author"], data["reviewRating"], data["reviewTitle"], data["reviewMessage"], data["productName"])
    return m.build_row(data, ai)


def main():
    print(f"Stores: {[s['name'] for s in m.STAMPED_STORES]}  dry_run={DRY_RUN} limit={LIMIT or 'none'} workers={WORKERS}")
    everything = []
    for store in m.STAMPED_STORES:
        revs = fetch_all(store)
        print(f"  {store['name']}: {len(revs)} reviews on Stamped")
        everything += revs

    sheet = m.read_sheet_replies()
    print(f"Sheet has {len(sheet)} reviews")

    if DRY_RUN:
        stale = sum(1 for r in everything if (r.get("reply") or "").strip()
                    and str(r.get("id")) in sheet and sheet[str(r.get("id"))][1] != r["reply"].strip())
        print(f"DRY RUN: {stale} reply cells would be updated")
    else:
        print(f"Replies copied into column S: {m.sync_sheet_replies(everything, sheet)}")

    missing = sorted((r for r in everything if str(r.get("id")) not in sheet), key=lambda r: r.get("dateCreated", ""))
    if LIMIT:
        missing = missing[:LIMIT]
    print(f"Reviews to import: {len(missing)}")
    if DRY_RUN or not missing:
        return

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i in range(0, len(missing), BATCH):
            chunk = missing[i:i + BATCH]
            rows = list(pool.map(row_for, chunk))
            for attempt in range(4):
                try:
                    m.append_rows(rows)
                    break
                except Exception as e:
                    if attempt == 3:
                        raise
                    print(f"  append failed ({e}); retrying")
                    time.sleep(10 * (attempt + 1))
            done += len(rows)
            print(f"  imported {done}/{len(missing)} (through {chunk[-1].get('dateCreated', '')[:10]})", flush=True)
    print("Import complete")


if __name__ == "__main__":
    sys.exit(main())
