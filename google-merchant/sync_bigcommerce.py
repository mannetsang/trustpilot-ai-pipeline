"""Sync the BigCommerce catalogue into a Merchant Center API data source.

    python google-merchant/sync_bigcommerce.py --label CA --dry-run
    python google-merchant/sync_bigcommerce.py --label CA --limit 25
    python google-merchant/sync_bigcommerce.py --label CA

One offer per BigCommerce variant. Every visible, purchasable variant with a
price and an image becomes a ProductInput in the storefront's "<storefront>
API feed" data source (create it first with data_sources.py). Variants of the
same product share an item_group_id, so Merchant Center shows them as one
product with options.

A full run (no --limit / --sku) also deletes offers that are in the data
source but no longer in the catalogue, so hidden or deleted products drop
out of Google within a day. --no-delete turns that off.

Credentials: the BigCommerce token comes from Secret Manager through
lib/secrets.py (BIGCOMMERCE_<store_hash>_ACCESS_TOKEN, or BC_ACCESS_TOKEN
locally); Merchant API auth is Application Default Credentials.

Known data-quality handling, from inspecting the store:
- upc/mpn hold Excel-mangled values such as "6.14043E+11" and gtin sometimes
  holds an import-shifted column ("Default Tax Class"). GTINs are kept only
  when they are 8/12/13/14 digits with a valid check digit; MPNs only when
  they look like part numbers. Otherwise identifier_exists=false is sent.
- Offer ids are the variant SKU when it is URL-safe, else bc-v<variant id>,
  because SKUs like TP BLUE BAG "ST" cannot sit in a resource path.
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser

from google.api_core import exceptions as gexc
from google.shopping import type as gtype
from google.shopping.merchant_datasources_v1 import DataSourcesServiceClient, ListDataSourcesRequest
from google.shopping.merchant_products_v1 import (
    Availability,
    Condition,
    DeleteProductInputRequest,
    InsertProductInputRequest,
    ListProductsRequest,
    ProductAttributes,
    ProductInput,
    ProductInputsServiceClient,
    ProductsServiceClient,
    Shipping,
    ShippingWeight,
)

from merchant_api import (
    STOREFRONTS,
    account_parent,
    add_account_argument,
    display_name_for,
    explain_api_error,
    resolve_account,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.secrets import get_secret  # noqa: E402

# BigCommerce store behind each feed label. Only the .ca store is known so far;
# add the others here once their store hashes and tokens exist.
STORES = {
    "CA": {"store_hash": "gmosz3ja", "brand_default": "Superhairpieces"},
}

BC_API = "https://api.bigcommerce.com/stores/{hash}"

# Google product taxonomy ids (taxonomy-with-ids.en-US.txt). Rules are tried in
# order against the product's BigCommerce category paths and name; the first
# match wins. Without this Google guesses, and it filed toupees under Hats.
GOOGLE_CATEGORIES = {
    "hair_extensions": "4057",   # Apparel & Accessories > Clothing Accessories > Hair Accessories > Hair Extensions
    "wig_glue_tape": "7306",     # ... > Hair Accessories > Wig Accessories > Wig Glue & Tape
    "wig_accessories": "7305",   # ... > Hair Accessories > Wig Accessories
    "wigs": "181",               # ... > Hair Accessories > Wigs
    "hair_care": "486",          # Health & Beauty > Personal Care > Hair Care
    "hair_loss": "4766",         # ... > Hair Care > Hair Loss Treatments
    "mannequins": "3803",        # Business & Industrial > Retail > Display Mannequins
    "hair_care_kits": "8452",    # Health & Beauty > Personal Care > Hair Care > Hair Care Kits
    "cosmetic_tools": "2619",    # Health & Beauty > Personal Care > Cosmetics > Cosmetic Tools
}
CATEGORY_RULES = [
    # (key, match regex, veto regex) tried in order on "category paths | name", lowercase.
    # A rule only fires when the match hits and the veto (if any) does not.
    ("mannequins", r"mannequin|styling block|wig head|wig holder|wig stand", None),
    ("cosmetic_tools", r"microblad|microshad|lip blush|eyelash|makeup|academy|training kit", None),
    ("hair_care_kits", r"gift set|\bkit\b", r"extension|toupee|hairpiece|starter"),
    ("wig_accessories", r"\bclips?\b|\bcombs?\b|wig cap|hook needle|colou?r ring|needle|hair ?net", r"topper|toupee|\bextensions?\b|hair system|\bwigs?\b"),
    ("hair_extensions", r"\bextensions?\b|\bweft\b|tape[- ]in\b|nail[- ]tip|i-?tip|micro ?link|clip[- ]in\b|\bhalo\b|pony ?tail", r"topper|toupee|\bwigs?\b|\btabs?\b|remover|\brolls?\b|pliers|\btools?\b|beads"),
    ("wig_glue_tape", r"\btapes?\b|\bglue\b|adhesive|solvent|remover|scalp protector|\bliner\b|\bbond\b|knot sealer|c-22|no shine|attachment & removal", None),
    ("wigs", r"toupee|topper|hair system|\bwigs?\b|sheitel|hair unit|hairpieces?\b|hair replacement", r"\bclips?\b|\bcombs?\b|\bstand\b|catalog"),
    ("wig_accessories", r"scissor|brush|applicator|glove|template|swatch|spray bottle|\btools?\b", None),
    ("hair_loss", r"thinning|hair loss|regrowth|minoxidil", None),
    ("hair_care", r"shampoo|conditioner|mousse|\bgel\b|serum|hair care|styling|leave-in|spray|cream|\boil\b|detangl|\bperm\b|color|colour|dye", None),
    ("wig_accessories", r"supplies", None),
    ("wigs", r"\bmen\b|\bwomen\b|frontal|closure|\bbase\b|\blace\b|\bdensity\b", None),
]
# Products that exist in BigCommerce for internal ordering and must never reach Google.
INTERNAL_CATEGORY = re.compile(r"office supplies|services?$", re.I)
INTERNAL_NAME = re.compile(
    r"extra charge|coffee|sugar|garbage|envelope|thermal paper|batter(y|ies)|catalog|price list|handling fee|"
    r"^ou_|\\bpens\\b|toilet paper|paper towel|\\bservices?\\b|base cut|deposit|gift card|consultation|certificad|certification|nanatest|\\btest\\d*\\b",
    re.I,
)
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,50}$")
WEIGHT_UNITS = {"LBS": "lb", "KGS": "kg", "Ounces": "oz", "Grams": "g", "Pounds": "lb", "Kilograms": "kg"}
TITLE_MAX = 150
DESCRIPTION_MAX = 5000
MAX_ADDITIONAL_IMAGES = 10


# --------------------------------------------------------------------------- BigCommerce


class BigCommerce:
    def __init__(self, store_hash, token):
        self.base = BC_API.format(hash=store_hash)
        self.token = token

    def get(self, path, **params):
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"X-Auth-Token": self.token, "Accept": "application/json"})
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    wait = int(exc.headers.get("X-Rate-Limit-Time-Reset-Ms", "5000")) / 1000
                    time.sleep(min(wait, 60) + 0.5)
                    continue
                if exc.code >= 500 and attempt < 5:
                    time.sleep(2**attempt)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt < 5:
                    time.sleep(2**attempt)
                    continue
                raise
        raise RuntimeError(f"BigCommerce request kept failing: {path}")

    def pages(self, path, **params):
        page = 1
        while True:
            data = self.get(path, limit=250, page=page, **params)
            yield from data["data"]
            if page >= data["meta"]["pagination"]["total_pages"]:
                return
            page += 1


def load_store(bc):
    store = bc.get("/v2/store")
    return {
        "url": store["secure_url"].rstrip("/"),
        "currency": store["currency"],
        "weight_unit": WEIGHT_UNITS.get(store["weight_units"], "lb"),
    }


def load_brands(bc):
    return {b["id"]: b["name"].strip() for b in bc.pages("/v3/catalog/brands", include_fields="name")}


def load_categories(bc):
    cats = {c["id"]: c for c in bc.pages("/v3/catalog/categories", include_fields="name,parent_id,is_visible")}
    paths = {}
    for cid, cat in cats.items():
        names, cur, seen = [], cat, set()
        while cur and cur["id"] not in seen:
            seen.add(cur["id"])
            names.append(cur["name"].strip())
            cur = cats.get(cur["parent_id"]) if cur["parent_id"] else None
        paths[cid] = " > ".join(reversed(names))
    return paths, cats


# --------------------------------------------------------------------------- mapping


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "br", "li", "div", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append(" ")


def clean_text(raw, limit):
    if not raw:
        return ""
    parser = _TextExtractor()
    parser.feed(raw)
    text = html.unescape(" ".join(parser.parts))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


def valid_gtin(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if digits != str(value or "").strip() or len(digits) not in (8, 12, 13, 14):
        return None
    total = 0
    for i, ch in enumerate(reversed(digits[:-1])):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    if (10 - total % 10) % 10 != int(digits[-1]):
        return None
    if set(digits) == {"0"}:
        return None
    return digits


def plausible_mpn(value):
    value = (value or "").strip()
    if not value or len(value) > 70 or "E+" in value.upper() or " " in value and len(value) > 30:
        return None
    if value.lower() in ("default tax class", "n/a", "none", "na"):
        return None
    return value


def offer_id_for(variant):
    sku = (variant.get("sku") or "").strip()
    if sku and SAFE_ID.match(sku):
        return sku
    return f"bc-v{variant['id']}"


def group_id_for(product):
    sku = (product.get("sku") or "").strip()
    if sku and SAFE_ID.match(sku):
        return sku
    return f"bc-p{product['id']}"


def google_category(product, category_paths):
    """Pick a Google product category id from the product name, then its BigCommerce categories.

    The name is tried first because category paths are noisy (a glue sits under
    "Hairpiece Tape", a topper under "Women > Wigs"); vetoes are checked against
    the name only.
    """
    name = (product["name"] or "").lower()
    paths = " | ".join(category_paths[c] for c in product.get("categories", []) if c in category_paths).lower()
    for haystack in (name, paths):
        for key, pattern, veto in CATEGORY_RULES:
            if re.search(pattern, haystack) and not (veto and re.search(veto, name)):
                return GOOGLE_CATEGORIES[key]
    return None


def is_internal(product, category_paths):
    """True for office stock, surcharges, services and other non-merchandise."""
    if any(INTERNAL_CATEGORY.search(category_paths[c]) for c in product.get("categories", []) if c in category_paths):
        return True
    if INTERNAL_NAME.search(product["name"] or ""):
        return True
    return not product.get("price") or float(product["price"]) <= 0


def money(amount, currency):
    return gtype.Price(amount_micros=int(round(float(amount) * 1_000_000)), currency_code=currency)


def availability_for(product, variant):
    if product["availability"] == "preorder":
        return Availability.PREORDER
    tracking = product["inventory_tracking"]
    if tracking == "none":
        return Availability.IN_STOCK
    level = variant["inventory_level"] if tracking == "variant" else product["inventory_level"]
    return Availability.IN_STOCK if (level or 0) > 0 else Availability.OUT_OF_STOCK


def build_offers(product, ctx, brands, category_paths):
    """Return ([(offer_id, ProductInput)], [(offer_id, reason)]) for one product."""
    offers, skipped = [], []
    variants = product.get("variants") or []
    if not variants:
        return offers, [(f"bc-p{product['id']}", "no variants")]

    if product["type"] != "physical":
        return offers, [(offer_id_for(v), f"type {product['type']}") for v in variants]
    if is_internal(product, ctx["category_paths"]):
        return offers, [(offer_id_for(v), "internal item") for v in variants]
    if product["availability"] == "disabled":
        return offers, [(offer_id_for(v), "availability disabled") for v in variants]
    if product.get("is_price_hidden"):
        return offers, [(offer_id_for(v), "price hidden") for v in variants]

    images = sorted(product.get("images") or [], key=lambda i: (not i.get("is_thumbnail"), i.get("sort_order", 0)))
    image_urls = [i["url_zoom"] for i in images if i.get("url_zoom")]
    description = clean_text(product.get("description"), DESCRIPTION_MAX) or product["name"].strip()
    brand = brands.get(product.get("brand_id")) or ctx["brand_default"]
    product_types = [category_paths[c] for c in product.get("categories", []) if c in category_paths][:10]
    gpc = google_category(product, category_paths)
    multi = len(variants) > 1
    group_id = group_id_for(product) if multi else None
    base_link = ctx["url"] + product["custom_url"]["url"]
    product_mpn = plausible_mpn(product.get("mpn"))
    product_gtin = valid_gtin(product.get("gtin")) or valid_gtin(product.get("upc"))

    for v in variants:
        oid = offer_id_for(v)
        if v.get("purchasing_disabled"):
            skipped.append((oid, "purchasing disabled"))
            continue
        price = v.get("price") if v.get("price") is not None else product["price"]
        if not price or float(price) <= 0:
            skipped.append((oid, "no price"))
            continue
        image = v.get("image_url") or (image_urls[0] if image_urls else None)
        if not image:
            skipped.append((oid, "no image"))
            continue

        title = product["name"].strip()
        if multi:
            labels = [o["label"].strip() for o in v.get("option_values", []) if o.get("label")]
            if labels:
                title = f"{title} - {', '.join(labels)}"
        title = title[:TITLE_MAX].rstrip()

        link = base_link
        if multi and v.get("sku"):
            link += ("&" if "?" in link else "?") + urllib.parse.urlencode({"sku": v["sku"]})

        attrs = ProductAttributes(
            title=title,
            description=description,
            link=link,
            image_link=image,
            additional_image_links=[u for u in image_urls if u != image][:MAX_ADDITIONAL_IMAGES],
            availability=availability_for(product, v),
            condition=Condition.NEW if product.get("condition", "New") == "New" else Condition.USED,
            price=money(price, ctx["currency"]),
            brand=brand,
            product_types=product_types,
        )
        if gpc:
            attrs.google_product_category = gpc

        sale = v.get("sale_price") if v.get("sale_price") is not None else product.get("sale_price")
        if sale and 0 < float(sale) < float(price):
            attrs.sale_price = money(sale, ctx["currency"])

        gtin = valid_gtin(v.get("gtin")) or valid_gtin(v.get("upc")) or product_gtin
        mpn = plausible_mpn(v.get("mpn")) or product_mpn
        if gtin:
            attrs.gtins = [gtin]
        if mpn and mpn != gtin:
            attrs.mpn = mpn
        if not gtin and not mpn:
            attrs.identifier_exists = False

        weight = v.get("calculated_weight") or v.get("weight") or product.get("weight")
        if weight and float(weight) > 0:
            attrs.shipping_weight = ShippingWeight(value=float(weight), unit=ctx["weight_unit"])

        if product.get("is_free_shipping") or v.get("is_free_shipping"):
            attrs.shipping = [Shipping(country=ctx["country"], price=money(0, ctx["currency"]))]

        if multi:
            attrs.item_group_id = group_id
        if product.get("availability") == "preorder" and product.get("preorder_release_date"):
            attrs.availability_date = product["preorder_release_date"]

        offers.append(
            (
                oid,
                ProductInput(
                    offer_id=oid,
                    content_language=ctx["language"],
                    feed_label=ctx["feed_label"],
                    product_attributes=attrs,
                ),
            )
        )
    return offers, skipped


# --------------------------------------------------------------------------- Merchant API


def find_data_source(parent, storefront):
    wanted = display_name_for(storefront)
    for ds in DataSourcesServiceClient().list_data_sources(request=ListDataSourcesRequest(parent=parent)):
        if ds.display_name == wanted:
            return ds.name
    sys.exit(f"error: data source {wanted!r} not found; run data_sources.py create first")


def insert_with_retry(client, request, attempts=6):
    for attempt in range(attempts):
        try:
            return client.insert_product_input(request=request)
        except (gexc.ResourceExhausted, gexc.ServiceUnavailable, gexc.DeadlineExceeded, gexc.InternalServerError):
            if attempt == attempts - 1:
                raise
            time.sleep(min(2**attempt, 30) + 0.1 * attempt)


def existing_offer_ids(parent, data_source):
    ids = set()
    for p in ProductsServiceClient().list_products(request=ListProductsRequest(parent=parent, page_size=1000)):
        if p.data_source == data_source:
            ids.add(p.offer_id)
    return ids


# --------------------------------------------------------------------------- main


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_account_argument(parser)
    parser.add_argument("--label", required=True, help="Feed label of the storefront to sync, e.g. CA")
    parser.add_argument("--limit", type=int, help="Stop after this many offers (testing; disables deletion)")
    parser.add_argument("--sku", nargs="+", help="Only products whose SKU is in this list (disables deletion)")
    parser.add_argument("--dry-run", action="store_true", help="Build everything, call nothing on Google")
    parser.add_argument("--no-delete", action="store_true", help="Keep offers Google has that BigCommerce no longer does")
    parser.add_argument("--workers", type=int, default=8, help="Parallel inserts (default 8)")
    parser.add_argument("--report", help="Write skipped/failed offers to this CSV")
    parser.add_argument("--sample", type=int, default=0, help="Print this many built offers as JSON (dry-run aid)")
    args = parser.parse_args(argv)

    label = args.label.upper()
    storefront = next((s for s in STOREFRONTS if s[1] == label), None)
    if not storefront or label not in STORES:
        sys.exit(f"error: no BigCommerce store configured for feed label {label!r}; known: {sorted(STORES)}")
    store_cfg = STORES[label]
    account = resolve_account(args)
    parent = account_parent(account)

    token = get_secret(f"BIGCOMMERCE_{store_cfg['store_hash']}_ACCESS_TOKEN", env_var="BC_ACCESS_TOKEN")
    bc = BigCommerce(store_cfg["store_hash"], token)
    store = load_store(bc)
    ctx = {
        **store,
        "brand_default": store_cfg["brand_default"],
        "feed_label": label,
        "language": storefront[2],
        "country": storefront[3][0],
    }
    print(f"store {store_cfg['store_hash']} ({store['url']}, {store['currency']}) -> {parent} feed {label}")

    brands = load_brands(bc)
    category_paths, _ = load_categories(bc)
    ctx["category_paths"] = category_paths

    t0 = time.time()
    offers, skipped, seen_products = [], [], 0
    params = {"is_visible": "true", "include": "variants,images,custom_fields"}
    if args.sku:
        params["sku:in"] = ",".join(args.sku)
    for product in bc.pages("/v3/catalog/products", **params):
        seen_products += 1
        built, skips = build_offers(product, ctx, brands, category_paths)
        offers.extend(built)
        skipped.extend(skips)
        if args.limit and len(offers) >= args.limit:
            offers = offers[: args.limit]
            break

    dupes = [oid for oid, n in Counter(o for o, _ in offers).items() if n > 1]
    if dupes:
        print(f"warning: {len(dupes)} duplicate offer ids, keeping the first of each: {dupes[:5]}")
        seen, unique = set(), []
        for oid, pi in offers:
            if oid not in seen:
                seen.add(oid)
                unique.append((oid, pi))
        offers = unique

    print(
        f"built {len(offers)} offers from {seen_products} products in {time.time() - t0:.0f}s; "
        f"skipped {len(skipped)}: {dict(Counter(r for _, r in skipped))}"
    )
    stats = Counter()
    for _, pi in offers:
        a = pi.product_attributes
        stats["with_gtin" if a.gtins else "without_gtin"] += 1
        stats["with_weight" if "shipping_weight" in a else "without_weight"] += 1
        stats[Availability(a.availability).name.lower()] += 1
        if "sale_price" in a:
            stats["on_sale"] += 1
    print(f"offer stats: {dict(stats)}")
    names = {v: k for k, v in GOOGLE_CATEGORIES.items()}
    gpc_counts = Counter(names.get(pi.product_attributes.google_product_category, "unmapped") for _, pi in offers)
    print(f"google categories: {dict(gpc_counts.most_common())}")

    for _, pi in offers[: args.sample]:
        print(json.dumps(json.loads(ProductInput.to_json(pi)), indent=1)[:2500])

    if args.dry_run:
        _write_report(args.report, skipped, [])
        print("dry run: nothing sent to Google")
        return 0

    data_source = find_data_source(parent, storefront[0])
    client = ProductInputsServiceClient()
    done, failed = 0, []
    lock = threading.Lock()
    t1 = time.time()

    def push(item):
        oid, pi = item
        try:
            insert_with_retry(client, InsertProductInputRequest(parent=parent, product_input=pi, data_source=data_source))
            return oid, None
        except gexc.GoogleAPICallError as exc:
            return oid, f"{type(exc).__name__}: {exc.message[:300] if hasattr(exc, 'message') else str(exc)[:300]}"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for oid, err in (f.result() for f in as_completed(pool.submit(push, o) for o in offers)):
            with lock:
                if err:
                    failed.append((oid, err))
                else:
                    done += 1
                if (done + len(failed)) % 500 == 0:
                    print(f"  {done + len(failed)}/{len(offers)} sent ({time.time() - t1:.0f}s)")
    print(f"inserted {done}, failed {len(failed)} in {time.time() - t1:.0f}s")
    for oid, err in failed[:10]:
        print(f"  FAILED {oid}: {err}")

    deleted = 0
    full_run = not args.limit and not args.sku
    if full_run and not args.no_delete:
        current = {oid for oid, _ in offers} | {oid for oid, _ in failed}
        stale = sorted(existing_offer_ids(parent, data_source) - current)
        if stale and len(stale) > max(50, len(offers) // 4):
            print(f"refusing to delete {len(stale)} stale offers (more than a quarter of the catalogue); pass --no-delete or check the feed")
        else:
            for oid in stale:
                name = f"{parent}/productInputs/{ctx['language']}~{label}~{oid}"
                try:
                    client.delete_product_input(request=DeleteProductInputRequest(name=name, data_source=data_source))
                    deleted += 1
                except gexc.GoogleAPICallError as exc:
                    failed.append((oid, f"delete: {exc}"))
            print(f"deleted {deleted} stale offers")
    elif not full_run:
        print("partial run: stale-offer deletion skipped")

    _write_report(args.report, skipped, failed)
    return 1 if failed else 0


def _write_report(path, skipped, failed):
    if not path:
        return
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["offer_id", "outcome", "detail"])
        for oid, reason in skipped:
            writer.writerow([oid, "skipped", reason])
        for oid, err in failed:
            writer.writerow([oid, "failed", err])
    print(f"report written to {path}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except gexc.GoogleAPICallError as exc:
        sys.exit(f"error: {explain_api_error(exc)}")
