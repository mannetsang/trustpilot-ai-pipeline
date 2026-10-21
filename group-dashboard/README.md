# Group dashboard

One page for the whole business: six storefronts, every source this repo can
read, and — just as deliberately — every source it cannot.

**This is the only dashboard.** It began as a second one beside the Shopping
dashboard, which was deep on superhairpieces.ca alone, and the two overlapped
on organic search and gave the business two URLs for one question. The .ca
Merchant Center tabs now live here as **Shopping feed** and **Action plan**,
and the `merchant-dashboard` service redirects here
(`google-merchant/dashboard/redirect/`).

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
| **Organic search** | Six markets on one chart, switchable between clicks, impressions and average position. Then top queries and the countries they come from, for the whole group or one market on its own, over the full history or the last 28 days. Across the group a query carries the markets it appears in and an impression-weighted position; inside one market it carries that market's own CTR. Per-market device and surface mix below. |
| **Catalogue** | Products, visibility, variants, stock and sale coverage per market, then price bands, brands, categories and best sellers for the market you pick. |
| **Sales** | Revenue by month per market, order-status mix, and how customers pay. |
| **Shopping feed** | All three Merchant Center accounts: offers, how much of each feed is actually serving, what is blocking the rest, and a year of Shopping clicks and impressions. Then superhairpieces.ca in detail — approval rate, every disapproval and warning, how Google categorised the offers, and a filterable explorer over all 1,014 products. |
| **Action plan** | The fourteen ranked actions from the Google Shopping review, ticked off in your browser. |
| **Coverage** | Every market against every source, with the exact fix for each gap. |

## What it reads, and what it cannot

| Source | Markets | Note |
|---|---|---|
| Search Console | all six | Needs the service account on each property; see `google-search-console/README.md`. |
| BigCommerce catalogue | all six | |
| BigCommerce orders | ca, com, es | The .nl, .fr and .de tokens carry catalogue scope only and answer 403 on `/v2/orders`. |
| Merchant Center | ca, com | All three accounts are readable. Only two carry offers: `5298296396` (.ca, 11,818 offers, pushed by this repo) and `289630622` (.com, 1,084 offers, autofeed). `670525760` is Gen'C Beauty, a separate brand. The .ca product explorer comes from `google-merchant/dashboard/build_data.py`; the per-account figures come from `merchant.py`. |

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

## Shopping

Three Merchant Center accounts, all readable since 2026-10-21. What they hold is
not what their names suggest:

| Account | Offers | Fed by | Serves |
|---|---|---|---|
| `5298296396` Superhairpieces.ca | 11,818 | this repo's API feed | .ca |
| `289630622` Superhairpieces | 1,084 | **autofeed** - Google crawls the site | .com only |
| `670525760` Gen'C Beauty | 4,333 | autofeed | a separate brand |

Two things the page is careful not to let slide:

- **An account you can read is not a storefront in Shopping.** `289630622` is
  named for .com *and the EU storefronts*, and every offer in it carries feed
  label `US`. **.nl, .fr, .es and .de have no offers in Google Shopping at all** -
  3,323 products with no Shopping presence. Coverage keys on feed labels, not on
  account access, so those four read as missing rather than connected.
- **An autofeed is Google guessing.** It crawls the storefront and keeps what it
  can parse. It is why .com carries 1,084 offers against .ca's 11,818 on a
  *larger* catalogue - while doing 32,772 Shopping clicks a year to .ca's 6,182.

Gen'C Beauty is the worst feed and the easiest fix: 2,741 of its 4,333 offers
(63%) are blocked by one setting, `missing_shipping_mismatch_of_shipping_method_and_offer_currency`
- its offers are priced in USD and CAD but shipping services are defined for only
one of them. Only 29% of that account is fully serving.

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
| .ca Merchant feed | the Shopping dashboard's own build | about two minutes |

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
