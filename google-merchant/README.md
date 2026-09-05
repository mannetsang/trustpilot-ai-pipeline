# Google Merchant Center — Merchant API

Scripts that drive Merchant Center through the **Merchant API** (the successor
to the Content API for Shopping): register the GCP project, create one product
data source per storefront, and push products into them.

| Merchant Center account | ID | Storefronts | Service account added? |
|---|---|---|---|
| Superhairpieces.ca | `5298296396` | superhairpieces.ca | yes, project registered |
| Superhairpieces | `289630622` | .com and the EU sites | no |
| Gen'C Beauty | `670525760` | genc | no |

The scripts only see accounts the service account has been added to, so
`.com` and the EU storefronts need step 2 below repeated on `289630622`
before anything can be created there. Account IDs are not secrets. Neither is anything else here: the scripts
authenticate with Application Default Credentials, so there is no API key and
nothing for Secret Manager to hold.

## One-time setup

1. **Enable the API** on `shp-ai-bot-2026` (done):
   `gcloud services enable merchantapi.googleapis.com --project=shp-ai-bot-2026`

2. **Add the service account to Merchant Center.** In Merchant Center:
   Settings → *People and access* → *Add person*, paste the service account
   email, access level **Admin**. Do this for every account the scripts should
   touch. Two identities are involved:

   | Runs where | Identity |
   |---|---|
   | Claude cloud sessions | `claude-sessions@shp-ai-bot-2026.iam.gserviceaccount.com` |
   | GitHub Actions | the `client_email` inside the `GCP_SA_KEY` repository secret |

   If those are the same account, add it once. Your own Google account is
   already an Admin, so running locally after
   `gcloud auth application-default login` works without this step.

3. **Register the project with the account** (once per account). Until this
   runs, every call fails with `GCP_NOT_REGISTERED`:

   ```
   python google-merchant/register_developer.py --account 5298296396 --email manne@superhairpieces.com
   ```

   Google says to allow about five minutes before the next call; verify with
   `--check`.

4. **Create the data sources**:

   ```
   python google-merchant/data_sources.py create --account 5298296396
   python google-merchant/data_sources.py list --account 5298296396
   ```

   Steps 3 and 4 can also be run from GitHub: *Actions* →
   *Google Merchant Center (Merchant API)* → *Run workflow*.

## What gets created

Merchant Center needs a separate primary data source for each feed label and
content language, so `create` makes six API-input sources, enabled for Shopping
ads and free listings:

| Display name | Feed label | Language | Country |
|---|---|---|---|
| superhairpieces.ca API feed | CA | en | CA |
| superhairpieces.com API feed | US | en | US |
| superhairpieces.nl API feed | NL | nl | NL |
| superhairpieces.fr API feed | FR | fr | FR |
| superhairpieces.es API feed | ES | es | ES |
| superhairpieces.de API feed | DE | de | DE |

`create` is idempotent: it matches on display name and skips what exists.
Any existing feeds (Content API, file uploads, the BigCommerce app) are left
alone; the API sources sit alongside them. Edit `STOREFRONTS` in
`merchant_api.py` to change the set.

## Pushing a product

```
python google-merchant/products.py insert --account 5298296396 --label CA product.json
python google-merchant/products.py delete --account 5298296396 --label CA --offer-id SHP-TEST-001
```

`product.json` is the Merchant API `ProductAttributes` object plus a
top-level `offerId`; `products.py --help` shows a minimal example. Useful for
one-off tests; the catalogue itself is pushed by the sync below.

## Catalogue sync from BigCommerce

`sync_bigcommerce.py` rebuilds the whole feed from the BigCommerce catalogue
on every run and pushes it into the storefront's API data source:

```
python google-merchant/sync_bigcommerce.py --label CA --dry-run --sample 3
python google-merchant/sync_bigcommerce.py --label CA --limit 25
python google-merchant/sync_bigcommerce.py --label CA
```

It runs daily from GitHub (*Google Merchant Center product sync*, 05:30
Toronto) and can be triggered by hand with a dry-run or a limit.

### What becomes an offer

One offer per **variant** of every visible product. Variants of one product
share an `item_group_id` (the product SKU) so Merchant Center groups them.

| Merchant Center | From BigCommerce |
|---|---|
| offer id | variant SKU, or `bc-v<variant id>` when the SKU has spaces/quotes |
| title | product name, plus the option labels for multi-variant products |
| link | storefront URL, with `?sku=` so the variant is preselected |
| image / additional images | variant image, else thumbnail then the rest |
| price / sale price | variant price (falls back to product), sale only when lower |
| availability | inventory tracking mode and level; `preorder` passes through |
| brand | BigCommerce brand, default Superhairpieces |
| gtin / mpn | only when valid (see below), else `identifier_exists=false` |
| shipping weight | variant calculated weight in the store's unit |
| shipping | free-shipping products get a 0.00 CA shipping line |
| product_type | full category paths |

Skipped, and listed in the report CSV: hidden products, `availability:
disabled`, digital products, variants with no price or no image,
`purchasing_disabled` variants.

A full run also **deletes** offers that are in the data source but not in the
catalogue any more, so hiding a product in BigCommerce removes it from Google
the next morning. Safety valve: if more than a quarter of the feed would go,
nothing is deleted and the run says so. `--limit`/`--sku` runs never delete.

### Data-quality findings from the first run

- `upc`/`mpn` hold Excel-mangled values like `6.14043E+11`, and `gtin`
  sometimes holds `Default Tax Class` (a shifted import column). The sync
  drops these, so supplies from Walker Tape etc. go out without a GTIN. Fixing
  the UPCs in BigCommerce would improve their Shopping performance.
- 179 variants have no weight; Google disapproves those when shipping is
  weight-based (the shampoo in the old autofeed was one). Set weights in
  BigCommerce.
- Google's own **autofeed** (`PRODUCTS SOURCE 1`) crawls the site and creates
  duplicate, poorer offers. Turn it off in Merchant Center once this feed is
  approved: Data sources → PRODUCTS SOURCE 1 → disable automatic feeds.

### Adding the .com and EU storefronts

Each needs: its BigCommerce store hash and a `BIGCOMMERCE_<hash>_ACCESS_TOKEN`
secret with catalogue read scope, an entry in `STORES` in
`sync_bigcommerce.py`, the service account added to Merchant Center account
`289630622`, that account registered with `register_developer.py`, and
`data_sources.py create` run there. Then add the label to the workflow's
`options`.

## Local runs

Set the account once in the gitignored `.env` at the repo root so `--account`
can be dropped:

```
GMC_ACCOUNT_ID=5298296396
```

Install the client libraries with `pip install -r google-merchant/requirements.txt`
(in a Claude cloud session use `/opt/gcp-venv/bin/pip`, then run the scripts
with `/opt/gcp-venv/bin/python`).

## Troubleshooting

| Error | Meaning | Fix |
|---|---|---|
| `GCP_NOT_REGISTERED` (401) | Project never registered with this account | Run `register_developer.py` |
| `PERMISSION_DENIED` (403) | The calling identity is not a user on the account | Add it under People and access, wait a few minutes |
| `data source ... not found` from `products.py` | `create` has not been run for that label | `data_sources.py create --only <label>` |
| Account "not migrated" errors | Account still on legacy multi-locale feeds | Accept the data-source migration prompt in Merchant Center |
