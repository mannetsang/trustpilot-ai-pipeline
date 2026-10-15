# GenC Consumption

A dashboard of what GenC Beauty takes out of its own stock for office use (videos,
samples, customers, other departments) and what it cost, with a form to log new
lines. Live since 2026-10-15 at
<https://genc-consumption-eqr4wko2oa-nn.a.run.app> (Google sign-in,
`superhairpieces.com` accounts only).

| | |
|---|---|
| Google Cloud project | `genc-consumption-2026` (number `83164121214`), billing `014EEF-576861-01E7C3` |
| Cloud Run service | `genc-consumption`, `northamerica-northeast1`, behind Identity-Aware Proxy |
| Runtime identity | `consumption-dashboard@genc-consumption-2026.iam.gserviceaccount.com` |
| Supabase project | **GenC Consumption**, ref `zitrqrchbdracjvxbwtn`, `ca-central-1`, GENC Beauty organisation, used by nothing else |
| Google Sheet | **office use** (`1fc2OkX2f9jfMlYxHSPuvyFP2p13JygwrguWxJQkeNdE`, owner `anais@`), tabs `明细`, `Note Ref`, `Cost Ref` |

## Where the data comes from

- **Lines from before the dashboard** stay in the sheet's `明细` tab and are read
  live (cached for a minute), never copied and never written. Change them in the
  sheet. Columns are found by their header text (`Date`, `SKU`, `Note`, `Qty`,
  `Cost CAD`, `Subtotal Amount CAD`, `Request By`, `Sent To`, `Process Date`,
  `拿货`, `出库`), so inserting or moving a column does not break anything; renaming
  `Date`, `SKU` or `Qty` does. Blank rows (the pre-filled `FALSE` checkboxes) are
  skipped. `Month Ref` is ignored: the month comes from `Date`.
- **Lines entered on the dashboard** go to the Supabase table `consumption`
  ([`schema.sql`](schema.sql)). Deleting one is soft (`deleted_at`, `deleted_by`).
- **Reasons** are the `Note Ref` tab (视频需要, 样品需要, 客户需求, 跨部门); a new reason
  added there shows up on the form within a minute. The colours follow that order.
- **Costs** for new lines come from the `Cost Ref` tab (itself an `IMPORTRANGE` of
  the "all cost" sheet), CAD column, first row of a SKU, case-insensitive: the same
  lookup the `明细` tab's `XLOOKUP` does. The cost is **stored with the line** when
  it is entered, so a later price change does not rewrite past months (the sheet's
  own rows re-price live, as they always have). A SKU that Cost Ref does not have
  can be given a cost by hand (marked `*` on the dashboard, `cost_source =
  'manual'`) or left without one (`'none'`, not counted in totals, and the totals
  say how many were left out). A Cost Ref price cannot be overridden on the
  dashboard; change it in the cost sheet.

The dashboard's totals for the sheet's rows match the sheet's `Summary` tab to the
cent (checked 2026-10-15: Jul $502.78, Aug $1,050.94, Sep $174.86, Oct $177.99).

## Access and roles

Identity-Aware Proxy signs people in with Google before any request reaches the
container; only the `superhairpieces.com` domain is on IAP's access list. The app
then verifies IAP's signed header (`X-Goog-IAP-JWT-Assertion`, ES256, audience
`IAP_AUDIENCE`) and checks the domain again (`ALLOWED_DOMAINS`).

| Who | May |
|---|---|
| anyone in the domain | see everything, add lines, tick 拿货 / 出库 and set the process date on any dashboard line |
| whoever entered a line | change its date, SKU, qty, reason, cost, requested by, sent to; delete it |
| `ADMIN_EMAILS` (`manne@`, `anais@`) | change or delete any dashboard line |

Ticking 出库 on a line without a process date fills in today (Toronto time).
Changing calls must carry `X-Requested-With: XMLHttpRequest`, which the page sends
and a form on another site cannot. *Switch account* in the header is IAP's
`/_gcp_iap/clear_login_cookie`. There is no separate sign-in page.

## Setup (done 2026-10-15)

Commands are for Windows `cmd`, one line each.

1. **Project.** Created by Manne, `claude-sessions@shp-ai-bot-2026` added as
   Owner. APIs enabled: Cloud Run, Cloud Build, Artifact Registry, Secret Manager,
   IAP, Sheets, IAM, Logging.
2. **Supabase.** Project created through the Management API (token: Secret
   Manager `SUPABASE_ACCESS_TOKEN` on `shp-ai-bot-2026`). Schema applied with
   `python genc-consumption\setup_db.py` (safe to re-run after a schema change).
   The anon and authenticated keys are refused on the table (RLS on, no policies,
   privileges revoked); only the service-role key reaches it.
