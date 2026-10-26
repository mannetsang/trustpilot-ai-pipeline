"""
Merchant Center for the group page: the same questions, asked of any account.

`google-merchant/dashboard/build_data.py` is the .ca deep dive. It joins
Merchant offers to the BigCommerce catalogue to build the product explorer, and
it is wired to that one store: its data source is found by the name this repo's
own sync gives it, and its label indexes `sync_bigcommerce.STORES`, which holds
only "CA". Pointed at another account it either dies on that lookup or, worse,
quietly joins a second account's offers to superhairpieces.ca's catalogue and
reports the result as one storefront.

So this module is the other half: what a Merchant Center account can answer
about *itself*, with no BigCommerce join and no assumption about which
storefront it belongs to. It runs against all three accounts.

Two things it is careful about:

- **Currency.** Conversion value arrives per row with its own currency code, and
  one account alone returns two of them. Nothing here adds them; `value` is
  always a mapping of currency to amount, and a caller that wants one number has
  to pick a currency.
- **Feed label, not account.** An account being readable says nothing about a
  storefront being in Shopping. 289630622 is reachable and covers five
  storefronts on paper, but every offer in it carries feed label US. The four EU
  markets have no offers at all, and `markets()` reports that rather than
  letting the account's reachability stand in for coverage.
"""
import datetime as dt
from collections import Counter, defaultdict

# One Merchant Center feed label per storefront. Feed labels are the country
# code, which is what Shopping campaigns filter on, so they map cleanly to our
# markets - but only for the ones we actually sell through.
FEED_MARKET = {"CA": "ca", "US": "com", "NL": "nl", "FR": "fr", "ES": "es", "DE": "de"}

# "markets" is which of OUR six storefronts an account is allowed to serve, not
# what it happens to hold. Gen'C Beauty is a separate brand on its own domain:
# 4,117 of its offers carry feed label CA, and crediting those to
# superhairpieces.ca would double the .ca feed with another company's products.
ACCOUNTS = [
    {"id": "5298296396", "name": "Superhairpieces.ca", "scope": "superhairpieces.ca",
     "markets": ["ca"]},
    {"id": "289630622", "name": "Superhairpieces", "scope": "superhairpieces.com and the EU storefronts",
     "markets": ["com", "nl", "fr", "es", "de"]},
    {"id": "670525760", "name": "Gen'C Beauty", "scope": "gencbeauty.com",
     "markets": []},
]

PERF_DAYS = 365

# How a data source is fed. An API source is this repo pushing the catalogue; an
# autofeed is Google crawling the site and keeping what it can parse, which is
# why an autofeed account can hold a small fraction of its real catalogue.
INPUT_NOTE = {
    "API": "pushed by this repo",
    "FILE": "scheduled file fetch",
    "AUTOFEED": "Google crawls the site; nothing is pushed",
    "WEBSITE_CRAWL": "Google crawls the site; nothing is pushed",
}


def _clients():
    from google.shopping.merchant_accounts_v1 import AccountsServiceClient, GetAccountRequest
    from google.shopping.merchant_datasources_v1 import DataSourcesServiceClient, ListDataSourcesRequest
    from google.shopping.merchant_reports_v1 import ReportServiceClient, SearchRequest
    return {
        "accounts": (AccountsServiceClient(), GetAccountRequest),
        "sources": (DataSourcesServiceClient(), ListDataSourcesRequest),
        "reports": (ReportServiceClient(), SearchRequest),
    }


def _search(client, request_cls, parent, query):
    return list(client.search(request=request_cls(parent=parent, query=query, page_size=1000)))


def _sources(client, request_cls, parent):
    out = []
    for d in client.list_data_sources(request=request_cls(parent=parent)):
        primary = d.primary_product_data_source
        out.append({
            "name": d.display_name,
            "input": d.input.name,
            "how": INPUT_NOTE.get(d.input.name, d.input.name.lower().replace("_", " ")),
            "primary": bool(primary),
            "feed_label": (primary.feed_label or None) if primary else None,
            "language": (primary.content_language or None) if primary else None,
            "countries": list(primary.countries) if primary else [],
        })
    return out


def _offers(rows):
    """Offer counts by status and feed label, plus the issues behind them."""
    by_status = Counter()
    by_feed = Counter()
    by_currency = Counter()
    issues = Counter()
    for r in rows:
        p = r.product_view
        by_status[p.aggregated_reporting_context_status.name or "UNSPECIFIED"] += 1
        by_feed[p.feed_label or "(none)"] += 1
        if p.price.currency_code:
            by_currency[p.price.currency_code] += 1
        for issue in p.item_issues:
            severity = issue.severity.aggregated_severity.name if issue.severity else "UNSPECIFIED"
            code = issue.type_.code if issue.type_ else "unknown"
            attribute = (issue.type_.canonical_attribute or None) if issue.type_ else None
            issues[(severity, code, attribute)] += 1
    return {
        "offers": len(rows),
        "by_status": [{"status": s, "offers": n} for s, n in by_status.most_common()],
        "by_feed_label": [{"feed_label": f, "offers": n} for f, n in by_feed.most_common()],
        "price_currencies": [{"currency": c, "offers": n} for c, n in by_currency.most_common()],
        "issues": [{"severity": s, "code": c, "attribute": a, "offers": n}
                   for (s, c, a), n in issues.most_common(20)],
    }


