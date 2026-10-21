# Group dashboard

One page for the whole business: six storefronts, every source this repo can
read, and — just as deliberately — every source it cannot.

The Shopping dashboard next door (`google-merchant/dashboard/`) is deep on one
market: superhairpieces.ca, its Merchant Center feed and its product catalogue.
This one is wide. It answers "how is the group doing" rather than "what is
wrong with the .ca feed".

Live at the `group-dashboard` Cloud Run service, rebuilt daily.

```
python group-dashboard/collect.py                      # all six, writes group_data.json
python group-dashboard/collect.py --store ca com --no-sales --print
python group-dashboard/build_site.py                   # renders site/index.html
```

## Tabs

| Tab | What it answers |
|---|---|
| **Group** | Every market on one row: organic clicks, catalogue size, orders, revenue in its own currency, whether its Shopping feed is connected. |
| **Organic search** | Six markets on one chart, switchable between clicks, impressions and average position. Queries that appear in more than one market, where in the world the searches happen, and per-market device and surface mix. |
| **Catalogue** | Products, visibility, variants, stock and sale coverage per market, then price bands, brands, categories and best sellers for the market you pick. |
| **Sales** | Revenue by month per market, order-status mix, and how customers pay. |
| **Coverage** | Every market against every source, with the exact fix for each gap. |

## What it reads, and what it cannot

| Source | Markets | Note |
|---|---|---|
| Search Console | all six | Needs the service account on each property; see `google-search-console/README.md`. |
| BigCommerce catalogue | all six | |
| BigCommerce orders | ca, com, es | The .nl, .fr and .de tokens carry catalogue scope only and answer 403 on `/v2/orders`. |
| Merchant Center | ca | Account `289630622` (.com and EU) has not been shared with the service account. |

Nothing here guesses around a gap. A market with no order scope shows "no
access" in the revenue column and a row in Coverage naming the fix; it never
shows a zero, because a zero would read as "sold nothing".

## Internal items

The storefronts double as an internal ordering system — office coffee, sugar,
garbage bags, thermal paper, surcharges, services, deposits. They are real
products with real order counts: **"Extra Charge" is the single best-selling
item on .ca by units**, ahead of every hairpiece. `lib/catalogue.py` holds the
patterns that identify them, first written to keep garbage bags out of Google
Shopping. The Catalogue tab counts them in their own column and leaves them out
of the best-seller ranking, where they would otherwise be the whole top of the
list.

## Money

- **Revenue is never added across currencies.** The Group tab subtotals by
  currency and names the markets in each. The EUR subtotal covers .es alone
  today, and says so.
- Revenue is `total_inc_tax` minus `refunded_amount`, over orders that are not
  Incomplete (0), Cancelled (5) or Declined (6). A refunded order keeps its
  original total in the API, so counting it whole overstates the month.
- `payment_method` is free text in places. The three labels BigCommerce records
  for card payments are merged into one row, and entries carrying service-request
  or contract numbers are grouped as "Other (free text)" rather than invented
  into categories. Case variants (`E-Transfer` / `e-Transfer`) collapse together.

## Cost of a run

| Step | Requests | Time |
|---|---|---|
| Search Console | ~14 per property | seconds |
| Catalogue | ~6 per store | seconds |
| Orders | 1 + one per 250 orders | the bulk of it |

The 12-month order window is about 190 pages across the three readable stores,
fetched four at a time. BigCommerce reports millions of requests left in its
30-second window, so the limit is wall time, not quota. `--no-sales` skips it
when you only want the catalogue and search sides.

## Store registry

`lib/stores.py` is the single source of truth for the six: hash, canonical
host, currency, market, feed label and token ids. `bing-indexing/config.py`,
`google-merchant/merchant_api.py` and `google-search-console/config.py` each
predate it and keep their own copy; fold them in next time one is touched.

It also carries `_clean_token()`, which exists because
`BIGCOMMERCE_qet21urb3p_ACCESS_TOKEN` holds the whole pasted credential block
rather than the bare token. An HTTP header built from that raises `ValueError`
with the client secret inside the message. The secret itself still wants
fixing — see `docs/credentials.md`.
