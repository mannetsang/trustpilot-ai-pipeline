"""
Build the group dashboard's payload: every market, every source we can read.

    python group-dashboard/collect.py                 # all six, writes group_data.json
    python group-dashboard/collect.py --store ca com --no-sales
    python group-dashboard/collect.py --print

Sources, and what each can tell us today:

| Source               | Markets        | Note                                        |
|----------------------|----------------|---------------------------------------------|
| Search Console       | all six        | organic clicks, queries, pages, sitemaps    |
| BigCommerce catalogue| all six        | products, stock, brands, price bands        |
| BigCommerce orders   | ca, com, es    | the other three tokens are catalogue-scoped |
| Merchant Center      | ca             | 289630622 has not been granted to us        |

Where a source is missing, the gap is recorded with the reason and the fix, and
the dashboard shows it. A blank panel would imply zero; "not connected" does not.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "google-search-console"))

import bigcommerce as bc  # noqa: E402
from lib.stores import STORES, select  # noqa: E402

# Search Console rows are trimmed here rather than at the source: the Shopping
# dashboard's own tab wants the deeper lists, and this page carries six markets.
GROUP_TOP = {"query": 100, "page": 100, "country": 60, "device": 10, "searchAppearance": 20}
SALES_DAYS = 365


def search_console(days, verbose):
    """Organic search for all six, via the module the Shopping dashboard uses."""
    # Loaded by path: this file is also called collect.py, and relying on
    # sys.path order to tell the two apart is a trap for whoever edits next.
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location(
        "search_console_collect", os.path.join(REPO, "google-search-console", "collect.py"))
    sc_collect = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sc_collect)

    try:
        data = sc_collect.collect(days=days, verbose=verbose)
    except Exception as exc:  # noqa: BLE001 - one missing grant must not end the build
        return sc_collect.unavailable(f"{type(exc).__name__}: {exc}")

    for prop in data["properties"]:
        for window in prop.get("dimensions", {}).values():
            for dim, cap in GROUP_TOP.items():
                if dim in window:
                    window[dim] = window[dim][:cap]
    return data


def store_block(store, since_iso, want_sales, verbose):
    """Catalogue and sales for one store, each with its own failure mode."""
    entry = {"key": store.key, "market": store.market, "domain": store.domain,
             "origin": store.origin, "currency": store.currency}
    token = store.token()
    if not token:
        entry["error"] = f"no token stored ({store.token_secret})"
        return entry

    client = bc.Client(store, token)
    try:
        entry["store_name"] = (client.get("/v2/store") or {}).get("name")
    except bc.StoreError as exc:
        entry["store_name"] = None
        entry["store_note"] = f"store details unavailable: {exc}"

    t0 = time.time()
    try:
        entry["catalogue"] = bc.catalogue(client)
        if verbose:
            print(f"  {store.key}: catalogue {entry['catalogue']['products']} products "
                  f"({time.time() - t0:.0f}s)")
    except bc.StoreError as exc:
        entry["catalogue_error"] = str(exc)
        if verbose:
            print(f"  {store.key}: catalogue unavailable ({exc})")

    if want_sales:
        t1 = time.time()
        try:
            entry["sales"] = bc.sales(client, since_iso)
            if verbose:
                s = entry["sales"]
                print(f"  {store.key}: {s['orders_counted']:,} orders, "
                      f"{s['currency']} {s['revenue']:,.0f} ({time.time() - t1:.0f}s)")
        except bc.StoreError as exc:
            entry["sales_error"] = (
                "this store's API token has no order scope" if "403" in str(exc) else str(exc))
            if verbose:
                print(f"  {store.key}: sales unavailable ({entry['sales_error']})")
    return entry


def merchant_feed(verbose):
    """The .ca Merchant Center feed, from the Shopping dashboard's own builder.

    build_data.py already pulls the performance reports, every offer's status
    and the BigCommerce join, and it is the thing under test every night. Call
    it rather than reimplementing it; --no-search stops it repeating the Search
    Console pull this script has already done.
    """
    import importlib.util  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    dashboard = os.path.join(REPO, "google-merchant", "dashboard")
    sys.path.insert(0, dashboard)
    sys.path.insert(0, os.path.join(REPO, "google-merchant"))
    spec = importlib.util.spec_from_file_location("merchant_build_data", os.path.join(dashboard, "build_data.py"))
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        with tempfile.NamedTemporaryFile("r+", suffix=".json", delete=False) as fh:
            module.main(["--account", "5298296396", "--label", "CA", "--no-search", "--out", fh.name])
            data = json.load(open(fh.name, encoding="utf-8"))
        os.unlink(fh.name)
    except Exception as exc:  # noqa: BLE001 - one market's feed must not end the build
        if verbose:
            print(f"  merchant feed unavailable ({type(exc).__name__}: {exc})")
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    data.pop("search", None)   # this script collects that itself, for all six
    data["available"] = True
    if verbose:
        print(f"  merchant feed: {len(data['products'])} products, {data['variants']['offers']} offers")
    return data


def merchant_centre(verbose):
    """Merchant Center reach, per account. Only .ca has been granted to us."""
    accounts = [
        {"id": "5298296396", "name": "Superhairpieces.ca", "covers": ["ca"]},
        {"id": "289630622", "name": "Superhairpieces (.com and EU)", "covers": ["com", "nl", "fr", "es", "de"]},
        {"id": "670525760", "name": "Gen'C Beauty", "covers": []},
    ]
    try:
        from google.api_core import exceptions as gexc  # noqa: PLC0415
        from google.shopping.merchant_accounts_v1 import AccountsServiceClient, GetAccountRequest  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        for a in accounts:
            a.update(connected=False, reason=f"client library unavailable: {exc}")
        return accounts

    client = AccountsServiceClient()
    for a in accounts:
        try:
            live = client.get_account(request=GetAccountRequest(name=f"accounts/{a['id']}"))
            a.update(connected=True, account_name=live.account_name)
        except gexc.GoogleAPICallError as exc:
            a.update(connected=False, reason=type(exc).__name__)
        if verbose:
            print(f"  merchant {a['id']}: {'connected' if a['connected'] else a['reason']}")
    return accounts


def collect(stores=None, days=480, want_sales=True, want_merchant_feed=True, verbose=True):
    stores = stores or list(STORES)
    today = dt.date.today()
    since_iso = (today - dt.timedelta(days=SALES_DAYS)).isoformat() + "T00:00:00Z"

    if verbose:
        print("search console:")
    search = search_console(days, verbose)

    if verbose:
        print("bigcommerce:")
    with ThreadPoolExecutor(max_workers=3) as pool:
        store_data = list(pool.map(lambda s: store_block(s, since_iso, want_sales, verbose), stores))

    if verbose:
        print("merchant center:")
    accounts = merchant_centre(verbose)
    feed = merchant_feed(verbose) if want_merchant_feed else {"available": False, "reason": "skipped"}

    return {
        "generated": today.isoformat(),
        "sales_since": since_iso[:10],
        "search": search,
        "stores": store_data,
        "merchant_accounts": accounts,
        "merchant": feed,
        "coverage": coverage(store_data, search, accounts),
    }


def coverage(store_data, search, accounts):
    """One row per market per source: connected, or the reason and the fix."""
    sc_by_key = {p["key"]: p for p in search.get("properties", [])}
    mc_by_market = {}
    for a in accounts:
        for market in a.get("covers", []):
            mc_by_market[market] = a

    rows = []
    for store in store_data:
        key = store["key"]
        prop = sc_by_key.get(key, {})
        account = mc_by_market.get(key)
        rows.append({
            "market": key,
            "domain": store["domain"],
            "search_console": {
                "ok": bool(prop.get("available")),
                "detail": prop.get("site_url") if prop.get("available") else prop.get("reason", "not collected"),
                "fix": "" if prop.get("available") else
                       "Search Console > Settings > Users and permissions > add the claude-sessions service account",
            },
            "catalogue": {
                "ok": "catalogue" in store,
                "detail": f"{store.get('catalogue', {}).get('products', 0)} products" if "catalogue" in store
                          else store.get("catalogue_error", store.get("error", "unavailable")),
                "fix": "" if "catalogue" in store else "check the store's API token",
            },
            "orders": {
                "ok": "sales" in store,
                "detail": (f"{store['sales']['orders_counted']:,} orders in the window"
                           if "sales" in store else store.get("sales_error", store.get("error", "unavailable"))),
                "fix": "" if "sales" in store else
                       "BigCommerce > Settings > API accounts: this token needs Orders (read-only)",
            },
            "merchant_center": {
                "ok": bool(account and account.get("connected")),
                "detail": (f"account {account['id']}" if account and account.get("connected")
                           else f"account {account['id']} not granted" if account else "no account covers this market"),
                "fix": "" if (account and account.get("connected")) else
                       "Merchant Center > People and access > add the claude-sessions service account as Admin",
            },
        })
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", nargs="+", help="Market keys; default all six")
    ap.add_argument("--days", type=int, default=480, help="Search Console history window")
    ap.add_argument("--no-sales", action="store_true", help="Skip the order pull (much faster)")
    ap.add_argument("--no-feed", action="store_true", help="Skip the .ca Merchant Center feed pull")
    ap.add_argument("--out", default=os.path.join(HERE, "group_data.json"))
    ap.add_argument("--print", action="store_true", dest="show")
    args = ap.parse_args(argv)

    data = collect(select(args.store), days=args.days, want_sales=not args.no_sales,
                   want_merchant_feed=not args.no_feed)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    size = os.path.getsize(args.out) / 1024
    print(f"wrote {args.out} ({size:.0f} KB)")

    if args.show:
        for row in data["coverage"]:
            marks = " ".join(f"{name}={'ok' if row[name]['ok'] else 'no'}"
                             for name in ("search_console", "catalogue", "orders", "merchant_center"))
            print(f"  {row['market']:<4} {row['domain']:<28} {marks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
