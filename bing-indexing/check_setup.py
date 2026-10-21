"""
Preflight: what does Bing actually know about us right now?

Run this first. It answers, per store, three questions that decide whether the
other two scripts can do anything: is the site on the account, is it verified,
and is its sitemap already registered.
"""
import argparse

import config
from bing_api import BingWebmaster, BingApiError


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", action="append", help="ca|com|nl|fr|es|de (repeatable)")
    args = ap.parse_args()

    stores = config.select(args.store)
    api = BingWebmaster(config.bing_api_key())

    try:
        sites = api.get_user_sites()
    except BingApiError as exc:
        raise SystemExit(f"Could not list sites: {exc}")

    known = {}
    for s in sites:
        url = (s.get("Url") or "").rstrip("/")
        known[url] = s
    print(f"Bing Webmaster account holds {len(sites)} site(s).\n")

    print(f"{'store':6} {'site':34} {'on account':11} {'sitemap registered'}")
    print("-" * 82)
    for st in stores:
        site = known.get(st.origin)
        if not site:
            print(f"{st.key:6} {st.origin:34} {'NO':11} -")
            continue

        try:
            feeds = api.get_feeds(st.site_url)
            urls = {(f.get("Url") or "").split("?")[0] for f in feeds}
            registered = "yes" if st.sitemap_url in urls else f"no ({len(feeds)} other)"
        except BingApiError as exc:
            registered = f"error: {exc.status}"

        print(f"{st.key:6} {st.origin:34} {'yes':11} {registered}")

    print("\nQuota (URL submissions):")
    for st in stores:
        if st.origin not in known:
            continue
        try:
            q = api.get_url_submission_quota(st.site_url)
            print(f"  {st.key:4} daily={q.get('DailyQuota')} monthly={q.get('MonthlyQuota')}")
        except BingApiError as exc:
            print(f"  {st.key:4} quota unavailable ({exc.status})")


if __name__ == "__main__":
    main()
