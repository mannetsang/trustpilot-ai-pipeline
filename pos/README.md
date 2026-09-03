# POS – cash register

A point-of-sale for the salons, starting with cash transactions. The screen
design it follows is `design/superhairpieces-pos-reference.html` (a self-contained
prototype; open it through `python -m http.server` as its banner explains).

Customers are not modelled on purpose. Products are captured as text per line
until the product list is decided; a products table can be referenced later
without touching existing rows.

## Data

Lives in Supabase (Postgres). Schema files are in `supabase/migrations/` and
are applied once each, in name order, by `apply_migration.py`.

| Table / view | What it holds |
|---|---|
| `pos_transactions` | One row per sale: where, who rang it, currency, totals, cash received and change, refund details. |
| `pos_transaction_items` | One row per line sold: SKU, name, spec text, grade, quantity, unit price, line total. |
| `pos_daily_cash_summary` | View. Per till, per Toronto calendar day, per currency: sales, cash in, change out, cash refunds, net drawer. |
| `pos_schema_migrations` | Which migration files have been applied. |

Rules the database enforces, so no register build can disagree:

- `grand_total = subtotal - discount_total + tax_total`.
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

Credentials come from Secret Manager (`SUPABASE_DB_URL` on `shp-ai-bot-2026`)
through `lib/secrets.py`, or from a `SUPABASE_DB_URL` line in a gitignored
`.env` for local runs. One line, Windows cmd:

```
python pos\apply_migration.py
```

`--list` shows applied and pending files without changing anything. The script
uses the `psycopg` package if installed (`pip install "psycopg[binary]"`) and
falls back to the `psql` command.

A direct Postgres connection needs outbound port 5432 (or 6543 for the pooler).
Where only HTTPS is open, paste the `.sql` into the Supabase SQL editor and then
insert its filename into `pos_schema_migrations` so the script stays in step.

## Adding a migration

Create `supabase/migrations/0002_<what>.sql`. Write it to be safe to re-run
(`if not exists`, `create or replace`) and never edit a file that has already
been applied somewhere.
