# Credentials

All credentials for this repo live in **GCP Secret Manager**, on project
`shp-ai-bot-2026`. Nothing sensitive goes in the repo, in a GitHub Actions
*variable*, or in a Claude cloud environment's *environment variables* box —
those are all plaintext and readable by anyone with access.

GitHub Actions needs **no secret at all** to reach Google Cloud: it uses
Workload Identity Federation (below). The old `GCP_SA_KEY` JSON-key secret is
retired.

## GitHub Actions: Workload Identity Federation (since 2026-09-29)

Each job asks GitHub for a short-lived OIDC token and Google's STS swaps it
for credentials of `claude-sessions@shp-ai-bot-2026.iam.gserviceaccount.com`.
Nothing long-lived is stored in GitHub.

```yaml
permissions:
  contents: read
  id-token: write   # required: lets the job request the OIDC token

steps:
  - uses: google-github-actions/auth@v2
    with:
      workload_identity_provider: projects/304363458561/locations/global/workloadIdentityPools/github-actions/providers/github
      service_account: claude-sessions@shp-ai-bot-2026.iam.gserviceaccount.com
```

Who may use it — three locks, all must hold:

| Lock | Where | Value |
|---|---|---|
| Owner | provider attribute condition | `assertion.repository_owner_id == '213646871'` (mannetsang — the numeric id, so a renamed or re-registered account name can't match) |
| Branch | provider attribute condition | `assertion.ref == 'refs/heads/main'` — pushes, schedules and manual runs on `main` only; pull-request and feature-branch runs get no Google credentials |
| Repository | `roles/iam.workloadIdentityUser` on the service account | `principalSet://…/workloadIdentityPools/github-actions/attribute.repository/<owner>/<repo>` for `mannetsang/trustpilot-ai-pipeline` and `mannetsang/genc-sales-dashboard` |

To let another repository use it:

```bash
gcloud iam service-accounts add-iam-policy-binding claude-sessions@shp-ai-bot-2026.iam.gserviceaccount.com \
  --project=shp-ai-bot-2026 --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/304363458561/locations/global/workloadIdentityPools/github-actions/attribute.repository/mannetsang/<repo>"
```

The permissions are the service account's own — the same account the old key
belonged to (the audit log shows every key-based deploy as claude-sessions), so
federation changed how workflows sign in, not what they may do.

## How scripts get credentials

Every script resolves credentials through [`lib/secrets.py`](../lib/secrets.py):

1. **An environment variable**, if the caller names one. This reads a
   gitignored `.env`, so you can run anything locally without touching GCP.
2. **Secret Manager**, via Application Default Credentials — claude-sessions
   through federation in GitHub Actions, the service identity on Cloud Run, or
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
| `BIGCOMMERCE_gmosz3ja_ACCESS_TOKEN` | `bigcommerce-reports/revenue_by_payment_method.py` |
| `google-business-profile-client-id` | `google-business/gbp_api.py` (env `GOOGLE_BUSINESS_PROFILE_CLIENT_ID`) |
| `google-business-profile-client-secret` | `google-business/gbp_api.py` (env `GOOGLE_BUSINESS_PROFILE_CLIENT_SECRET`) |
| `google-business-profile-refresh-token` | `google-business/gbp_api.py` (env `GOOGLE_BUSINESS_PROFILE_REFRESH_TOKEN`) — user OAuth token for the listings' manager, issued by a developer-owned OAuth client; the three must stay together |
| `shp-ats-supabase-secret-key` | `hr-ats/` dashboard + both jobs (env `SUPABASE_SECRET_KEY`) — Supabase `shp-ats` secret key `hr_ats_services` |
| `hr-ats-pipeline-gmail-token` | `hr-ats/pipeline` (env `PIPELINE_OAUTH_JSON`) — base64 OAuth token JSON for office@, gmail.modify + drive |
| `jobs-dashboard-gmail-token` | `hr-ats/dashboard` (env `GMAIL_TOKEN_JSON_B64`) — base64 OAuth token JSON for office@, gmail.modify + Chat |
| `jobs-dashboard-password` | `hr-ats/dashboard` (env `DASHBOARD_PASSWORD`) |
| `hr-ats-maps-api-key` | `hr-ats/pipeline/offer_packet.py` (env `MAPS_API_KEY`) |

Only genuine secrets belong here. The BigCommerce store hash identifies the
store in a URL path and is not sensitive, so it is set inline in the workflow
rather than taking up a secret.

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

`$SA` is `claude-sessions@shp-ai-bot-2026.iam.gserviceaccount.com`, the account
GitHub Actions signs in as.

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
