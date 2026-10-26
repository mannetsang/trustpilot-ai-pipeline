"""
Pull everything the Search Console API will give us for the six storefronts.

    python google-search-console/collect.py --out search_data.json
    python google-search-console/collect.py --property ca --days 90 --print

One run is about fourteen calls per property: totals, a daily series, one call
per dimension, one per search type, and a fresh-data total. That is well inside
the quota (1,200 queries a minute per property) and takes a few seconds each.

Nothing here writes to Google. A property the credential cannot see is recorded
with the reason and skipped, so one missing grant never fails the run - the
dashboard shows which properties are connected and what to do about the rest.
"""
import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import surface  # noqa: E402
from config import PROPERTIES, select  # noqa: E402
from search_console import AccessDenied, SearchConsole, SearchConsoleError  # noqa: E402

# Keep the payload small enough to ship inside the dashboard page. Queries and
# pages are open-ended, so they get a cap; the rest are closed sets we want
# whole, because showing which of a dimension's possible values have data is
# half the point of the tab.
TOP_ROWS = {"query": 250, "page": 250, "country": 250, "device": 10, "searchAppearance": 50}
RECENT_CAP = 150          # queries and pages in the 28-day window
RECENT_DAYS = 28
MAX_HISTORY_DAYS = 480    # the API keeps 16 months


def _rows_to_list(rows, keys=1):
    """Turn API rows into compact records: keys first, then the four metrics."""
    out = []
    for r in rows:
        k = r.get("keys", [])
        out.append({
            "k": k[0] if keys == 1 and k else k,
            "clicks": r.get("clicks", 0),
            "impr": r.get("impressions", 0),
            "ctr": round(r.get("ctr", 0.0), 6),
            "pos": round(r.get("position", 0.0), 2),
        })
    return out


def _totals(rows):
    if not rows:
        return {"clicks": 0, "impr": 0, "ctr": 0, "pos": None}
    r = rows[0]
    return {
        "clicks": r.get("clicks", 0),
        "impr": r.get("impressions", 0),
        "ctr": round(r.get("ctr", 0.0), 6),
        "pos": round(r.get("position", 0.0), 2),
    }


def _sitemap(entry, parent=None):
    """One WmxSitemap, flattened. int64 fields arrive as quoted strings.

    `indexed` is not carried through: Google deprecated it in 2019 and stopped
    filling it in, so a column of zeroes would say something untrue.
    """
    return {
        "path": entry.get("path"), "type": entry.get("type"), "parent": parent,
        "last_submitted": entry.get("lastSubmitted"), "last_downloaded": entry.get("lastDownloaded"),
        "is_pending": entry.get("isPending", False), "is_index": entry.get("isSitemapsIndex", False),
        "errors": int(entry.get("errors", 0)), "warnings": int(entry.get("warnings", 0)),
        "contents": [
            {"type": c.get("type"), "submitted": int(c.get("submitted", 0))}
            for c in entry.get("contents", [])   # absent while a sitemap is pending
        ],
    }


def sitemap_rows(sc, site_url):
    """Submitted sitemaps, with the children of each index file.

    Every storefront submits one BigCommerce index at /xmlsitemap.php, and a
    plain sitemaps.list returns only that one entry with no URL counts. The
    counts live in the children, which need a second call with sitemapIndex.
    """
    rows = []
    for top in sc.sitemaps(site_url):
        rows.append(_sitemap(top))
        if top.get("isSitemapsIndex") and top.get("path"):
            try:
                rows.extend(_sitemap(child, parent=top["path"]) for child in sc.sitemaps(site_url, sitemap_index=top["path"]))
            except SearchConsoleError:
                pass   # the index itself is still worth showing
    return rows


