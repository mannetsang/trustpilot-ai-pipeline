"""Build dashboard_data.json for the Google Shopping dashboard.

Pulls twelve months of Merchant API performance, every offer's status and
issues, Google's categorisation, and the BigCommerce catalogue (sales, views,
stock, cost), then joins them per product.

    python google-merchant/dashboard/build_data.py --account 5298296396 --label CA --out google-merchant/dashboard/dashboard_data.json

Auth is Application Default Credentials for Google and Secret Manager for the
BigCommerce token, exactly like sync_bigcommerce.py.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter, defaultdict

from google.protobuf.json_format import MessageToDict
from google.shopping.merchant_products_v1 import ListProductsRequest, ProductsServiceClient
from google.shopping.merchant_reports_v1 import ReportServiceClient, SearchRequest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from lib.secrets import get_secret  # noqa: E402
from merchant_api import STOREFRONTS, account_parent, add_account_argument, display_name_for, resolve_account  # noqa: E402
from sync_bigcommerce import STORES, BigCommerce, load_categories, offer_id_for  # noqa: E402

ISSUE_NAMES = {
    "attribute_pending_review": "Image under review (temporary)", "image_unwanted_overlays": "Promotional overlay on image",
    "missing_shipping_weight": "Missing shipping weight", "personal_hardships_policy_violation": "Personal hardships policy",
    "violated_discovery_ads_policy_experiment2": "YouTube Shopping image policy", "attribute_violated_discovery_ads_policy": "Image policy (Discovery)",
    "healthcare_pet_pharma_policy_violation": "Pet pharmaceuticals policy", "price_mismatch": "Price differs from page",
    "healthcare_pdt_policy_violation": "Prescription drugs policy", "image_link_pending_crawl": "Image not yet crawled (temporary)",
    "image_link_internal_error": "Image processing error", "tobacco_policy_violation": "Tobacco policy",
    "availability_updated": "Availability auto-corrected by Google", "price_updated": "Price auto-corrected by Google",
    "image_too_small_for_high_resolution": "Image under 500px", "description_short": "Description too short",
    "title_all_caps": "Title in capitals", "image_link_internal_error_fallback": "Image processing delayed",
    "utf8_encoding_error": "Encoding error in description", "low_image_quality": "Low image quality", "image_too_small": "Additional image too small",
}
INTERNAL = re.compile(r"extra charge|coffee|sugar|battery|catalog|price list|handling fee|garbage|envelope|thermal paper|pens\b|^ou_|service", re.I)


def money(p):
    return int(p.get("amount_micros", 0)) / 1e6 if p else 0.0


def search(client, parent, query):
    rows = []
    for r in client.search(request=SearchRequest(parent=parent, query=query, page_size=1000)):
        d = MessageToDict(r._pb, preserving_proto_field_name=True)
        rows.append(next(iter(d.values())))
    return rows


def nm(x):
    return x.name if hasattr(x, "name") else str(x)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_account_argument(ap)
    ap.add_argument("--label", default="CA")
    ap.add_argument("--out", default=os.path.join(HERE, "dashboard_data.json"))
    ap.add_argument("--days", type=int, default=380, help="History window for performance data")
    args = ap.parse_args(argv)
    account = resolve_account(args)
    parent = account_parent(account)
    label = args.label.upper()
    storefront = next(s for s in STOREFRONTS if s[1] == label)
    today = dt.date.today()
    start = (today - dt.timedelta(days=args.days)).isoformat()

    rep = ReportServiceClient()
    print("reports: product_view")
    pv = search(rep, parent, "SELECT id, offer_id, title, brand, price, availability, aggregated_reporting_context_status, category_l1, category_l2, category_l3, product_type_l1, gtin, item_group_id FROM product_view")
    print("reports: performance")
    perf_daily = search(rep, parent, f"SELECT date, marketing_method, clicks, impressions, conversions, conversion_value FROM product_performance_view WHERE date BETWEEN '{start}' AND '{today}'")
    perf_offer = search(rep, parent, f"SELECT offer_id, title, clicks, impressions, conversions, conversion_value FROM product_performance_view WHERE date BETWEEN '{(today - dt.timedelta(days=365)).isoformat()}' AND '{today}'")
    perf_cat = search(rep, parent, f"SELECT category_l1, category_l2, clicks, impressions, conversions, conversion_value FROM product_performance_view WHERE date BETWEEN '{(today - dt.timedelta(days=365)).isoformat()}' AND '{today}'")
    nonp = search(rep, parent, f"SELECT date, clicks, impressions FROM non_product_performance_view WHERE date BETWEEN '{start}' AND '{today}'")

    print("products: statuses")
    ds_name = display_name_for(storefront[0])
    from google.shopping.merchant_datasources_v1 import DataSourcesServiceClient, ListDataSourcesRequest
    ds_id = next((d.name for d in DataSourcesServiceClient().list_data_sources(request=ListDataSourcesRequest(parent=parent)) if d.display_name == ds_name), None)
    statuses = []
    for p in ProductsServiceClient().list_products(request=ListProductsRequest(parent=parent, page_size=1000)):
        if ds_id and p.data_source != ds_id:
            continue
        a, st = p.product_attributes, p.product_status
        dest = {}
        for d in st.destination_statuses:
            dest[nm(d.reporting_context)] = "disapproved" if d.disapproved_countries else "pending" if d.pending_countries else "approved" if d.approved_countries else "none"
        statuses.append({"offer_id": p.offer_id, "brand": a.brand, "price": a.price.amount_micros / 1e6, "availability": nm(a.availability), "gtin": list(a.gtins),
                         "weight": a.shipping_weight.value if "shipping_weight" in a else None, "image": a.image_link, "dest": dest,
                         "issues": [(i.code, nm(i.severity)) for i in st.item_level_issues]})

    print("bigcommerce: catalogue")
    store = STORES[label]
    bc = BigCommerce(store["store_hash"], get_secret(f"BIGCOMMERCE_{store['store_hash']}_ACCESS_TOKEN", env_var="BC_ACCESS_TOKEN"))
    paths, cats = load_categories(bc)
    prods = list(bc.pages("/v3/catalog/products", include="variants", include_fields="name,sku,price,cost_price,sale_price,categories,brand_id,inventory_level,inventory_tracking,is_visible,total_sold,view_count,reviews_count,custom_url,type"))
    store_info = bc.get("/v2/store")
    base_url = store_info["secure_url"].rstrip("/")

    def root(cid):
        cur = cats.get(cid)
        seen = set()
        while cur and cur["parent_id"] and cur["id"] not in seen:
            seen.add(cur["id"]); cur = cats.get(cur["parent_id"])
        return cur["name"] if cur else None

    offer2prod = {}
    for p in prods:
        for v in p.get("variants") or []:
            offer2prod[offer_id_for(v)] = p
    gview = {r["offer_id"]: r for r in pv}
    clicks = defaultdict(lambda: {"clicks": 0, "impr": 0, "conv": 0.0})
    for r in perf_offer:
        k = (r.get("title") or "")[:40].lower()
        clicks[k]["clicks"] += int(r.get("clicks", 0)); clicks[k]["impr"] += int(r.get("impressions", 0)); clicks[k]["conv"] += float(r.get("conversions", 0))

    by_prod = defaultdict(list)
    for s in statuses:
        p = offer2prod.get(s["offer_id"])
        if p:
            by_prod[p["id"]].append(s)
    rows = []
    for pid, offers in by_prod.items():
        p = offer2prod[offers[0]["offer_id"]]
        gcat = Counter((gview.get(o["offer_id"], {}).get("category_l3") or gview.get(o["offer_id"], {}).get("category_l2") or gview.get(o["offer_id"], {}).get("category_l1") or "(no category)") for o in offers).most_common(1)[0][0]
        dis, warn = set(), set()
        for o in offers:
            for code, sev in o["issues"]:
                (dis if sev == "DISAPPROVED" else warn).add(code)
        roots = sorted({root(c) for c in p["categories"] if c in cats and root(c)}) or ["(uncategorised)"]
        leaf = next((paths[c] for c in p["categories"] if c in cats and cats[c]["parent_id"]), paths.get(p["categories"][0], "") if p["categories"] else "")
        ck = clicks.get(p["name"][:40].lower(), {"clicks": 0, "impr": 0, "conv": 0})
        prices = [o["price"] for o in offers]
        rows.append({"id": pid, "name": p["name"][:80], "sku": p["sku"], "url": base_url + p["custom_url"]["url"], "root": "/".join(roots), "cat": leaf[:90], "gcat": gcat,
                     "price": round(min(prices), 2), "pmax": round(max(prices), 2), "offers": len(offers), "oos": sum(1 for o in offers if o["availability"] == "OUT_OF_STOCK"),
                     "approved": sum(1 for o in offers if o["dest"].get("SHOPPING_ADS") == "approved"), "disapproved": sum(1 for o in offers if o["dest"].get("SHOPPING_ADS") == "disapproved"),
                     "issues": sorted(dis), "warnings": sorted(warn), "sold": p["total_sold"], "views": p["view_count"], "gclicks": ck["clicks"], "gimpr": ck["impr"], "gconv": round(ck["conv"], 1),
                     "margin": (round((p["price"] - p["cost_price"]) / p["price"] * 100) if p.get("cost_price") and p["price"] else None), "brand": offers[0]["brand"],
                     "gtin": any(o["gtin"] for o in offers), "weight": all(o["weight"] for o in offers),
                     "internal": bool(INTERNAL.search(p["name"])) or min(prices) < 3 or "Office Supplies" in leaf, "img": offers[0]["image"]})

    daily = defaultdict(lambda: {"organic": 0, "ads": 0, "impr": 0, "conv": 0.0, "val": 0.0, "np": 0, "npi": 0})
    for r in perf_daily:
        d = f"{r['date']['year']}-{r['date']['month']:02d}-{r['date']['day']:02d}"
        daily[d]["organic" if r.get("marketing_method") == "ORGANIC" else "ads"] += int(r.get("clicks", 0)); daily[d]["impr"] += int(r.get("impressions", 0))
        daily[d]["conv"] += float(r.get("conversions", 0)); daily[d]["val"] += money(r.get("conversion_value"))
    for r in nonp:
        d = f"{r['date']['year']}-{r['date']['month']:02d}-{r['date']['day']:02d}"
        daily[d]["np"] += int(r.get("clicks", 0)); daily[d]["npi"] += int(r.get("impressions", 0))
    series = [{"d": d, **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in daily[d].items()}} for d in sorted(daily)]

    gcats = Counter((r.get("category_l3") or r.get("category_l2") or r.get("category_l1") or "(no category)") for r in pv)
    cat_perf = [{"cat": (r.get("category_l1") or "(unclassified)") + (" › " + r["category_l2"] if r.get("category_l2") else ""), "clicks": int(r.get("clicks", 0)), "impr": int(r.get("impressions", 0)), "conv": round(float(r.get("conversions", 0)), 1), "val": round(money(r.get("conversion_value")))} for r in sorted(perf_cat, key=lambda x: -int(x.get("clicks", 0)))[:8]]
    agg = defaultdict(lambda: {"clicks": 0, "impr": 0, "conv": 0.0, "val": 0.0, "title": ""})
    for r in perf_offer:
        a = agg[r["offer_id"]]; a["title"] = r.get("title", ""); a["clicks"] += int(r.get("clicks", 0)); a["impr"] += int(r.get("impressions", 0)); a["conv"] += float(r.get("conversions", 0)); a["val"] += money(r.get("conversion_value"))
    top_clicked = [dict(title=a["title"][:70], clicks=a["clicks"], impr=a["impr"], conv=round(a["conv"], 1), val=round(a["val"])) for a in sorted(agg.values(), key=lambda a: -a["clicks"])[:12]]
    third = [s for s in statuses if s["brand"].lower() != store["brand_default"].lower()]
    out = {"generated": today.isoformat(), "products": rows, "daily": series, "issue_names": ISSUE_NAMES, "category_perf": cat_perf, "top_clicked": top_clicked,
           "google_categories": [{"category": k, "offers": v} for k, v in gcats.most_common(12)],
           "gtin": {"third_party_offers": len(third), "third_party_with_gtin": sum(1 for s in third if s["gtin"]), "all_with_gtin": sum(1 for s in statuses if s["gtin"])},
           "reviews": {"products_with_reviews": sum(1 for p in prods if p["is_visible"] and p.get("reviews_count")), "reviews": sum(p.get("reviews_count", 0) for p in prods if p["is_visible"])},
           "variants": {"products": len(by_prod), "offers": len(statuses), "products_over_50": sum(1 for v in by_prod.values() if len(v) > 50), "offers_in_over_50": sum(len(v) for v in by_prod.values() if len(v) > 50)},
           "issue_counts": [{"code": c, "offers": n} for c, n in Counter(code for s in statuses for code, sev in s["issues"] if sev == "DISAPPROVED").most_common()],
           "warning_counts": [{"code": c, "offers": n} for c, n in Counter(code for s in statuses for code, sev in s["issues"] if sev != "DISAPPROVED").most_common()]}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {args.out}: {len(rows)} products, {len(series)} days, {len(statuses)} offers")


if __name__ == "__main__":
    main()
