"""
BigCommerce reads for the group dashboard: catalogue and sales, per store.

One store is one market and one currency. Nothing here ever adds money across
stores; each figure carries the store's own currency and the page keeps them in
separate columns.

Not every token has the same scopes. The .nl, .fr and .de tokens are catalogue
only and answer 403 on /v2/store and /v2/orders, so each section returns
whatever it could read plus the reason the rest is missing. A partly readable
store is still worth showing.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from lib.catalogue import is_internal

TIMEOUT = 60
PAGE = 250

# Orders that are not revenue, per CLAUDE.md: Incomplete, Cancelled, Declined.
NON_REVENUE = {0, 5, 6}

# payment_method is free text in places. Card revenue arrives under three
# different labels, and E-Transfer under several casings.
CARD_LABELS = re.compile(r"credit card|authorize\.net|google pay|apple pay|visa|mastercard|amex", re.I)
FREE_TEXT = re.compile(r"\d{4,}|service request|contract", re.I)


class StoreError(RuntimeError):
    pass


class Client:
    def __init__(self, store, token):
        self.store = store
        self.token = token

    def get(self, path, **params):
        url = self.store.api_base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"X-Auth-Token": self.token, "Accept": "application/json"})
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    body = resp.read()
                    return json.loads(body) if body.strip() else None
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    wait = int(exc.headers.get("X-Rate-Limit-Time-Reset-Ms", "5000")) / 1000
                    time.sleep(min(wait, 60) + 0.5)
                    continue
                if exc.code >= 500 and attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                raise StoreError(f"HTTP {exc.code} on {path}") from None
            except (urllib.error.URLError, TimeoutError):
                if attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                raise StoreError(f"{path} unreachable") from None
        raise StoreError(f"{path} kept failing")

    def pages(self, path, **params):
        page = 1
        while True:
            data = self.get(path, limit=PAGE, page=page, **params)
            rows = data["data"] if isinstance(data, dict) else (data or [])
            yield from rows
            if isinstance(data, dict):
                pagination = data["meta"]["pagination"]
                if page >= pagination["total_pages"]:
                    return
            elif len(rows) < PAGE:
                return
            page += 1


# --------------------------------------------------------------------------- catalogue


def price_band(price):
    price = float(price or 0)
    for limit, label in ((25, "Under $25"), (100, "$25–99"), (300, "$100–299"),
                         (600, "$300–599"), (1000, "$600–999")):
        if price < limit:
            return label
    return "$1,000+"


def catalogue(client):
    """Counts, visibility, stock, brands, price bands and the best sellers."""
    brands = {}
    try:
        brands = {b["id"]: b["name"].strip() for b in client.pages("/v3/catalog/brands", include_fields="name")}
    except StoreError:
        pass
    roots, cats = {}, {}
    try:
        cats = {c["id"]: c for c in client.pages("/v3/catalog/categories", include_fields="name,parent_id")}
        for cid, cat in cats.items():
            cur, seen = cat, set()
            while cur and cur.get("parent_id") and cur["id"] not in seen:
                seen.add(cur["id"])
                cur = cats.get(cur["parent_id"])
            roots[cid] = (cur or cat)["name"].strip()
    except StoreError:
        pass

    fields = "name,sku,price,sale_price,categories,brand_id,inventory_level,inventory_tracking,is_visible,total_sold,view_count,date_created"
    products = list(client.pages("/v3/catalog/products", include_fields=fields))

    # The storefront doubles as an internal ordering system, and its office
    # stock outsells the hairpieces. Count it, then leave it out of anything
    # that ranks the catalogue.
    for p in products:
        p["_internal"] = is_internal(p.get("name"), p.get("sku"),
                                     [roots.get(c, "") for c in p.get("categories", [])])
    visible = [p for p in products if p.get("is_visible")]
    merch = [p for p in visible if not p["_internal"]]
    out_of_stock = [p for p in visible
                    if p.get("inventory_tracking") != "none" and not (p.get("inventory_level") or 0)]
    by_brand = Counter(brands.get(p.get("brand_id"), "(no brand)") for p in visible)
    by_root = Counter(r for p in visible for r in {roots.get(c) for c in p.get("categories", [])} if r)
    by_band = Counter(price_band(p.get("price")) for p in visible)

    try:
        variants = client.get("/v3/catalog/variants", limit=1)["meta"]["pagination"]["total"]
    except (StoreError, KeyError, TypeError):
        variants = None

    best = sorted(merch, key=lambda p: -(p.get("total_sold") or 0))[:15]
    return {
        "products": len(products),
        "visible": len(visible),
        "hidden": len(products) - len(visible),
        "internal": sum(1 for p in products if p["_internal"]),
        "variants": variants,
        "out_of_stock": len(out_of_stock),
        "on_sale": sum(1 for p in visible if p.get("sale_price")),
        "brands": [{"name": n, "products": c} for n, c in by_brand.most_common(12)],
        "categories": [{"name": n, "products": c} for n, c in by_root.most_common(12)],
        "price_bands": [{"band": b, "products": by_band[b]} for b in
                        ("Under $25", "$25–99", "$100–299", "$300–599", "$600–999", "$1,000+") if by_band[b]],
        "best_sellers": [{"name": (p.get("name") or "")[:70], "sku": p.get("sku"),
                          "price": round(float(p.get("price") or 0), 2),
                          "sold": p.get("total_sold") or 0, "views": p.get("view_count") or 0}
                         for p in best],
        "newest": max((p.get("date_created") or "" for p in products), default=None),
    }


# --------------------------------------------------------------------------- sales


def normalise_payment(raw):
    """Collapse the free-text and case-variant mess into something countable."""
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return "(not recorded)"
    if CARD_LABELS.search(text):
        return "Card"
    if FREE_TEXT.search(text):
        return "Other (free text)"
    return text.title()


def _order_month(order):
    # date_created is RFC 2822: "Tue, 21 Oct 2025 00:19:07 +0000"
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(order["date_created"]).strftime("%Y-%m")
    except Exception:  # noqa: BLE001 - a malformed date must not lose the order
        return "unknown"


def sales(client, since_iso, workers=4):
    """Twelve months of orders, aggregated. Paged, because there is no totals API.

    /v2/orders/count gives the status breakdown in one call - that part is free.
    Revenue has to be summed from the orders themselves; at 250 an page this is
    a couple of hundred requests for the whole group, well inside the rate
    limit (the API reports millions of requests left in a 30-second window).
    """
    counts = client.get("/v2/orders/count", min_date_created=since_iso)
    lifetime = client.get("/v2/orders/count")
    total_pages = max(1, -(-counts["count"] // PAGE))

    monthly = defaultdict(lambda: {"orders": 0, "revenue": 0.0})
    payments = defaultdict(lambda: {"orders": 0, "revenue": 0.0})
    revenue, refunded, revenue_orders, excluded, refund_orders = 0.0, 0.0, 0, 0, 0

    def fetch(page):
        return client.get("/v2/orders", min_date_created=since_iso, limit=PAGE, page=page,
                          sort="date_created:asc") or []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for orders in pool.map(fetch, range(1, total_pages + 1)):
            for o in orders:
                if o.get("status_id") in NON_REVENUE:
                    excluded += 1
                    continue
                # Money kept, not money taken: a Refunded or Partially Refunded
                # order still carries its original total, so net it off here
                # rather than reporting a refund as revenue.
                gross = float(o.get("total_inc_tax") or 0)
                back = float(o.get("refunded_amount") or 0)
                net = gross - back
                if back:
                    refunded += back
                    refund_orders += 1
                month = _order_month(o)
                monthly[month]["orders"] += 1
                monthly[month]["revenue"] += net
                pay = payments[normalise_payment(o.get("payment_method"))]
                pay["orders"] += 1
                pay["revenue"] += net
                revenue += net
                revenue_orders += 1

    return {
        "currency": client.store.currency,
        "since": since_iso[:10],
        "orders_all_statuses": counts["count"],
        "orders_counted": revenue_orders,
        "orders_excluded": excluded,
        "revenue": round(revenue, 2),
        "refunded": round(refunded, 2),
        "refund_orders": refund_orders,
        "aov": round(revenue / revenue_orders, 2) if revenue_orders else None,
        "lifetime_orders": lifetime["count"],
        "statuses": [{"id": s["id"], "name": s["name"], "orders": s["count"]}
                     for s in counts["statuses"] if s["count"]],
        "monthly": [{"m": m, **{k: round(v, 2) if isinstance(v, float) else v for k, v in vals.items()}}
                    for m, vals in sorted(monthly.items())],
        "payments": sorted(
            ({"method": m, "orders": v["orders"], "revenue": round(v["revenue"], 2)}
             for m, v in payments.items()),
            key=lambda p: -p["revenue"])[:12],
    }