def collect_property(sc, prop, start, end, known_sites, verbose=True):
    """Everything for one property, or a record saying why we got nothing."""
    entry = {
        "key": prop.key, "market": prop.market, "origin": prop.origin,
        "site_url": prop.site_url, "available": False,
    }

    # The property may be verified as a URL-prefix property or as a Domain one.
    site_url, permission = None, None
    for candidate in (prop.site_url, prop.domain_property):
        match = known_sites.get(candidate)
        if match:
            site_url, permission = candidate, match
            break
    if not site_url:
        entry["reason"] = "not visible to this credential"
        if verbose:
            print(f"  {prop.key}: no access")
        return entry

    entry.update(available=True, site_url=site_url, permission=permission)
    if verbose:
        print(f"  {prop.key}: {site_url} ({permission})")

    def q(**kw):
        return sc.query(site_url, start, end, **kw)

    try:
        entry["totals"] = _totals(q())
        entry["fresh_totals"] = _totals(q(data_state="all"))
        # dataState=all so the last few days show up at all; the API says from
        # which day they are still provisional, and the chart marks it.
        day_rows, day_meta = q(dimensions=["date"], data_state="all",
                               max_rows=MAX_HISTORY_DAYS, with_metadata=True)
        entry["daily"] = [
            {"d": r["k"], "clicks": r["clicks"], "impr": r["impr"], "ctr": r["ctr"], "pos": r["pos"]}
            for r in _rows_to_list(day_rows)
        ]
        entry["provisional_from"] = (day_meta or {}).get("first_incomplete_date")

        # Every dimension over two windows. The tables have to say which window
        # they cover, and "the last 16 months" answers a different question from
        # "the last four weeks", so collect both rather than mislabel one.
        recent_start = max(start, (dt.date.fromisoformat(end) - dt.timedelta(days=RECENT_DAYS)).isoformat())
        windows = {"full": (start, end, None), "recent": (recent_start, end, RECENT_CAP)}
        dims = {}
        for window, (w_start, w_end, cap) in windows.items():
            rows = {}
            for dim, dim_cap in TOP_ROWS.items():
                try:
                    rows[dim] = _rows_to_list(sc.query(
                        site_url, w_start, w_end, dimensions=[dim],
                        max_rows=min(dim_cap, cap) if cap else dim_cap,
                    ))
                except SearchConsoleError as exc:
                    rows[dim] = []
                    entry.setdefault("partial", []).append(f"{window}/{dim}: {exc}")
            dims[window] = rows
        entry["dimensions"] = dims
        entry["windows"] = {"full": [start, end], "recent": [recent_start, end]}

        types = {}
        for t in (s["name"] for s in surface.SEARCH_TYPES):
            try:
                types[t] = _totals(q(search_type=t))
            except SearchConsoleError as exc:
                entry.setdefault("partial", []).append(f"type {t}: {exc}")
        entry["types"] = types

        try:
            entry["sitemaps"] = sitemap_rows(sc, site_url)
        except SearchConsoleError as exc:
            entry["sitemaps"] = []
            entry.setdefault("partial", []).append(f"sitemaps: {exc}")

    except AccessDenied as exc:
        entry.update(available=False, reason=str(exc))
    except SearchConsoleError as exc:
        entry.setdefault("partial", []).append(str(exc))
    return entry


def collect(properties=None, days=MAX_HISTORY_DAYS, verbose=True, access_token=None):
    """The whole payload the dashboard's Search tab renders."""
    props = properties or list(PROPERTIES)
    today = dt.date.today()
    end = today.isoformat()
    start = (today - dt.timedelta(days=min(days, MAX_HISTORY_DAYS))).isoformat()

    sc = SearchConsole.connect(access_token=access_token)
    if verbose:
        print(f"search console: {sc.how}")

    entries = sc.sites()
    known = {e.get("siteUrl"): e.get("permissionLevel") for e in entries}
    if verbose:
        print(f"credential sees {len(known)} properties")

    return {
        "generated": end,
        "start": start,
        "auth": sc.how,
        "visible_properties": len(known),
        "properties": [collect_property(sc, p, start, end, known, verbose) for p in props],
        "surface": {
            "dimensions": surface.DIMENSIONS,
            "metrics": surface.METRICS,
            "search_types": surface.SEARCH_TYPES,
            "data_states": surface.DATA_STATES,
            "aggregation_types": surface.AGGREGATION_TYPES,
            "filter_operators": surface.FILTER_OPERATORS,
            "resources": surface.RESOURCES,
            "inspection_fields": surface.INSPECTION_FIELDS,
            "limits": surface.LIMITS,
        },
    }


def unavailable(reason):
    """The same shape, for when no credential could be resolved at all."""
    return {
        "generated": dt.date.today().isoformat(), "auth": None, "error": reason,
        "visible_properties": 0,
        "properties": [
            {"key": p.key, "market": p.market, "origin": p.origin, "site_url": p.site_url,
             "available": False, "reason": reason}
            for p in PROPERTIES
        ],
        "surface": {
            "dimensions": surface.DIMENSIONS, "metrics": surface.METRICS,
            "search_types": surface.SEARCH_TYPES, "data_states": surface.DATA_STATES,
            "aggregation_types": surface.AGGREGATION_TYPES, "filter_operators": surface.FILTER_OPERATORS,
            "resources": surface.RESOURCES, "inspection_fields": surface.INSPECTION_FIELDS,
            "limits": surface.LIMITS,
        },
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--property", nargs="+", help="Market keys to pull; default all six")
    ap.add_argument("--days", type=int, default=MAX_HISTORY_DAYS, help=f"History window (max {MAX_HISTORY_DAYS})")
    ap.add_argument("--out", help="Write the payload here as JSON")
    ap.add_argument("--print", action="store_true", dest="show", help="Print a summary")
    args = ap.parse_args(argv)

    try:
        data = collect(select(args.property), days=args.days)
    except AccessDenied as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        print(f"wrote {args.out}")
    if args.show or not args.out:
        for p in data["properties"]:
            if not p["available"]:
                print(f"  {p['key']:<4} —  {p.get('reason', 'no data')}")
                continue
            t = p["totals"]
            dims = {k: len(v) for k, v in p["dimensions"]["full"].items()}
            print(f"  {p['key']:<4} {t['clicks']:>7,} clicks  {t['impr']:>9,} impr  "
                  f"pos {t['pos']}  rows {dims}  sitemaps {len(p['sitemaps'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
