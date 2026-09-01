# Credentials

All credentials for this repo live in **GCP Secret Manager**, on project
`shp-ai-bot-2026`. Nothing sensitive goes in the repo, in a GitHub Actions
*variable*, or in a Claude cloud environment's *environment variables* box —
those are all plaintext and readable by anyone with access.

`GCP_SA_KEY` in GitHub repository secrets is the one exception, and the only
secret GitHub needs: it's the key that lets workflows read everything else.

## How scripts get credentials

Every script resolves credentials through [`lib/secrets.py`](../lib/secrets.py):

1. **An environment variable**, if the caller names one. This reads a
   gitignored `.env`, so you can run anything locally without touching GCP.
2. **Secret Manager**, via Application Default Credentials — the `GCP_SA_KEY`
   service account in GitHub Actions, the service identity on Cloud Run, or
   `gcloud auth application-default login` on your machine.

```python
from lib.secrets import get_secret

token = get_secret("bigcommerce-access-token", env_var="BC_ACCESS_TOKEN")
```

The Secret Manager client is imported lazily, so scripts resolving everything
from the environment need no third-party packages.

## Naming convention

`<service>-<credential>`, lowercase and hyphenated:

| Secret | Used by |
|---|---|
| `bigcommerce-store-hash` | `bigcommerce-reports/revenue_by_payment_method.py` |
| `bigcommerce-access-token` | same |

## One-time setup

Run once for the project:

```bash
gcloud services enable secretmanager.googleapis.com --project=shp-ai-bot-2026
```

## Adding a credential

Do this yourself — a credential should never be pasted into a chat, a ticket,
or a file in this repo.

```bash
PROJECT=shp-ai-bot-2026
NAME=bigcommerce-access-token

gcloud secrets create "$NAME" --replication-policy=automatic --project="$PROJECT"

# printf, not echo: echo appends a newline that becomes part of the secret.
printf '%s' 'PASTE_THE_VALUE_HERE' \
  | gcloud secrets versions add "$NAME" --data-file=- --project="$PROJECT"
```

Then grant the CI service account read access to **that secret only** — per
secret, not project-wide, so a leaked key can't read everything:

```bash
SA=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['client_email'])" /path/to/gcp-sa-key.json)

gcloud secrets add-iam-policy-binding "$NAME" \
  --member="serviceAccount:$SA" \
  --role="roles/secretmanager.secretAccessor" \
  --project="$PROJECT"
```

`$SA` is the `client_email` from the same JSON key stored as `GCP_SA_KEY`.

## Rotating a credential

Add a new version; scripts read `latest`, so they pick it up on the next run
with no code change.

```bash
printf '%s' 'NEW_VALUE' \
  | gcloud secrets versions add bigcommerce-access-token --data-file=- --project=shp-ai-bot-2026
```

Then disable the old version once you've confirmed the new one works:

```bash
gcloud secrets versions disable 1 --secret=bigcommerce-access-token --project=shp-ai-bot-2026
```

## Local development

Put values in a gitignored `.env` at the repo root, using the environment
variable names each script documents:

```
BC_STORE_HASH=gmosz3ja
BC_ACCESS_TOKEN=...
```

`lib/secrets.py` walks up from the script to find it. Real environment
variables always win over `.env`.

## What not to do

- Don't paste credentials into a Claude session, a cloud environment's
  variables box, or a GitHub Actions *variable*. All three are readable by
  anyone with access, and session content is retained in transcripts.
- Don't grant `roles/secretmanager.secretAccessor` at the project level.
- Don't commit a `.env`, a service-account JSON, or a `.pem`.
