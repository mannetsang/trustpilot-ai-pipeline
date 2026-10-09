# AP credit-card reconciliation

Accounts payable (`ap@superhairpieces.com`) uploads a credit-card statement or
an RBC **Transaction Export** (one `.xlsx`/`.csv` with every card of the
account). The service in [`cloud_run/`](cloud_run/) stores each line once,
reads every invoice filed in the AP Invoice folder on Drive with Gemini,
pairs purchases with invoices, posts an *invoices needed* card to each credit
card's own Google Chat space, and serves a dashboard, organised by year and
month, where card owners record the cost centre and usage of each charge and
link or waive invoices.

Storage is a **dedicated Supabase project** (Postgres, schema in
[`schema.sql`](schema.sql)), separate from every other Supabase project the
company runs, reached from the server only with the service-role key. The
Cloud Run service is `ap-reconciliation` in `us-central1`.

## The Invoice folder

Invoices and receipts live in `ap@superhairpieces.com`'s My Drive, folder
**Invoice** (id `18YkGTNDIrxdQgHRgquvQizI8fLsFOpPk`, env `INVOICE_FOLDER_ID`),
laid out as

```
Invoice/
  2026 09/
    Canada Post/      invoice-2026-09-04.pdf
    Amazon/           order-112-...pdf  receipt.jpg
    Google Cloud/     ...
    stray-receipt.pdf           (allowed, no vendor hint)
  2026 10/
    ...
```

- The month folder is `YYYY MM` (a space, zero-padded month; `2026-09` and
  `2026_9` are accepted too). Anything not shaped like a month is skipped
  unless it sits below a month folder.
- The first folder level below the month is the **vendor folder**. Its name
  travels with every file as a matching hint, so a receipt under
  `2026 09/Amazon` is treated as an Amazon charge from around
  September even when the document itself is hard to read.
- PDF, PNG, JPEG, WebP, GIF, HEIC and plain-text files are read; Google
  Docs, Sheets and Slides are exported as PDF first; shortcuts are followed.
  Word, Excel, zip and e-mail files are **not** read: save them as PDF.
  Files over 15 MB are skipped.
- The service reads the folder as the AP user, with the user OAuth refresh
  token in Secret Manager `google-workspace-ap-*` (the Business Profile
  scripts use the same mechanism). When those three values are missing it
  falls back to Application Default Credentials, which only works if the
  folder is shared with the runtime service account.

A file is read **once**: the result is stored by Drive file id and the file
is only re-read when its `modifiedTime` changes. Moving or renaming a file
updates its folder hints without another model call.

## Pipeline

1. **Upload** (`POST /api/statements`). `.xlsx`, `.xlsm`, `.csv` and `.tsv`
   exports with a recognisable header row (`Transaction date`, `Supplier`,
   `Amount`, `Account ****-****-****-1610`, `Cardholder first/last name`,
   `Source currency/amount`, `Billing currency`...) are parsed directly, no
   AI involved. PDF statements, photos and spreadsheets whose columns are not
   recognised go to Gemini with a fixed output schema. Every line gets a
   deterministic id (card, dates, normalised description, amount, currency,
   plus a counter for identical lines in one file), so uploading the same
   export twice, or an overlapping one, adds nothing. Cards seen for the
   first time are created in the `cards` table; credits, payments and fees
   are stored with status `not_required`. With `STATEMENTS_FOLDER_ID` set a
   copy of the upload is kept on Drive under the month's name.
2. **Index** (`POST /api/index`). Walks `Invoice/<month-1>`, `<month>` and
   `<month+1>`, downloads each new or changed file and asks Gemini for the
   vendor, invoice number, date, total actually charged, currency, printed
   card digits and a one-line summary. `INDEX_BATCH` (30) files per call on
   `INDEX_WORKERS` (4) threads; the answer's `remaining` count tells the
   dashboard to call again until it is 0.
3. **Match** (`POST /api/match`). Rules first, Gemini judge for the rest;
   see the next section.
