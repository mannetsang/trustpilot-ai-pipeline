# Google Ads — reporting dashboard over the Google Ads API

Scripts that read the company's Google Ads accounts through the **Google Ads
API** (GAQL over REST) and build a dashboard showing everything the API
returns: performance, campaigns, keywords, search terms, products, ads and
assets, audiences and geography, conversions, account settings, the change
history, and which reports worked.

| Google Ads account | ID | Type | Service account added? |
|---|---|---|---|
| Super Hair Pieces | `4233688880` | manager | yes, Standard (2026-09-23) |
| Superhairpieces (mainly gencbeauty.com campaigns) | `8654921686` | client, CAD | yes, Standard (2026-09-21); also reached through the manager |
| second manager linked above `8654921686` | `9703293352` | manager | no |

The US and EU storefront accounts are not linked under the manager yet, so
the dashboard covers one account until they are linked (Accounts → + → Link
existing account in the manager) or the service account is added to each.
Account IDs are not secrets.

## Access model

Authentication is Application Default Credentials, exactly like
`google-merchant/`. Since 2026-09-09 Google grants API access to the **Google
Cloud project** that issued the OAuth credentials, so there is **no developer
token**: nothing to apply for, nothing to store in Secret Manager. Project
`shp-ai-bot-2026` already has production access (it queries the live account).

Two identities are involved, and each must be a user on the manager account:

| Runs where | Identity |
|---|---|
| Claude cloud sessions | `claude-sessions@shp-ai-bot-2026.iam.gserviceaccount.com` (added) |
| GitHub Actions | the `client_email` inside the `GCP_SA_KEY` repository secret |

If those are the same account, nothing more is needed. Otherwise add the
GitHub one in the manager account: Admin → Access and security → Security →
Allowed domains, add `shp-ai-bot-2026.iam.gserviceaccount.com`; then Users →
+ → paste the email, Read only is enough. Google Ads matches the allowed
domain exactly, so `gserviceaccount.com` alone is refused. A second admin
approves the request (multi-party approval is on).

## Files

| File | Purpose |
|---|---|
| `ads_api.py` | REST client: ADC token with the `adwords` scope, `searchStream`, retries, `login-customer-id`, row flattening (micros → currency units) |
| `dashboard/build_data.py` | Runs the report catalog (71 GAQL queries) for every client under the manager and writes `dashboard_data.json` |
| `dashboard/template.html` | The dashboard page; inline SVG charts, no build step |
| `dashboard/build_site.py` | Injects the JSON into the template → `site/index.html` (and an artifact copy with `--artifact`) |
| `dashboard/server.py`, `Dockerfile` | Static server for Cloud Run |

## Running

```
python google-ads/dashboard/build_data.py
python google-ads/dashboard/build_site.py
```

Options: `--customer 8654921686` (one client), `--login-customer-id` (another
manager), `--detail-days 30` (window for keywords, products, geo…; default 30
complete days ending yesterday), `--only search_terms keywords` (a few reports
while testing). The build prints one line per report with its row count or
error, and never fails on a single report: failures are recorded and shown in
the page's Coverage section.

In a Claude cloud session use `/opt/gcp-venv/bin/python` (google-auth and
requests are installed there). On your machine:

```
gcloud auth application-default login --scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform
```

then `pip install google-auth requests`. Google may refuse user credentials
issued through gcloud's own OAuth client under the project-based access model;
the GitHub workflow and cloud sessions are the intended way to run the build.

## Daily rebuild

`.github/workflows/google-ads-dashboard.yml` builds the data and page every
morning (05:45 Toronto) and on demand, then deploys `dashboard/` to Cloud Run
as `google-ads-dashboard` (us-central1, unauthenticated like the Merchant
dashboard): <https://google-ads-dashboard-304363458561.us-central1.run.app>. Run it by hand from *Actions → Google Ads dashboard → Run
workflow*; the step summary lists the report coverage.

## What the page shows

- **Overview**: spend, clicks, impressions, CTR, CPC, conversions, cost per
  conversion and value with a 7/14/30/90-day range and comparison; daily and
  monthly trends; clicks by hour and weekday, by network and click type.
- **Campaigns**: a card per campaign with its spend trend for the chosen
  range, then every campaign in a table with type, status, bidding, budget,
  budget use and results. Budgets and impression share tables.
- **Campaign explorer**: pick one campaign (or click it anywhere above) and
  see everything about it in place: settings and targeting, results with a
  comparison to the previous period, daily spend and conversions, its own
  hour-by-weekday pattern, conversions by action, network, device, region,
  age and gender, then only the tables that apply to that campaign type: ad
  groups, keywords, search terms, Performance Max search categories,
  products, asset groups, ads and their assets, extensions, landing pages,
  placements, audiences, impression share, Google's budget forecast,
  recommendations and its change history.
- **Search**: search terms (top 2,000 by impressions), keywords with quality
  score, ad groups, Performance Max search categories, ad positions,
  negative keywords.
- **Shopping**: products (top 2,500 by spend), brands, Google product
  categories, Shopping product groups, Performance Max product filters.
- **Ads and assets**: ads with strength and policy status (click for
  headlines, descriptions, URLs), responsive search ad asset ratings,
  Performance Max asset groups and their assets, extensions, landing pages,
  placements.
- **Audience and geography**: devices, cities, regions, user location versus
  targeted area, distance from the store, age, gender, audience lists,
  per-campaign targeting.
- **Conversions**: actions and their settings, counts and value by action,
  daily trend of the top action, conversion goals, offline imports.
- **Account**: settings, users and roles, recommendations, auto-apply
  subscriptions, linked Merchant Center accounts, asset sets, exclusions.
- **Change history**: who changed what in the last 28 days, from which tool.
- **Coverage**: every query, its row count, timing, status and GAQL.

Known gap: auction insights (`metrics.auction_insight_*`) are only served to
allowlisted developers, so that report shows as restricted. Data is limited
to what Google exposes: Performance Max reports no demographics, keywords or
per-asset metrics, and product rows are aggregated by product id.

## Troubleshooting

| Error | Meaning | Fix |
|---|---|---|
| `NOT_ADS_USER` (401) | The identity is not a user on any Google Ads account | Add it under Access and security (allowed domain first) |
| `USER_PERMISSION_DENIED` (403) | The identity cannot see that customer | Add it to the manager account, or pass the right `--login-customer-id` |
| `METRIC_ACCESS_DENIED` | Metric restricted to allowlisted developers | Nothing to do; the Coverage table marks it restricted |
| `UNRECOGNIZED_FIELD` | A field renamed or removed in a new API version | Fix the query in `build_data.py`; bump `API_VERSION` in `ads_api.py` after checking the release notes |
