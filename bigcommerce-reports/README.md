# BigCommerce reports

## revenue_by_payment_method.py

Revenue split by payment method for a date range. BigCommerce exposes no
aggregate endpoint for this, so the script pages through the v2 Orders API and
sums the orders itself.

### Setup

Create a **store-level API account** in BigCommerce (Settings → API accounts) with
the `Orders` scope set to *read-only*, then put the credentials in `.env` at the
repo root — it is gitignored, and the token must never be committed:

```
BC_STORE_HASH=abc123
BC_ACCESS_TOKEN=...
```

The store hash is the `stores/<hash>` segment of the API path BigCommerce shows
you when the account is created.

**Running inside a Claude Code cloud session instead?** Store the token as an
**API credential** on the cloud environment (host `api.bigcommerce.com`, custom
header `X-Auth-Token` with an empty prefix) and put only `BC_STORE_HASH` in the
environment's variables. Leave `BC_ACCESS_TOKEN` unset: the agent proxy attaches
the header after the request leaves the sandbox, so the token is never readable
from inside the session. The script detects the missing token and sends the
request unauthenticated on purpose.

### Run

```bash
python3 bigcommerce-reports/revenue_by_payment_method.py --year 2026
```

Useful flags:

| Flag | Effect |
|---|---|
| `--start 2026-01-01 --end 2026-07-01` | Arbitrary range instead of a calendar year (end is exclusive) |
| `--csv out.csv` | Also write the aggregates as CSV |
| `--by-channel` | Break each row down by `channel_id` (multi-storefront) |
| `--label superhairpieces.com` | Tag the output when running across several stores |
| `--include-all-statuses` | Keep Incomplete / Cancelled / Declined orders, which are excluded by default |

No third-party packages — stdlib only.

### How the numbers are built

- **Gross** is the sum of `total_inc_tax`, the order total the customer was charged.
- **Refunded** is the sum of `refunded_amount`, so partial refunds land in the
  right payment method rather than disappearing.
- **Net** is gross minus refunded, and the `% net` column is each method's share
  of net revenue.
- Orders with status **Incomplete (0)**, **Cancelled (5)** or **Declined (6)** are
  excluded by default: those are abandoned carts and orders that never took
  money. The run prints how many it dropped and why.
- Results are grouped **per currency** and never summed across currencies — a CAD
  total and a EUR total are different quantities. If you need one consolidated
  figure, convert downstream at a rate you control.
- Dates filter on `date_created` in **UTC**, which can shift a handful of orders
  at each year boundary relative to the BigCommerce admin's store-timezone view.
- The method name comes from the order's `payment_method` field, so gateways
  appear exactly as BigCommerce labels them (`Credit Card`, `PayPal (PayPal
  Checkout)`, …). Orders with no value — store credit or fully discounted, for
  instance — group under `(none recorded)`.

If the six storefronts are separate BigCommerce stores, run the script once per
store with that store's hash and `--label`, then combine the CSVs.
