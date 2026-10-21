"""
Ping Bing with the URLs that changed since the last run.

Per store: ask BigCommerce which visible products were modified since our
watermark, then push those URLs through the Bing Webmaster URL Submission API
(and IndexNow too, if a key file is ever hostable - see indexnow.py).

The watermark advances only after a store's submissions all succeed, so a
failed run re-sends the same window next time instead of losing it.

    python ping_changed.py                      # dry run, all six stores
    python ping_changed.py --submit             # live
    python ping_changed.py --store ca --submit
    python ping_changed.py --since 2026-10-01T00:00:00Z --store ca --submit
    python ping_changed.py --since-minutes 20 --submit    # what the cron runs
"""
import argparse
import datetime as dt
import json
import os

import bc
import config
import indexnow
from bing_api import BingWebmaster, BingApiError

WATERMARK_FILE = os.path.join(config.STATE_DIR, "watermarks.json")
BATCH = 500           # URLs per SubmitUrlBatch call
OVERLAP_MINUTES = 5   # re-cover a small window, to survive clock skew


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_watermarks() -> dict:
    try:
        with open(WATERMARK_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_watermark(store_key: str, value: str):
    os.makedirs(config.STATE_DIR, exist_ok=True)
    marks = load_watermarks()
    marks[store_key] = value
    tmp = WATERMARK_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(marks, fh, indent=2, sort_keys=True)
    os.replace(tmp, WATERMARK_FILE)


def _with_overlap(iso: str) -> str:
    try:
        t = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return iso
    return (t - dt.timedelta(minutes=OVERLAP_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", action="append", help="ca|com|nl|fr|es|de (repeatable)")
    ap.add_argument("--submit", action="store_true", help="actually submit")
    ap.add_argument("--since", help="override the watermark, e.g. 2026-10-01T00:00:00Z")
    ap.add_argument("--since-minutes", type=int,
                    help="look back this many minutes instead of using the watermark; "
                         "stateless, so this is what the scheduled run uses")
    ap.add_argument("--all", action="store_true",
                    help="every visible product, ignoring the watermark (burns quota)")
    ap.add_argument("--limit", type=int, help="cap URLs per store (testing)")
    args = ap.parse_args()

    stores = config.select(args.store)
    marks = load_watermarks()
    api = BingWebmaster(config.bing_api_key()) if args.submit else None
    started = _now_iso()

    verified = set()
    if args.submit:
        verified = {(s.get("Url") or "").rstrip("/") for s in api.get_user_sites()}

    exit_code = 0
    for st in stores:
        if args.all:
            since = None
        elif args.since_minutes:
            since = (dt.datetime.now(dt.timezone.utc)
                     - dt.timedelta(minutes=args.since_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif args.since:
            since = args.since
        else:
            prev = marks.get(st.key)
            since = _with_overlap(prev) if prev else None

        window = since or "(everything)"
        try:
            urls = bc.changed_product_urls(st, st.token(), since, limit=args.limit)
        except Exception as exc:  # credential or API failure, reported per store
            print(f"{st.key:4} FAIL  BigCommerce: {exc}")
            exit_code = 1
            continue

        if not urls:
            print(f"{st.key:4} --    nothing changed since {window}")
            if args.submit and not (args.since_minutes or args.since or args.all):
                save_watermark(st.key, started)
            continue

        print(f"{st.key:4} {len(urls):5d} changed since {window}")
        for u in urls[:3]:
            print(f"       e.g. {u}")

        if not args.submit:
            continue

        if st.origin not in verified:
            print(f"{st.key:4} SKIP  {st.origin} is not verified on the Bing account")
            exit_code = 1
            continue

        ok = True
        for i in range(0, len(urls), BATCH):
            chunk = urls[i:i + BATCH]
            try:
                api.submit_url_batch(st.site_url, chunk)
                print(f"{st.key:4} OK    submitted {len(chunk)} URLs to Bing")
            except BingApiError as exc:
                print(f"{st.key:4} FAIL  Bing batch {i // BATCH + 1}: {exc}")
                ok = False
                exit_code = 1
                break

        # Secondary transport. Disabled until a root key file exists; a clear
        # one-line reason beats a silent skip.
        try:
            for status, meaning in indexnow.submit(st, urls):
                print(f"{st.key:4} IN    IndexNow {status}: {meaning}")
        except indexnow.IndexNowDisabled as exc:
            print(f"{st.key:4} IN    skipped ({exc})")

        if ok and not (args.since_minutes or args.since or args.all):
            save_watermark(st.key, started)

    if not args.submit:
        print("\nDry run. Re-run with --submit to send these to Bing.")
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
