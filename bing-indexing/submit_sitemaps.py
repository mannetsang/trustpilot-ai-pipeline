"""
Submit each store's BigCommerce sitemap to Bing Webmaster Tools.

One sitemap index per store at <origin>/xmlsitemap.php; Bing walks the index
and picks up the child sitemaps itself, so only the index is submitted.

Submission is idempotent - re-running does not create duplicate feeds - so this
is safe to re-run after adding a store or re-verifying a site.
"""
import argparse

import config
from bing_api import BingWebmaster, BingApiError


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", action="append", help="ca|com|nl|fr|es|de (repeatable)")
    ap.add_argument("--submit", action="store_true",
                    help="actually submit; without it the run is a dry run")
    args = ap.parse_args()

    stores = config.select(args.store)
    api = BingWebmaster(config.bing_api_key())

    verified = {(s.get("Url") or "").rstrip("/") for s in api.get_user_sites()}

    failures = 0
    for st in stores:
        if st.origin not in verified:
            print(f"{st.key:4} SKIP  {st.origin} is not on the Bing account - "
                  f"add and verify it first")
            failures += 1
            continue

        if not args.submit:
            print(f"{st.key:4} DRY   would submit {st.sitemap_url}")
            continue

        try:
            api.submit_feed(st.site_url, st.sitemap_url)
            print(f"{st.key:4} OK    submitted {st.sitemap_url}")
        except BingApiError as exc:
            print(f"{st.key:4} FAIL  {st.sitemap_url} -> {exc}")
            failures += 1

    if not args.submit:
        print("\nDry run. Re-run with --submit to send these to Bing.")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
