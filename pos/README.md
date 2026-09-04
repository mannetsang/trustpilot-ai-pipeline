# POS – cash register

A point-of-sale for the salons, starting with cash transactions. The screen
design it follows is `design/superhairpieces-pos-reference.html` (a self-contained
prototype; open it through `python -m http.server` as its banner explains).

Customers are not modelled on purpose. The first use is the cash till at the
ESI Montreal trade show, selling the products on the show-prep Google Sheet.

## The register

`src/cloud_run/` is the register: a Flask service that serves the page in
`static/`, the admin portal at `/admin`, and a JSON API over the Supabase
database. Staff open it in a browser on the booth tablet or laptop, tap their
name, enter their PIN, and sell. A USB or Bluetooth barcode scanner works as a
keyboard: the search box treats a code followed by Enter as a scan.

### Events

An event is a show or any selling period: a name, a currency and a start and
end date, managed in the admin portal. The register sells under the event
chosen in its header (remembered per device; the event running today is the
default), every sale carries the event it was rung under, and the Sales list
and Analytics read one event at a time. Archiving an event hides it from the
selector and keeps its sales.

### Users and the admin portal

Accounts live in `pos_users`. Each person has a name, a role and a personal
PIN of 4 to 8 digits, stored as a salted hash. Cashiers can sell and see
sales. Admins can also open `/admin` to add users, reset PINs, change roles
and deactivate people, and they approve refunds on the register by entering
their name and PIN.

The master code (secret `POS_ACCESS_CODE`) opens the admin portal on its own.
That is how the first admin gets created, and the recovery path if every admin
forgets their PIN. Keep it with whoever owns the project, not with booth staff.
The service reads it live and re-reads every minute, so creating or rotating
it needs no redeploy.

Five wrong PINs in a row lock a name for one minute.

What it enforces server-side, so the browser can never ring a wrong amount:
every line is priced from `pos_products`, totals are recomputed (there is no
tax: the total is the sum of the shelf prices minus any discount), cash
received must cover the total, and each sale carries a `clientRecordId` from
the browser so a retry after a dropped connection cannot record the sale twice.
Refunds are cash only and need the till code typed again as approval.

Configuration is by environment variable (see the docstring in `main.py`).
Two values come from Secret Manager: `SUPABASE_DB_URL`, mounted at deploy
time, and `POS_ACCESS_CODE`, the master code described above.

Create the master code once (one line, Windows cmd; pick your own digits):

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
| `pos_products` | The product list, one row per SKU: name, category, price, cost, quantity brought to the show, flags, and the raw sheet row. |
| `pos_product_barcodes` | Scannable codes (UPC, EAN, ASIN) pointing at a product. Several codes may point at one product. |
| `pos_daily_cash_summary` | View. Per till, per Toronto calendar day, per currency: sales, cash in, change out, cash refunds, net drawer. |
| `pos_events` | Shows and selling periods: code, name, dates, currency, active flag. Sales carry `event_id`. |
| `pos_users` | Register and admin accounts: name, role, salted PIN hash, active flag, last sign-in. |
| `pos_schema_migrations` | Which migration files have been applied. |

Rules the database enforces, so no register build can disagree:

- `grand_total = subtotal - discount_total`. There is no tax handling anywhere:
  the price on the shelf is what the customer pays.
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

Prices on the sheet are whole dollars and are charged exactly as listed.

## Adding a migration

Create `supabase/migrations/0002_<what>.sql`. Write it to be safe to re-run
(`if not exists`, `create or replace`) and never edit a file that has already
been applied somewhere.
