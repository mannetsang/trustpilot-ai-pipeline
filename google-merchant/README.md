# Google Merchant Center — Merchant API

Scripts that drive Merchant Center through the **Merchant API** (the successor
to the Content API for Shopping): register the GCP project, create one product
data source per storefront, and push products into them.

| Merchant Center account | ID |
|---|---|
| Superhairpieces | `289630622` |
| Gen'C Beauty | `670525760` |

Account IDs are not secrets. Neither is anything else here: the scripts
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
   python google-merchant/register_developer.py --account 289630622 --email manne@superhairpieces.com
   ```

   Google says to allow about five minutes before the next call; verify with
   `--check`.

4. **Create the data sources**:

   ```
   python google-merchant/data_sources.py create --account 289630622
   python google-merchant/data_sources.py list --account 289630622
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
python google-merchant/products.py insert --account 289630622 --label CA product.json
python google-merchant/products.py delete --account 289630622 --label CA --offer-id SHP-TEST-001
```

`product.json` is the Merchant API `ProductAttributes` object plus a
top-level `offerId`; `products.py --help` shows a minimal example. Inserting
an offer id that already exists in that data source replaces it. Products
show up in Merchant Center under *Products* within a few minutes and go
through the normal review.

A scheduled sync from the BigCommerce catalogue into these data sources is the
natural next step and is **not** included here.

## Local runs

Set the account once in the gitignored `.env` at the repo root so `--account`
can be dropped:

```
GMC_ACCOUNT_ID=289630622
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
