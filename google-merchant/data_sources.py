"""Create and inspect Merchant Center product data sources over the Merchant API.

    python google-merchant/data_sources.py list   --account 123456789
    python google-merchant/data_sources.py create --account 123456789
    python google-merchant/data_sources.py create --account 123456789 --only CA US
    python google-merchant/data_sources.py delete --account 123456789 --only CA
    python google-merchant/data_sources.py autofeed --account 123456789 --off

`autofeed` shows or switches Google's automatic website-crawl source
("PRODUCTS SOURCE 1"), which duplicates the API feed once that carries the
catalogue.

`create` is idempotent: a data source whose display name already exists is
left alone and reported as "exists". It makes one API-input primary product
data source per storefront (see STOREFRONTS in merchant_api.py), enabled for
Shopping ads and free listings. Products are then pushed into a data source
with products.py.
"""

import argparse
import sys

from google.api_core import exceptions as gexc
from google.shopping import type as gtype
from google.shopping.merchant_datasources_v1 import (
    CreateDataSourceRequest,
    DataSource,
    DataSourcesServiceClient,
    DeleteDataSourceRequest,
    ListDataSourcesRequest,
    PrimaryProductDataSource,
)

from merchant_api import (
    STOREFRONTS,
    account_parent,
    add_account_argument,
    display_name_for,
    explain_api_error,
    resolve_account,
)

_Dest = PrimaryProductDataSource.Destination
_DestEnum = gtype.Destination.DestinationEnum


def _selected(only):
    if not only:
        return list(STOREFRONTS)
    wanted = {label.upper() for label in only}
    chosen = [s for s in STOREFRONTS if s[1] in wanted]
    missing = wanted - {s[1] for s in chosen}
    if missing:
        sys.exit(f"error: unknown feed label(s) {sorted(missing)}; known: {[s[1] for s in STOREFRONTS]}")
    return chosen


def _existing_by_name(client, parent):
    found = {}
    for ds in client.list_data_sources(request=ListDataSourcesRequest(parent=parent)):
        found[ds.display_name] = ds
    return found


def _kind(ds: DataSource) -> str:
    for field in (
        "primary_product_data_source",
        "supplemental_product_data_source",
        "local_inventory_data_source",
        "regional_inventory_data_source",
        "promotion_data_source",
        "product_review_data_source",
        "merchant_review_data_source",
    ):
        if field in ds:
            return field.removesuffix("_data_source")
    return "unknown"


def cmd_list(client, parent, args):
    rows = []
    for ds in client.list_data_sources(request=ListDataSourcesRequest(parent=parent)):
        p = ds.primary_product_data_source if "primary_product_data_source" in ds else None
        rows.append(
            (
                ds.data_source_id,
                _kind(ds),
                DataSource.Input(ds.input).name,
                p.feed_label if p else "",
                p.content_language if p else "",
                ",".join(p.countries) if p else "",
                ds.display_name,
            )
        )
    if not rows:
        print("no data sources on this account")
        return
    header = ("id", "kind", "input", "label", "lang", "countries", "display name")
    widths = [max(len(str(r[i])) for r in rows + [header]) for i in range(len(header))]
    for r in [header] + rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(r, widths)).rstrip())


def cmd_create(client, parent, args):
    # A dry run must work before access is set up, so skip the lookup then.
    existing = {} if args.dry_run else _existing_by_name(client, parent)
    for storefront, label, lang, countries in _selected(args.only):
        name = display_name_for(storefront)
        if name in existing:
            print(f"exists   {label}  {existing[name].name}")
            continue
        primary = PrimaryProductDataSource(
            feed_label=label,
            content_language=lang,
            countries=countries,
            destinations=[
                _Dest(destination=_DestEnum.SHOPPING_ADS, state=_Dest.State.ENABLED),
                _Dest(destination=_DestEnum.FREE_LISTINGS, state=_Dest.State.ENABLED),
            ],
        )
        ds = DataSource(display_name=name, primary_product_data_source=primary)
        if args.dry_run:
            print(f"would create {label}  {name}  ({lang}, {','.join(countries)})")
            continue
        created = client.create_data_source(request=CreateDataSourceRequest(parent=parent, data_source=ds))
        print(f"created  {label}  {created.name}")


def cmd_delete(client, parent, args):
    if not args.only:
        sys.exit("error: delete requires --only <label...>; refusing to delete every storefront at once")
    existing = _existing_by_name(client, parent)
    for storefront, label, _, _ in _selected(args.only):
        name = display_name_for(storefront)
        ds = existing.get(name)
        if not ds:
            print(f"absent   {label}  {name}")
            continue
        if args.dry_run:
            print(f"would delete {label}  {ds.name}")
            continue
        client.delete_data_source(request=DeleteDataSourceRequest(name=ds.name))
        print(f"deleted  {label}  {ds.name}")


def cmd_autofeed(client, parent, args):
    import google.shopping.merchant_accounts_v1 as accounts
    from google.protobuf import field_mask_pb2

    svc = accounts.AutofeedSettingsServiceClient()
    name = f"{parent}/autofeedSettings"
    if args.on or args.off:
        if args.dry_run:
            print(f"would set enable_products={bool(args.on)} on {name}")
            return
        svc.update_autofeed_settings(
            request=accounts.UpdateAutofeedSettingsRequest(
                autofeed_settings=accounts.AutofeedSettings(name=name, enable_products=bool(args.on)),
                update_mask=field_mask_pb2.FieldMask(paths=["enable_products"]),
            )
        )
    settings = svc.get_autofeed_settings(request=accounts.GetAutofeedSettingsRequest(name=name))
    print(f"autofeed products: {'enabled' if settings.enable_products else 'disabled'} (eligible: {settings.eligible})")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["list", "create", "delete", "autofeed"])
    parser.add_argument("--on", action="store_true", help="autofeed: enable Google's automatic product source")
    parser.add_argument("--off", action="store_true", help="autofeed: disable it")
    add_account_argument(parser)
    parser.add_argument("--only", nargs="+", metavar="LABEL", help="Restrict to these feed labels, e.g. CA US")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without calling the API")
    args = parser.parse_args(argv)

    account = resolve_account(args)
    parent = account_parent(account)
    client = DataSourcesServiceClient()
    try:
        {"list": cmd_list, "create": cmd_create, "delete": cmd_delete, "autofeed": cmd_autofeed}[args.command](
            client, parent, args
        )
    except gexc.GoogleAPICallError as exc:
        sys.exit(f"error: {explain_api_error(exc)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