def _performance(rows):
    """Clicks and impressions by marketing method; conversion value per currency.

    Clicks, impressions and conversion counts are countable across everything.
    Conversion *value* is money, and the rows carry more than one currency even
    inside a single account, so it stays split by currency the whole way.
    """
    method = defaultdict(lambda: {"clicks": 0, "impr": 0, "conv": 0.0})
    monthly = defaultdict(lambda: {"organic": 0, "ads": 0, "impr": 0, "conv": 0.0})
    value = Counter()
    days = set()
    for r in rows:
        v = r.product_performance_view
        name = v.marketing_method.name or "UNSPECIFIED"
        m = method[name]
        m["clicks"] += int(v.clicks or 0)
        m["impr"] += int(v.impressions or 0)
        m["conv"] += float(v.conversions or 0)
        if v.date.year:
            day = dt.date(v.date.year, v.date.month, v.date.day)
            days.add(day)
            bucket = monthly[day.strftime("%Y-%m")]
            bucket["organic" if name == "ORGANIC" else "ads"] += int(v.clicks or 0)
            bucket["impr"] += int(v.impressions or 0)
            bucket["conv"] += float(v.conversions or 0)
        cv = v.conversion_value
        if cv.currency_code and cv.amount_micros:
            value[cv.currency_code] += cv.amount_micros / 1e6
    return {
        "days_with_data": len(days),
        "first_day": min(days).isoformat() if days else None,
        "last_day": max(days).isoformat() if days else None,
        "clicks": sum(m["clicks"] for m in method.values()),
        "impressions": sum(m["impr"] for m in method.values()),
        "conversions": round(sum(m["conv"] for m in method.values()), 1),
        # Never one number: a caller that wants a total has to name a currency.
        "value": [{"currency": c, "amount": round(v, 2)} for c, v in value.most_common()],
        "by_method": [{"method": k, "clicks": v["clicks"], "impressions": v["impr"],
                       "conversions": round(v["conv"], 1)}
                      for k, v in sorted(method.items(), key=lambda kv: -kv[1]["clicks"])],
        "monthly": [{"m": k, **{f: (round(x, 1) if isinstance(x, float) else x) for f, x in v.items()}}
                    for k, v in sorted(monthly.items())],
    }


def account(entry, verbose=True):
    """Everything one Merchant Center account can say about itself."""
    out = dict(entry)
    parent = f"accounts/{entry['id']}"
    try:
        from google.api_core import exceptions as gexc
    except Exception as exc:  # noqa: BLE001 - the page must still build
        out.update(connected=False, reason=f"client library unavailable: {exc}")
        return out

    try:
        clients = _clients()
    except Exception as exc:  # noqa: BLE001
        out.update(connected=False, reason=f"client library unavailable: {exc}")
        return out

    acc_client, acc_req = clients["accounts"]
    try:
        live = acc_client.get_account(request=acc_req(name=parent))
        out.update(connected=True, account_name=live.account_name,
                   time_zone=live.time_zone.id or None, language=live.language_code or None)
    except gexc.GoogleAPICallError as exc:
        out.update(connected=False, reason=type(exc).__name__)
        if verbose:
            print(f"  merchant {entry['id']}: {out['reason']}")
        return out

    # Each section is allowed to fail on its own. A disapproval list we cannot
    # read is not a reason to drop the account's performance as well.
    src_client, src_req = clients["sources"]
    try:
        out["sources"] = _sources(src_client, src_req, parent)
    except gexc.GoogleAPICallError as exc:
        out["sources"], out["sources_error"] = [], type(exc).__name__

    rep_client, rep_req = clients["reports"]
    try:
        rows = _search(rep_client, rep_req, parent,
                       "SELECT id, offer_id, feed_label, price, "
                       "aggregated_reporting_context_status, item_issues FROM product_view")
        out["catalogue"] = _offers(rows)
    except gexc.GoogleAPICallError as exc:
        out["catalogue_error"] = f"{type(exc).__name__}: {exc}"[:200]

    today = dt.date.today()
    start = (today - dt.timedelta(days=PERF_DAYS)).isoformat()
    try:
        rows = _search(rep_client, rep_req, parent,
                       "SELECT date, marketing_method, clicks, impressions, conversions, "
                       f"conversion_value FROM product_performance_view "
                       f"WHERE date BETWEEN '{start}' AND '{today}'")
        out["performance"] = _performance(rows)
        out["performance"]["window"] = [start, today.isoformat()]
    except gexc.GoogleAPICallError as exc:
        out["performance_error"] = f"{type(exc).__name__}: {exc}"[:200]

    if verbose:
        cat = out.get("catalogue") or {}
        perf = out.get("performance") or {}
        print(f"  merchant {entry['id']}: {cat.get('offers', 0):,} offers, "
              f"{perf.get('clicks', 0):,} clicks in {PERF_DAYS}d")
    return out


def markets(accounts):
    """Which storefronts actually have offers in Shopping, and in which account.

    Reachability is not coverage. An account can be readable, and named as
    covering five storefronts, while every offer in it carries one feed label.
    This keys on the feed labels that came back, so a market with no offers
    reads as no feed rather than as a connected account.
    """
    found = {}
    for a in accounts:
        allowed = set(a.get("markets") or [])
        for row in (a.get("catalogue") or {}).get("by_feed_label", []):
            market = FEED_MARKET.get(row["feed_label"])
            # A feed label only counts for a market the account actually serves.
            # Gen'C Beauty ships CA and US labels of its own; they are not ours.
            if not market or market not in allowed:
                continue
            seen = found.setdefault(market, {"offers": 0, "accounts": []})
            seen["offers"] += row["offers"]
            seen["accounts"].append({"id": a["id"], "name": a["name"], "offers": row["offers"],
                                     "how": next((s["how"] for s in a.get("sources", [])
                                                  if s.get("primary")), None)})
    return found