4. **Notify** (`POST /api/notify`). One Chat card per credit card that still
   has purchases without an invoice, listing the missing purchases, the
   possible matches to confirm and the number of charges without a cost
   centre, with *Open dashboard* (deep link to the card's month) and
   *Invoice folder* buttons. Every post is logged in `notifications`.
5. **Weekly reminder** (`POST /maintenance/remind`, Cloud Scheduler, Monday
   14:00 UTC). For each card, the newest month that still has missing or
   possible purchases is re-indexed, re-matched and notified again.

`POST /api/statements?sync=1` runs steps 1-4 in one request (the CLI, curl
and the tests use it); the dashboard's *Upload and process* button runs them
as separate calls so it can show progress. Jobs serialize on an in-process
lock: a second index, match, notify or synchronous upload gets `409 busy`
while one runs.

## Matching rules and statuses

Every purchase is `matched`, `possible`, `missing` or `waived`; credits,
payments and fees are `not_required`. Matching only ever touches purchases
that are `missing` or `possible`: a manual link, confirmation or waiver is
never undone by a re-run.

**Rule pass.** Each pending purchase is scored against every unused invoice
of the surrounding month folders:

| Signal | Score |
|---|---|
| Amount: invoice total against the billed amount, or against the original foreign amount when the invoice is in that currency | equal (within one cent) 1.0, within 1% 0.6, within 5% 0.3 |
| Vendor: token overlap between the statement description/supplier and the invoice's vendor, vendor folder and file name (`Amzn` = Amazon, `ChatGPT` = OpenAI, `Goog` = Google and a few more aliases) | 0 to 1 |
| Date: invoice date vs transaction date | 3 days 1.0, 7 days 0.8, 14 days 0.5, 35 days 0.2 |
| Card digits printed on the invoice | a different card rules the pair out |

An exact amount with either a vendor overlap or a date within a week is
`matched`; an exact amount alone is `possible`; an amount within 1% with a
strong vendor overlap and a date within 10 days is `possible`. Pairs are
taken best-first and an invoice is linked to at most one transaction.

**Gemini judge.** What is still unmatched (purchases and invoices) goes to
the model in batches with the same instructions a person would get: same
business, same amount (allowing tips, exchange rates or separate tax when
it says so), invoice date within about two weeks, one invoice per charge,
never a match on amount alone. Confidence 0.85 and up is `matched`, 0.6 and
up is `possible`, the rest stay `missing`. The `match_note` on each
transaction says which pass decided and why.

**Manual actions** (the `action` field of `PATCH /api/transactions/<id>`):
`confirm` a possible match, `link` an invoice chosen from the picker,
`unlink`, `waive` (no invoice exists, with a reason) and `unwaive`.

## Roles and sign-in

The page signs the user in with Google Identity Services (the project's
OAuth client, `GOOGLE_OAUTH_CLIENT_ID`) and sends the ID token as
`Authorization: Bearer <token>` on every `/api` call. The server verifies the
token against the client id, requires a verified e-mail in `ALLOWED_DOMAINS`
(`superhairpieces.com`) and derives the role:

| Role | Who | May |
|---|---|---|
| `ap` | the addresses in `AP_EMAILS` (`ap@`, `manne@`) | upload, index, match, notify, edit cards, edit every transaction |
| `member` | anyone else in the domain | read everything; edit only the transactions of cards whose **owner e-mail** (set by AP under *Cards...*) is theirs. `/api/transactions` returns an `editable` flag per row and the page disables the rest |

The service URL is public (`--allow-unauthenticated`) because the browser
must reach `/` and `/api/config` before signing in; nothing else answers
without a valid token. `/api/config` only carries the client id (public by
design) and the dashboard URL. With `AUTH_DISABLED=1` (local runs) no sign-in
is shown and every caller is `ap`.

## Dashboard

- **Sign-in** screen with the Google button; the token lives in the tab's
  session storage, *Sign out* clears it.
- **Months** down the left (newest first), each with its spend and badges for
  missing and possible invoices; the URL hash is `#/YYYY/MM?card=1610`, which
  is what the Chat card buttons open.
- **Tiles** for the month (or the selected card): spend, purchases, invoices
  found, missing, possible, waived, charges without a cost centre.
- **AP panel** (AP only): upload with a *Send Chat reminders after matching*
  checkbox, *Re-run matching*, *Send reminders*, *Cards...* (label, holder,
  owner e-mail, Chat space name, active flag, and whether a webhook is
  configured for the card), plus a log of the running job.
- **Card chips** and a status filter (all, missing, possible, matched,
  waived, no cost centre), a search box over description, amounts, cost
  centre and usage, and a card selector.
- **Transaction table**: date, card, description with city/country, amount
  (with the original foreign amount), cost centre dropdown (the
  `cost_centers` table, with an *Other...* free text), usage, note, and the
  invoice column: the linked file with vendor, number and total, or the
  buttons *Confirm* / *Not this* on a possible match, *Link...* / *No
  invoice* on a missing one, *Undo* on a waived one. Edits save on change.
- **Invoice picker** (from *Link...*): every invoice of the surrounding
  month folders, searchable by vendor, file, folder and amount; ones already
  linked elsewhere are greyed out.
- **Uploads this month** and **Last notifications** lists for AP.

## Chat reminders per card

Each credit card has its own Chat space (the card owner and AP in it), and
each space has an incoming webhook. The webhook URLs are secrets and travel
as one JSON object in `CARD_WEBHOOKS_JSON`:

```json
{"1610": "https://chat.googleapis.com/v1/spaces/AAAA.../messages?key=...&token=...",
 "4894": "https://chat.googleapis.com/v1/spaces/BBBB.../messages?key=...&token=...",
 "default": "https://chat.googleapis.com/v1/spaces/CCCC.../messages?key=...&token=..."}
```

A card without its own entry falls back to `default` (or to
`GCHAT_WEBHOOK_URL`); with neither, the notify result says *no webhook
configured for this card* and nothing is sent. To create a webhook: open the
space in Google Chat, click the space name, **Apps & integrations**,
**Webhooks** (*Add webhooks* / *Manage webhooks*), name it `AP
reconciliation`, save and copy the URL. Cards that are inactive (the flag
under *Cards...*) are never notified.

## Endpoints

| Method and path | Auth | What it does |
|---|---|---|
| `GET /` | none | the dashboard page |
| `GET /healthz` | none | `{"ok": true, "store": ..., "last_job": ...}` |
| `GET /api/config` | none | OAuth client id, `auth_disabled`, dashboard URL, Invoice folder URL, `ai_enabled` |
| `GET /api/me` | signed in | e-mail, name, role, cards owned |
| `GET /api/months` | signed in | month navigation with per-card counts, cards, cost centres |
| `GET /api/transactions?year&month[&card]` | signed in | the month's transactions with their invoice, `editable` flag, uploads and recent notifications |
| `PATCH /api/transactions/<id>` | signed in, AP or the card's owner | body: `cost_center`, `usage`, `note`, and/or `action` (`waive` with `reason`, `unwaive`, `confirm`, `link` with `invoice_id`, `unlink`) |
| `GET /api/invoices?year&month` | signed in | invoices of month-1..month+1 with a `linked` flag (the picker) |
| `GET /api/cards` | signed in | cards |
| `POST /api/cards` | AP | `{"last4", "label", "holder_name", "owner_email", "chat_space", "active"}` |
| `GET /api/statements?year&month` | signed in | uploads that touched the month |
| `POST /api/statements` | AP | multipart `file`; `?sync=1` also indexes, matches and notifies (`&notify=0` to skip the cards) |
| `POST /api/index?year&month[&limit]` | AP | read up to `limit` (max 100) new invoices for month-1..month+1 |
| `POST /api/match?year&month[&card]` | AP | pair purchases with invoices |
| `POST /api/notify?year&month[&card][&dry_run=1]` | AP | post the Chat cards (`dry_run` returns the payloads instead) |
| `POST /maintenance/index[?months=2026 09,2026 10&limit=30]` | `X-Api-Token` | index the whole folder, or the named month folders |
| `POST /maintenance/match?year&month[&card]` | `X-Api-Token` | same as `/api/match` for curl and schedulers |
| `POST /maintenance/remind` | `X-Api-Token` | the weekly reminder described above |

Job endpoints answer `409 {"status": "busy"}` while another job runs and
`500 {"status": "error"}` with the message when one fails.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `STORE` | `supabase` | `memory` keeps everything in-process (tests, demos) |
| `SUPABASE_URL` | | `https://<ref>.supabase.co` (GitHub variable `AP_RECONCILIATION_SUPABASE_URL`) |
| `SUPABASE_SERVICE_KEY` | | service-role key; secret |
| `INVOICE_FOLDER_ID` | the Invoice folder above | root of the walk |
| `STATEMENTS_FOLDER_ID` | empty | when set, uploads are copied to Drive under `<folder>/<YYYY MM>/` |
| `GOOGLE_WORKSPACE_AP_CLIENT_ID` / `_CLIENT_SECRET` / `_REFRESH_TOKEN` | | the AP user's OAuth triple for Drive; secrets. ADC when unset |
| `CARD_WEBHOOKS_JSON` | | the webhook object above; secret |
| `GCHAT_WEBHOOK_URL` | empty | optional single fallback webhook |
| `API_TOKEN` | | value of the `X-Api-Token` header for `/maintenance/*`; secret |
| `GOOGLE_OAUTH_CLIENT_ID` | | Web application client id (GitHub variable `AP_RECONCILIATION_OAUTH_CLIENT_ID`) |
| `ALLOWED_DOMAINS` | `superhairpieces.com` | comma-separated e-mail domains allowed to sign in |
| `AP_EMAILS` | `ap@superhairpieces.com,manne@superhairpieces.com` | addresses with the `ap` role |
| `AUTH_DISABLED` | | `1` skips sign-in (local only) |
| `DASHBOARD_URL` | request origin | absolute URL for the Chat card buttons; the workflow sets it |
| `GCP_PROJECT` / `VERTEX_LOCATION` | `shp-ai-bot-2026` / `us-central1` | where Gemini runs (ADC) |
| `GEMINI_EXTRACT_MODEL` / `GEMINI_MATCH_MODEL` | see `main.py` | the reading and judging models |
| `AI_DISABLED` | | `1` skips every model call: no invoice reading, rules only, no PDF statements |
| `INDEX_BATCH` / `INDEX_WORKERS` | `30` / `4` | files per index call and download threads |

## Secrets

All on `shp-ai-bot-2026`. The runtime identity is the project's default
compute service account, `304363458561-compute@developer.gserviceaccount.com`,
which must hold `roles/secretmanager.secretAccessor` on **each** of these
(per secret, never project-wide; the deploy fails with *Permission denied on
secret* otherwise):

| Secret Manager id | Container env var | Notes |
|---|---|---|
| `ap-reconciliation-supabase-service-key` | `SUPABASE_SERVICE_KEY` | service-role key of the AP Supabase project; bypasses RLS, server side only |
| `google-workspace-ap-client-id` | `GOOGLE_WORKSPACE_AP_CLIENT_ID` | already exists: the OAuth client that issued the AP user's refresh token |
| `google-workspace-ap-client-secret` | `GOOGLE_WORKSPACE_AP_CLIENT_SECRET` | already exists; the three stay together |
| `google-workspace-ap-refresh-token` | `GOOGLE_WORKSPACE_AP_REFRESH_TOKEN` | already exists: `ap@superhairpieces.com`'s refresh token with the Drive scope |
| `ap-reconciliation-card-webhooks` | `CARD_WEBHOOKS_JSON` | the JSON object of Chat webhooks |
| `ap-reconciliation-api-token` | `API_TOKEN` | random string; also pasted into the scheduler job's header |
| `SUPABASE_ACCESS_TOKEN` | (not mounted) | Supabase personal access token for `setup_db.py` only; optional, a local `.env` entry does the same |

Gemini (Vertex AI) runs as the compute service account through Application
Default Credentials, as it does for `gbp-reviews`; no secret is involved.
Local `.env` names are the env vars above.

## One-time setup

Commands are for Windows `cmd` (one line each, `python` not `python3`).
Secret values go through a file written with Notepad, which adds no trailing
newline (a newline would end up inside the token), and the file is deleted
right after. Never paste a value into a chat, a ticket or this repo.

1. **Supabase project.** At <https://supabase.com/dashboard/projects> create a
   new project named `ap-reconciliation` in the company organisation, region
   *East US (North Virginia)* (closest to `us-central1`); generate a database
   password and keep it in the password manager (nothing here uses it). Note
   the **project ref** (Project Settings, General) and the URL
   `https://<ref>.supabase.co`.
2. **Schema.** Create a personal access token (avatar, *Account*, *Access
   Tokens*, name `setup_db`), put it in the gitignored root `.env` as
   `SUPABASE_ACCESS_TOKEN=...`, then run
   `python ap-reconciliation\setup_db.py --project-ref <ref>`
   and expect `ok: applied 18 statements from schema.sql to project <ref>`.
   The Table Editor now shows `cards`, `statements`, `invoices`,
   `transactions`, `notifications`, `cost_centers` and the `month_summary`
   view. Delete the token from `.env` (or keep it for future schema changes).
3. **Service-role key.** Project Settings, *API Keys*, reveal `service_role`,
   paste it into Notepad and save as `C:\temp\sb.txt`. Then:
   `gcloud secrets create ap-reconciliation-supabase-service-key --replication-policy=automatic --project=shp-ai-bot-2026`
   `gcloud secrets versions add ap-reconciliation-supabase-service-key --data-file=C:\temp\sb.txt --project=shp-ai-bot-2026`
   `del C:\temp\sb.txt`
   and the project URL as a GitHub Actions **variable** (not a secret, it is
   public):
   `gh variable set AP_RECONCILIATION_SUPABASE_URL --body "https://<ref>.supabase.co" --repo mannetsang/trustpilot-ai-pipeline`
4. **Chat spaces and webhooks.** For each credit card create a space (card
   owner + AP, e.g. `AP - Visa 1610`), add an incoming webhook as described
   above and collect the URLs in Notepad as the JSON object shown above,
   saved as `C:\temp\hooks.json`. The secret already exists with a
   placeholder version `{}` (created 2026-10-09, so the first deploy works
   before any space exists); add the real object as a new version, and
   again whenever a card is added:
   `gcloud secrets versions add ap-reconciliation-card-webhooks --data-file=C:\temp\hooks.json --project=shp-ai-bot-2026`
   `del C:\temp\hooks.json`
5. **API token.** Already created on 2026-10-09 (`ap-reconciliation-api-token`,
   a random 43-character value nobody needs to remember). To rotate it:
   `python -c "import secrets; open(r'C:\temp\token.txt', 'w').write(secrets.token_urlsafe(32))"`
   `gcloud secrets versions add ap-reconciliation-api-token --data-file=C:\temp\token.txt --project=shp-ai-bot-2026`
   `del C:\temp\token.txt`
   then recreate the scheduler job (step 9).
6. **IAM grants.** One per secret. The webhooks and API token secrets
   were granted when they were created; the Supabase key and the three AP
   Drive secrets (which pre-date this service) still need it:
   `gcloud secrets add-iam-policy-binding ap-reconciliation-supabase-service-key --member="serviceAccount:304363458561-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor" --project=shp-ai-bot-2026`
   `gcloud secrets add-iam-policy-binding google-workspace-ap-client-id --member="serviceAccount:304363458561-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor" --project=shp-ai-bot-2026`
   `gcloud secrets add-iam-policy-binding google-workspace-ap-client-secret --member="serviceAccount:304363458561-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor" --project=shp-ai-bot-2026`
   `gcloud secrets add-iam-policy-binding google-workspace-ap-refresh-token --member="serviceAccount:304363458561-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor" --project=shp-ai-bot-2026`
   The compute account normally carries Editor, which covers Vertex AI; if
   that was trimmed, add `roles/aiplatform.user` on the project.
7. **First deploy.** Push to `main` or run *Deploy AP credit-card
   reconciliation service* from the Actions tab. Sign-in does not work yet
   (no client id); read the service URL from the *Point DASHBOARD_URL* step
   or with
   `gcloud run services describe ap-reconciliation --region us-central1 --project shp-ai-bot-2026 --format "value(status.url)"`
8. **Google OAuth client.** In the Cloud console, project `shp-ai-bot-2026`,
   *APIs & Services*, *OAuth consent screen*: if none is configured yet, app
   name `AP Reconciliation`, the AP support e-mail, user type *Internal*
   when the project sits in the Workspace organisation (otherwise *External*;
   the server's domain check still keeps outsiders out). Then *Credentials*,
   *Create credentials*, *OAuth client ID*, type **Web application**, name
   `AP Reconciliation dashboard`, **Authorised JavaScript origins**: the
   service URL from step 7 (scheme and host only, no path or trailing
   slash), plus `http://localhost:8765` and `http://127.0.0.1:8765` for local
   runs with sign-in. No redirect URI is needed. Copy the client id (ends in
   `.apps.googleusercontent.com`; the client secret is never used) into the
   GitHub variable and redeploy:
   `gh variable set AP_RECONCILIATION_OAUTH_CLIENT_ID --body "<client id>" --repo mannetsang/trustpilot-ai-pipeline`
   `gh workflow run deploy-ap-reconciliation.yml --repo mannetsang/trustpilot-ai-pipeline`
9. **Scheduler job**, created once by an owner because the job carries the
   token in its header and Cloud Scheduler cannot read Secret Manager (the
   workflow only refreshes the schedule and URI of an existing job). The
   `for /f` reads the token from Secret Manager so it never touches the
   clipboard; replace the URI with the service URL from step 7:
   `for /f "delims=" %t in ('gcloud secrets versions access latest --secret=ap-reconciliation-api-token --project=shp-ai-bot-2026') do gcloud scheduler jobs create http ap-reconciliation-remind --location us-central1 --project shp-ai-bot-2026 --schedule "0 14 * * 1" --time-zone Etc/UTC --uri "https://ap-reconciliation-304363458561.us-central1.run.app/maintenance/remind" --http-method POST --headers "X-Api-Token=%t" --attempt-deadline 1800s`
   Test it with
   `gcloud scheduler jobs run ap-reconciliation-remind --location us-central1 --project shp-ai-bot-2026`
   and check the service logs. When the token is rotated, delete and
   recreate the job the same way.
10. **Cards.** Sign in as AP, upload the first export, then under *Cards...*
    give each card its label, owner e-mail (the person who may code its
    charges) and Chat space name. The *Webhook* column shows whether the
    card has an entry in `CARD_WEBHOOKS_JSON`.

## Local runs

Install the runtime once: `pip install -r ap-reconciliation\cloud_run\requirements.txt`.
Everything reads the gitignored root `.env` through `lib/secrets.py`, so no
variable has to be exported by hand.

**Demo without a database or Google.** From `ap-reconciliation\cloud_run`:

```
set STORE=memory&& set AUTH_DISABLED=1&& set AI_DISABLED=1&& python main.py serve --port 8765
```

(bash: `STORE=memory AUTH_DISABLED=1 AI_DISABLED=1 python main.py serve --port 8765`).
Then upload an export and open <http://127.0.0.1:8765>:

```
curl -F "file=@C:\path\to\export.xlsx" "http://127.0.0.1:8765/api/statements"
curl "http://127.0.0.1:8765/api/months"
curl "http://127.0.0.1:8765/api/transactions?year=2026&month=9"
```

Every row is `missing` (no invoices are read with `AI_DISABLED`), every row
is editable, and the cost centre, usage, waive and link actions all work
against memory. Restarting the server forgets everything.

**Dry-run CLI.** Parses a file, indexes the real Invoice folder, matches and
prints the Chat payloads, all on an in-memory store; nothing is posted and
nothing is written to Supabase. Needs the `GOOGLE_WORKSPACE_AP_*` triple in
`.env` (or ADC with the folder shared) and ADC for Gemini
(`gcloud auth application-default login`):

```
python main.py parse C:\path\to\export.xlsx
python main.py dry-run C:\path\to\export.xlsx
python main.py dry-run C:\path\to\export.xlsx --no-ai
```

**Against the real store.** Put `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, the
`GOOGLE_WORKSPACE_AP_*` triple, `DASHBOARD_URL` and (only if you mean to
post) `CARD_WEBHOOKS_JSON` in `.env`, then:

```
python main.py index --months "2026 09,2026 10"
python main.py match --year 2026 --month 9
python main.py notify --year 2026 --month 9 --dry-run
python main.py serve --port 8765
```

`serve` without `AUTH_DISABLED` shows the real sign-in, which works locally
once `http://localhost:8765` is an authorised origin of the OAuth client and
`GOOGLE_OAUTH_CLIENT_ID` is in `.env`.

**Tests** (standard library, no network): from `ap-reconciliation`,
`python -m unittest discover -s tests -v`.

## Deployment

[`.github/workflows/deploy-ap-reconciliation.yml`](../.github/workflows/deploy-ap-reconciliation.yml)
runs on every push to `main` touching `cloud_run/`, signs in to Google Cloud
through Workload Identity Federation (`docs/credentials.md`), deploys with
`--source` (one instance, 1 GiB, 30-minute request timeout), mounts the
secrets above, then reads the service URL and sets `DASHBOARD_URL` to it so
the Chat card buttons point at the right place. A last step refreshes the
scheduler job's schedule and URI and only warns when the job is missing or
when `claude-sessions` may not touch it.

Secrets mounted as environment variables are read when an instance starts,
so a new secret version (a rotated key, an added card webhook) reaches the
service on the next instance; with `--min-instances 0` that is after the
next idle period, or at once with *Run workflow*.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `409 {"status": "busy"}` from an upload, index, match or notify | one job at a time per instance; wait for it (`/healthz` shows `last_job`) and retry. The dashboard retries by itself |
| Invoice listed in the picker with *unsupported file type* | Word, Excel, zip or e-mail files are not read; save as PDF into the same folder. Files over 15 MB likewise. Such files are not retried; any other extraction error is retried on the next index |
| Index keeps reporting the same `remaining` count | the files that fail are listed in the job log (`errors`); fix or remove them. The dashboard stops after two stalled rounds |
| *AP token refresh failed ... invalid_grant* | the AP user's refresh token was revoked (password change, app removed under the account's third-party access, token unused for six months while the OAuth app is in *testing*). Mint a new one with the same client (OAuth Playground, scope `https://www.googleapis.com/auth/drive`, signed in as `ap@`), add it as a new version of `google-workspace-ap-refresh-token`, redeploy |
| *Google Drive error ... 404* on the Invoice folder | the folder id is wrong, or the service is on ADC and the folder is not shared with the compute service account |
| `database error: GET transactions -> 401` (PostgREST) | `SUPABASE_SERVICE_KEY` is wrong: the anon key instead of `service_role`, or the key was rotated in Supabase. Add the right one as a new secret version and redeploy |
| `database error: ... 404 ... relation "..." does not exist` | the schema is not applied to this project: `setup_db.py --project-ref <ref>` |
| `database error: ... 5xx` or connection errors for hours | the Supabase project is paused (free-tier projects pause after a week idle). Restore it from the dashboard; consider the paid tier |
| Sign-in button missing, or *token rejected* / 401 right after signing in | `GOOGLE_OAUTH_CLIENT_ID` is empty or belongs to a different client than the one the page used; or the browser shows *The given origin is not allowed for the given client ID*: add the service URL to the client's authorised JavaScript origins (changes take a few minutes) |
| 401 *sign in with your superhairpieces.com Google account* for a real colleague | the account is outside `ALLOWED_DOMAINS`, or its e-mail is not verified by Google |
| Notify says *no webhook configured for this card* | add the card's `last4` (or a `default`) to `CARD_WEBHOOKS_JSON`, new secret version, redeploy |
| Notify says *chat webhook returned 404/403* | the webhook was deleted in the space or the space was archived; create a new one and update the secret |
| Deploy fails with *Permission denied on secret* or *secret not found* | step 6 (grant) or steps 3-5 (create) were skipped for that secret |
| Scheduler run returns 401 | the token in the job's header differs from `ap-reconciliation-api-token` (rotated since the job was created): delete and recreate the job (step 9) |
| Upload reports `new: 0` | the same lines are already stored (ids are deterministic); nothing is lost. Legacy `.xls` is refused: save as `.xlsx` |
| Everything stays `missing` although invoices are filed | the invoices are not indexed yet (`AI_DISABLED`, or `remaining` > 0), are filed under a month folder outside month-1..month+1, or have no readable total (`extraction_error` in the picker) |
| A Chat card's *Open dashboard* button opens the wrong host | `DASHBOARD_URL` was not set (the workflow's second step failed); set it with `gcloud run services update ap-reconciliation --region us-central1 --project shp-ai-bot-2026 --update-env-vars DASHBOARD_URL=<service url>` |
