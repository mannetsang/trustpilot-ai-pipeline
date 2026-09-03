# POS – cash register

A point-of-sale for the salons, starting with cash transactions. The screen
design it follows is `design/superhairpieces-pos-reference.html` (a self-contained
prototype; open it through `python -m http.server` as its banner explains).

Customers are not modelled on purpose. The first use is the cash till at the
ESI Montreal trade show, selling the products on the show-prep Google Sheet.

## The register

`src/cloud_run/` is the register: a Flask service that serves the page in
`static/` and a JSON API over the Supabase database. Staff open it in a browser
on the booth tablet or laptop, type their name and the till code, and sell.
A USB or Bluetooth barcode scanner works as a keyboard: the search box treats
a code followed by Enter as a scan.

What it enforces server-side, so the browser can never ring a wrong amount:
every line is priced from `pos_products`, totals are recomputed, cash received
must cover the total, and each sale carries a `clientRecordId` from the
browser so a retry after a dropped connection cannot record the sale twice.
Refunds are cash only and need the till code typed again as approval.

Configuration is by environment variable (see the docstring in `main.py`).
Two values come from Secret Manager: `SUPABASE_DB_URL`, mounted at deploy
time, and `POS_ACCESS_CODE`, the till code, which the service reads itself and
re-reads every minute, so it can be created or rotated without a redeploy.
Until that secret exists the till stays locked and says so.

Create the till code once (one line, Windows cmd; pick your own digits):

```
echo|set /p="123456"|gcloud secrets create POS_ACCESS_CODE --replication-policy=automatic --project=shp-ai-bot-2026 --data-file=-
```

### Running it locally

```
pip install -r pos\src\cloud_run\requirements.txt
set SUPABASE_DB_URL=... && set POS_ACCESS_CODE=1234 && python pos\src\cloud_run\main.py
```

then open http://localhost:8080.

### Deploying

The register is live at https://shp-pos-304363458561.northamerica-northeast1.run.app

`pos/deploy.sh` deploys the service `shp-pos` to Cloud Run in
`northamerica-northeast1` (Montreal) from source, and prints its URL. It runs
from a Claude cloud session, a workstation, or the `deploy-pos.yml` workflow
once the `GCP_SA_KEY` repository secret is set (it is empty today, which is
why none of the deploy workflows in this repo have ever succeeded).

The deploying identity needs these roles on `shp-ai-bot-2026`: Cloud Run
Admin, Cloud Build Editor, Artifact Registry Administrator, Storage Admin,
Service Usage Admin, and Service Account User on the Compute Engine default
service account that the service runs as. That runtime account also needs
Secret Manager Secret Accessor for the two secrets above.

## Data

Lives in Supabase (Postgres). Schema files are in `supabase/migrations/` and
are applied once each, in name order, by `apply_migration.py`.

| Table / view | What it holds |
|---|---|
| `pos_transactions` | One row per sale: where, who rang it, currency, totals, cash received and change, refund details. |
| `pos_transaction_items` | One row per line sold: SKU, name, quantity, unit price, line total, and the product and barcode it came from. |
| `pos_products` | The product list, one row per SKU: name, category, tax-inclusive price, cost, quantity brought to the show, flags, and the raw sheet row. |
| `pos_product_barcodes` | Scannable codes (UPC, EAN, ASIN) pointing at a product. Several codes may point at one product. |
| `pos_daily_cash_summary` | View. Per till, per Toronto calendar day, per currency: sales, cash in, change out, cash refunds, net drawer. |
| `pos_schema_migrations` | Which migration files have been applied. |

Rules the database enforces, so no register build can disagree:

- `grand_total = subtotal - discount_total + tax_total`, or, when
  `prices_include_tax` is set, `grand_total = subtotal - discount_total` with
  `tax_total` being the amount backed out for the receipt.
- An inspection order records `deposit_total` and `amount_paid` equals it;
  a normal sale has no deposit and `amount_paid` equals `grand_total`.
- Cash sales must carry `cash_received`, and `change_given` must equal
  `cash_received - amount_paid`.
- `payment_method` is `cash` only. Widen the check constraint when card, gift
  card or split tenders are added.
- A refund cannot exceed what was paid; `status = 'refunded'` requires one.
- `client_record_id` is unique: the register queues sales offline and retries,
  and a retry must not create a duplicate.
- Amounts are in the row's `currency`. Never sum across currencies.

Row Level Security is on with no policies, so only server-side code holding
the service key can read or write. Add policies once the register's auth is
decided.

## Applying a migration

One line, Windows cmd, from the repo root:

```
python pos\apply_migration.py
```

`--list` shows applied and pending files without changing anything.

Credentials come from Secret Manager on `shp-ai-bot-2026` through
`lib/secrets.py`, or from a gitignored `.env` for local runs. There are two
routes in and the script picks the first that has credentials:

| Route | Secret | Needs | Where it works |
|---|---|---|---|
| `--via api` | `SUPABASE_ACCESS_TOKEN` (a Supabase personal access token) | HTTPS only | Anywhere, including Claude cloud sessions |
| `--via db` | `SUPABASE_DB_URL` | outbound port 6543 (or 5432), plus `pip install "psycopg[binary]"` or `psql` on PATH | Your own machine |

The API route runs the SQL through the Supabase Management API against project
`ngwlwntvoteuafeplobx` (the project reference is an identifier, not a secret;
override it with the `SUPABASE_PROJECT_REF` environment variable).

## Importing the product list

Products come from the tab `所有拿货及定价` of the show-prep sheet, which is
shared with the sessions' service account. One line, Windows cmd:

```
python pos\import_products.py
```

`--dry-run` reads and reports without writing. `--deactivate-missing` also
marks SKUs that are no longer on the sheet as not sellable. Re-running is
safe: rows are matched on SKU. What the importer does with the sheet's quirks
is documented at the top of the script: the `ESI Montreal -` prefix is dropped
from names, a SKU on several rows becomes one product with several barcodes,
quantities such as `2+1*` become 3 with the raw text kept, and a blank
Montreal price makes the product inactive.

Prices on the sheet are whole dollars with tax included (the sheet's website
price is exactly the show price divided by 1.13), so `price_includes_tax` is
true on every imported product and a sale of them sets `prices_include_tax`
on the transaction.

## Adding a migration

Create `supabase/migrations/0002_<what>.sql`. Write it to be safe to re-run
(`if not exists`, `create or replace`) and never edit a file that has already
been applied somewhere.
