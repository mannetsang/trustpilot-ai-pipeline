# Google Search Console

What Google's *organic* search results do for the six storefronts: impressions,
clicks, the queries behind them, and whether the sitemaps are being read. The
Merchant API next door covers Shopping; this covers everything else on Google.

The data feeds the **Organic search** tab of the Shopping dashboard
(`google-merchant/dashboard/`), rebuilt nightly by the *Google Merchant Center
product sync* workflow, and `collect.py` also runs on its own.

```
python google-search-console/collect.py                      # all six, 16 months, summary
python google-search-console/collect.py --property ca --days 90 --print
python google-search-console/collect.py --out search.json
```

## Authentication

Search Console does not use Application Default Credentials the way the
Merchant API does, because **access is granted per property to a specific
user** — there is no project-level or organisation-wide role. Whatever
identity the job runs as has to be added to all six properties.

`SearchConsole.connect()` tries three routes, in this order:

| Route | How it is supplied | When to use it |
|---|---|---|
| Pasted access token | `GOOGLE_SEARCH_CONSOLE_ACCESS_TOKEN` in the environment | A one-off look. Mint it in the OAuth 2.0 Playground with the `webmasters.readonly` scope; it lasts an hour and stores nothing. |
| User OAuth refresh token | `google-search-console-refresh-token` in Secret Manager (`GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN` locally) | Automation under a person's Google account. The client id and secret fall back to the `google-business-profile-*` pair, since minting against that OAuth client is fine. |
| Service account (ADC) | Nothing to store | Automation. `claude-sessions@shp-ai-bot-2026.iam.gserviceaccount.com` already authenticates the rest of the pipeline. |

### Granting the service account (the tidy option)

In Search Console, **once per property**: Settings → Users and permissions →
Add user → paste
`claude-sessions@shp-ai-bot-2026.iam.gserviceaccount.com` → permission
**Full**. Nothing goes into Secret Manager and the nightly job picks it up on
its next run.

Until that is done the credential sees nothing, and `sites.list` says so in the
quietest possible way:

```
$ python google-search-console/collect.py --property ca
search console: service account (ADC)
credential sees 0 properties
  ca   —  not visible to this credential
```

An empty `{}` rather than a 403 is the API's answer to "you have no
properties", so a build that treats silence as success will happily report
zeroes forever. `collect.py` records the reason per property instead, and the
dashboard tab shows it.

### The trap

**Do not reuse the `google-business-profile-refresh-token`.** It was granted
`https://www.googleapis.com/auth/business.manage` and nothing else. The token
endpoint still returns 200 and a valid-looking access token, and then every
Search Console call fails `403 insufficientPermissions` — the first step
succeeding is what makes it hard to diagnose. `mint_access_token()` reads the
scope the token endpoint echoes back and refuses a token without `webmasters`,
so the failure arrives at the door with an explanation.

Minting a *new* refresh token against that same OAuth client is fine, and
leaves the Business Profile token working: Google allows many refresh tokens
per user and client.

## What gets collected

Per property, over two windows — the full 16 months and the last 28 days:

| | |
|---|---|
| Totals | clicks, impressions, CTR, average position, plus the same with `dataState=all` so the last two or three days are visible |
| Daily series | up to 480 days, the API's full retention |
| Every dimension | `query`, `page`, `country`, `device`, `searchAppearance` |
| Every search type | `web`, `image`, `video`, `news`, `discover`, `googleNews` |
| Sitemaps | type, last read, URLs submitted and indexed, errors, warnings |

`surface.py` holds the rest of the API's vocabulary — data states, aggregation
types, filter operators, the URL-inspection fields and the documented limits —
which the tab renders beside the live figures, so the page shows what *could*
be pulled as well as what was.

Queries and pages are capped (250 for the full window, 150 for the recent one)
to keep the dashboard payload reasonable; everything else is collected whole,
because knowing which of a dimension's possible values have no data is the
point.

## Things that will bite

- **The property string is exact and goes in the URL path.**
  `https://superhairpieces.ca/` with the trailing slash, and `.com` is the
  **www** host. It must be percent-encoded with nothing left safe
  (`https%3A%2F%2Fsuperhairpieces.ca%2F`); an un-encoded value gives 404, not
  an empty result. `config.Property` and `search_console.encode_site()` handle
  both. A property verified as a Domain property answers to
  `sc-domain:superhairpieces.ca` instead, which `collect.py` falls back to.
- **16 months is the whole history.** Anything older is gone, so the dashboard
  is the archive if we ever want a longer series.
- **Rare queries are withheld.** Per-query rows add up to less than the
  property total, by design. Do not "reconcile" them.
- **`searchAppearance` cannot be combined with another dimension**, which is
  why it is queried on its own.
- **25,000 rows per call**, paged with `startRow`; a short page means the end.
- **URL inspection is 2,000 calls a day per property**, one URL per call, so it
  is a spot check and not a crawl. `search_console.inspect()` is there for it;
  nothing calls it nightly.

## Companion files

`Google Search Console API.postman_collection.json` at the repo root drives the
same endpoints by hand, with the same percent-encoding gotcha documented. The
Postman environment lists all six properties.
