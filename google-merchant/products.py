"""Insert or delete a product in a Merchant Center data source over the Merchant API.

    python google-merchant/products.py insert --account 123456789 --label CA product.json
    python google-merchant/products.py delete --account 123456789 --label CA --offer-id SKU123

product.json is the ProductAttributes payload in Merchant API JSON form, plus
top-level "offerId". Minimal example:

    {
      "offerId": "SHP-TEST-001",
      "title": "Test hair system",
      "description": "Smoke test product. Delete me.",
      "link": "https://www.superhairpieces.ca/",
      "imageLink": "https://www.superhairpieces.ca/product-image.jpg",
      "availability": "OUT_OF_STOCK",
      "condition": "NEW",
      "price": {"amountMicros": 199000000, "currencyCode": "CAD"},
      "brand": "Superhairpieces"
    }

The data source is looked up by the storefront's feed label (see STOREFRONTS
in merchant_api.py), so run data_sources.py create first. Inserting the same
offerId again replaces the earlier version of the product. Products appear
under Products in Merchant Center within a few minutes.
"""

import argparse
import json
import sys

from google.api_core import exceptions as gexc
from google.shopping.merchant_datasources_v1 import DataSourcesServiceClient, ListDataSourcesRequest
from google.shopping.merchant_products_v1 import (
    DeleteProductInputRequest,
    InsertProductInputRequest,
    ProductAttributes,
    ProductInput,
    ProductInputsServiceClient,
)

from merchant_api import (
    STOREFRONTS,
    account_parent,
    add_account_argument,
    display_name_for,
    explain_api_error,
    resolve_account,
)


def _storefront(label):
    for s in STOREFRONTS:
        if s[1] == label.upper():
            return s
    sys.exit(f"error: unknown feed label {label!r}; known: {[s[1] for s in STOREFRONTS]}")


def _data_source_name(parent, storefront):
    wanted = display_name_for(storefront[0])
    client = DataSourcesServiceClient()
    for ds in client.list_data_sources(request=ListDataSourcesRequest(parent=parent)):
        if ds.display_name == wanted:
            return ds.name
    sys.exit(f"error: no data source named {wanted!r}; run data_sources.py create --only {storefront[1]}")


def cmd_insert(parent, storefront, args):
    with open(args.product, encoding="utf-8") as handle:
        payload = json.load(handle)
    offer_id = payload.pop("offerId", None) or args.offer_id
    if not offer_id:
        sys.exit("error: product JSON needs an offerId (or pass --offer-id)")

    _, label, lang, _ = storefront
    product_input = ProductInput(
        offer_id=str(offer_id),
        content_language=lang,
        feed_label=label,
        product_attributes=ProductAttributes.from_json(json.dumps(payload)),
    )
    data_source = _data_source_name(parent, storefront)
    if args.dry_run:
        print(f"would insert {offer_id} into {data_source}")
        return
    client = ProductInputsServiceClient()
    result = client.insert_product_input(
        request=InsertProductInputRequest(parent=parent, product_input=product_input, data_source=data_source)
    )
    print(f"inserted {result.name}")
    print(f"product resource: {result.product}")


def cmd_delete(parent, storefront, args):
    if not args.offer_id:
        sys.exit("error: delete needs --offer-id")
    _, label, lang, _ = storefront
    data_source = _data_source_name(parent, storefront)
    # productInput names are {channel}~{contentLanguage}~{feedLabel}~{offerId};
    # ONLINE is the only channel for non-local sources.
    name = f"{parent}/productInputs/online~{lang}~{label}~{args.offer_id}"
    if args.dry_run:
        print(f"would delete {name} from {data_source}")
        return
    ProductInputsServiceClient().delete_product_input(
        request=DeleteProductInputRequest(name=name, data_source=data_source)
    )
    print(f"deleted {name}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["insert", "delete"])
    parser.add_argument("product", nargs="?", help="Path to the product JSON (insert only)")
    add_account_argument(parser)
    parser.add_argument("--label", required=True, help="Feed label of the target storefront, e.g. CA")
    parser.add_argument("--offer-id", help="Offer id (SKU); required for delete, overrides the JSON for insert")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "insert" and not args.product:
        parser.error("insert needs a product JSON path")

    account = resolve_account(args)
    parent = account_parent(account)
    storefront = _storefront(args.label)
    try:
        {"insert": cmd_insert, "delete": cmd_delete}[args.command](parent, storefront, args)
    except gexc.GoogleAPICallError as exc:
        sys.exit(f"error: {explain_api_error(exc)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