3. **Secrets**, in this project's own Secret Manager (`genc-consumption-2026`,
   like `genc-pos-2026`), never in the repo:

   | Secret | Use |
   |---|---|
   | `supabase-service-key` | the project's `service_role` key, mounted as `SUPABASE_SERVICE_KEY`; the runtime account has `secretAccessor` on this secret only |
   | `supabase-db-password` | the Postgres password set at creation; nothing uses it, kept so it is not lost |

   Rotating the key (Supabase, Project Settings, API Keys), pasting it at a hidden prompt:
   `python -c "import getpass,sys; sys.stdout.write(getpass.getpass('Paste the service_role key, then Enter: ').strip())" | gcloud secrets versions add supabase-service-key --data-file=- --project=genc-consumption-2026`
   then redeploy (*Run workflow*) so a new instance reads it.
4. **Sheet.** Shared with the runtime account
   `consumption-dashboard@genc-consumption-2026.iam.gserviceaccount.com` as
   **Viewer** (and with `claude-sessions@shp-ai-bot-2026` as Editor, by Manne).
   If the dashboard says the sheet could not be read, check that share first.
5. **IAP.** `gcloud run deploy --iap` set up IAP's service agent as the
   service's only invoker; the domain grant is
   `gcloud iap web add-iam-policy-binding --project=genc-consumption-2026 --resource-type=cloud-run --region=northamerica-northeast1 --service=genc-consumption --member="domain:superhairpieces.com" --role="roles/iap.httpsResourceAccessor"`
   (a colleague outside the domain: the same with `--member="user:name@example.com"`,
   and add their address to `ADMIN_EMAILS` or their domain to `ALLOWED_DOMAINS`).

## Deployment

[`.github/workflows/deploy-genc-consumption.yml`](../.github/workflows/deploy-genc-consumption.yml)
deploys on every push to `main` touching `genc-consumption/cloud_run/`, or from the
Actions tab, as `claude-sessions` through Workload Identity Federation
(`docs/credentials.md`). The same command by hand:

`gcloud run deploy genc-consumption --source genc-consumption/cloud_run --region northamerica-northeast1 --project genc-consumption-2026 --service-account consumption-dashboard@genc-consumption-2026.iam.gserviceaccount.com --no-allow-unauthenticated --iap --min-instances 0 --max-instances 2 --memory 512Mi --timeout 120 --set-env-vars "^;^SUPABASE_URL=https://zitrqrchbdracjvxbwtn.supabase.co;IAP_AUDIENCE=/projects/83164121214/locations/northamerica-northeast1/services/genc-consumption;ALLOWED_DOMAINS=superhairpieces.com;ADMIN_EMAILS=manne@superhairpieces.com,anais@superhairpieces.com;SPREADSHEET_ID=1fc2OkX2f9jfMlYxHSPuvyFP2p13JygwrguWxJQkeNdE" --set-secrets "SUPABASE_SERVICE_KEY=supabase-service-key:latest"`

`--set-env-vars` replaces the whole set, so keep every variable in it. `^;^` makes
gcloud split on semicolons, because `ADMIN_EMAILS` holds commas.

| Env var | Default | |
|---|---|---|
| `SPREADSHEET_ID` | the office use sheet | |
| `DETAIL_TAB` / `DETAIL_GID` | `明细` / `0` | the tab read, and its gid for the *Sheet row ↗* links |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | | |
| `IAP_AUDIENCE` | | required on Cloud Run: `/projects/<number>/locations/<region>/services/<service>` |
| `ALLOWED_DOMAINS` | `superhairpieces.com` | |
| `ADMIN_EMAILS` | `manne@`, `anais@` | |
| `TIMEZONE` | `America/Toronto` | "today" for dates and the process date |
| `STORE` | `supabase` | `memory` for local demos and tests |
| `AUTH_DISABLED` | | `1` lets everyone in as an admin: local runs only |

## Local runs and tests

`pip install -r genc-consumption\cloud_run\requirements.txt`, then from
`genc-consumption\cloud_run`, with your own Google account able to read the sheet
(`gcloud auth application-default login`):

`set AUTH_DISABLED=1&& set STORE=memory&& python main.py --port 8765`

and open <http://127.0.0.1:8765>. Lines you add live in memory and are gone when
the server stops. `GET /health?deep=1` checks the sheet, Cost Ref and the database.

Tests (no network): from `genc-consumption`, `python -m unittest discover -s tests -v`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Google says *You don't have access* | the account is not in `superhairpieces.com`; grant it as in setup step 5 |
| Banner: *The Google Sheet could not be read* | the sheet is no longer shared with `consumption-dashboard@genc-consumption-2026.iam.gserviceaccount.com`, or the `明细` tab was renamed (`DETAIL_TAB`), or `Date`/`SKU`/`Qty` headers were renamed. Dashboard lines still show |
| Saving says *the Cost Ref tab could not be read* | same share, or the `Cost Ref` tab was renamed; nothing is saved without a price lookup |
| A SKU is *Not in Cost Ref* although it is in the cost sheet | Cost Ref is cached for 10 minutes, and its `IMPORTRANGE` may lag; try again later, or type the cost |
| Banner: *The database could not be read* | the Supabase project is paused or the key was rotated: restore it in Supabase, or add the new key as in setup step 3 |
| *Your Google session has ended* | IAP's session expired; *Reload* |
