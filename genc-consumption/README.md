# GenC Consumption

A dashboard of what GenC Beauty takes out of its own stock for office use (videos,
samples, customers, other departments) and what it cost, with a form to log new
lines. Live since 2026-10-15 at
<https://genc-consumption-eqr4wko2oa-nn.a.run.app> (Google sign-in,
`superhairpieces.com` accounts only).

The page has two tabs under one row of filters (year, month, reason, requester,
status, source, search), which apply to both: **Transactions** (the first, and
the default) lists every line, with 拿货 / 出库 ticks, edit and CSV download;
**Charts** has the totals, spend by month (chart or table), and spend by reason,
requester and SKU. The open tab is kept in the address (`#transactions`,
`#charts`), so a reload or a shared link opens the same one.

**On a phone** (720 px wide or less) the same page changes layout: the
transactions are cards (date, reason, SKU, qty × cost, requested by / sent to,
the 拿货 and 出库 ticks), sorted newest, oldest or largest; the filters fold into
a **筛选 Filters** button that shows how many are set; **+ 添加 Add** and
**🎙 问助手** sit in a bar at the bottom; the Add form and the voice assistant
open full screen; inputs are 16 px so iPhones do not zoom in on them.

## Voice assistant (语音助手)

Added 2026-10-15. **🎙 问助手 Ask** opens a conversation with Gemini Live
(`gemini-3.8-live` on Vertex AI, `us-central1`): tap the microphone and talk,
or type; it answers out loud with a live transcript and can be interrupted
mid-sentence. It speaks **Mandarin by default** and English when spoken to in
English. Example: 八月份一共花了多少钱？ → 八月份一共花了大约 1,051 加元。

- **Read only.** Its tools ([`cloud_run/voice.py`](cloud_run/voice.py)) total
  and list lines with the same filters as the page, search Cost Ref, read
  live SkuVault stock (WH1) and switch the dashboard to a tab and filters
  ("给我看九月份的样品"). It cannot add, change, delete or stock out a line,
  and never changes SkuVault.
- **How it streams.** The page records the microphone (16 kHz PCM, made in the
  browser, so it works in Safari on iPhone too) and sends it to this service,
  which holds the one connection to the Live API as the runtime account and
  relays the spoken answer (24 kHz PCM) back. The server, not the page, picks
  the model, writes the instructions and runs the tools. The page connects by
  WebSocket (`/api/voice/ws`, own origin only) and, if the network refuses
  WebSockets, falls back by itself to plain HTTP (`/api/voice/sessions`:
  POST to send, a server-sent-event stream to receive).
- **Limits.** One session per person (a new one ends the old); a session ends
  after 10 minutes or 3 silent minutes (tap the mic to start a new one, which
  forgets the earlier conversation); at most 8 at once per instance. The
  instructions carry the names of requesters and sent-to values from the data
  so misheard names (STC heard as 西门) are matched to real ones.
- **Setup (done):** Vertex AI API enabled on `genc-consumption-2026`, the
  runtime account holds `roles/aiplatform.user`. `LIVE_MODEL`,
  `LIVE_LOCATION`, `LIVE_VOICE`, `VOICE_DISABLED=1` change or switch it off.
  Voice audio goes to Google's `us-central1` region.
- **Not verified from here:** whether Identity-Aware Proxy lets the WebSocket
  through for a signed-in browser (it cannot be tested without a person's
  Google sign-in). The HTTP fallback covers a refusal; if neither works the
  panel says *无法连接语音助手* with the reason.

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

## SkuVault stock-out (出库)

Added 2026-10-15. 出库 on a dashboard line means the stock came out of SkuVault.
The Add form offers three choices, the first one preselected:

| Choice | What happens |
|---|---|
| **Remove from SkuVault now** | the line is saved first, then its quantity is removed from SkuVault (`inventory/removeItem`) and 出库 is ticked |
| **Already removed in SkuVault by hand** | 出库 is ticked, SkuVault is not touched (`skuvault_status = 'manual'`) |
| **Not stocked out yet** | nothing; ticking 出库 in the table later asks, then removes it from SkuVault |

- **Bins:** least stock first, only in `SKUVAULT_WAREHOUSES` (**WH1** only for
  now), reserved bins skipped; when the smallest bin does not hold enough, the
  rest comes out of the next one. One bin lookup per save for every item
  (`getInventoryByLocation`), then one `removeItem` per bin. What came out of
  which bin is kept on the line (`skuvault_removed`) and shown in the table and
  the edit dialog.
- **Reason:** the English name of the dashboard reason (`SKUVAULT_REASONS`):

  | Dashboard | SkuVault transaction reason |
  |---|---|
  | 视频需要 | `Video` |
  | 样品需要 | `Sample` |
  | 客户需求 | `Customer Request` |
  | 跨部门 | `Cross-Department` |

  **Each must exist in SkuVault** (its transaction-reason settings) before the
  first removal: SkuVault refuses a reason it does not know, and the line is then
  saved as *failed* with SkuVault's message. To use other names, change them in
  SkuVault and set `SKUVAULT_REASONS` to the JSON map (e.g.
  `{"视频需要": "Video", ...}`) in the deploy.
- **Whole units only** (SkuVault's quantity is an integer).
- **When it fails** (not enough stock in WH1, unknown reason, SkuVault busy) the
  line is still saved, 出库 stays unticked, the table shows a **!** with the
  reason, and ticking 出库 again retries. If one bin succeeded and the next
  failed the line is *partial*: the retry removes only what is left.
- **Locked once removed:** 出库 cannot be unticked and the SKU and quantity
  cannot change (date, reason, requested by, sent to still can). Deleting the line
  does not put the stock back: correct SkuVault by hand.
- **No double removal:** a removal first claims the line in one conditional
  database update (`skuvault_status` none/failed/partial to `pending`), so two
  clicks or two people cannot both remove it. A line left `pending` by an
  instance that died can be retried after 10 minutes (check SkuVault first).
- **Login:** the `SKUVAULT_EMAIL` / `SKUVAULT_PASSWORD` secrets on
  `shp-ai-bot-2026` (the SkuVault tenant the Walmart bridge also uses), mounted
  cross-project; the runtime account `consumption-dashboard@` holds
  `secretAccessor` on those two secrets only. Removals show in SkuVault as that
  user. SkuVault allows 5 bin lookups and 10 removals a minute per tenant, shared
  with the other integrations; a short throttle is waited out, a long one fails
  the removal with *SkuVault is busy*.
- **Staff who removed office stock in SkuVault by hand** (reason *Office Use
  (Video/Packaging/Floor Model)*) should stop doing both: either let the
  dashboard remove it, or choose *Already removed in SkuVault by hand*.

## Access and roles

Identity-Aware Proxy signs people in with Google before any request reaches the
container; only the `superhairpieces.com` domain is on IAP's access list. The app
then verifies IAP's signed header (`X-Goog-IAP-JWT-Assertion`, ES256, audience
`IAP_AUDIENCE`) and checks the domain again (`ALLOWED_DOMAINS`).

| Who | May |
|---|---|
| anyone in the domain | see everything, add lines, tick 拿货 / 出库 (which removes the stock from SkuVault) and set the process date on any dashboard line |
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

`gcloud run deploy genc-consumption --source genc-consumption/cloud_run --region northamerica-northeast1 --project genc-consumption-2026 --service-account consumption-dashboard@genc-consumption-2026.iam.gserviceaccount.com --no-allow-unauthenticated --iap --min-instances 0 --max-instances 2 --memory 512Mi --timeout 120 --set-env-vars "^;^SUPABASE_URL=https://zitrqrchbdracjvxbwtn.supabase.co;IAP_AUDIENCE=/projects/83164121214/locations/northamerica-northeast1/services/genc-consumption;ALLOWED_DOMAINS=superhairpieces.com;ADMIN_EMAILS=manne@superhairpieces.com,anais@superhairpieces.com;SPREADSHEET_ID=1fc2OkX2f9jfMlYxHSPuvyFP2p13JygwrguWxJQkeNdE;SKUVAULT_WAREHOUSES=WH1" --set-secrets "SUPABASE_SERVICE_KEY=supabase-service-key:latest,SKUVAULT_EMAIL=projects/304363458561/secrets/SKUVAULT_EMAIL:latest,SKUVAULT_PASSWORD=projects/304363458561/secrets/SKUVAULT_PASSWORD:latest"`

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
| `SKUVAULT_EMAIL`, `SKUVAULT_PASSWORD` | | the SkuVault login (secrets, cross-project); without them the "remove from SkuVault" choice is off |
| `SKUVAULT_WAREHOUSES` | `WH1` | warehouse codes stock may come out of, comma-separated (others: `RCE`, `WH2`, `IT1`, `PKO`, `PKP`) |
| `SKUVAULT_REASONS` | the table above | JSON map of dashboard reason to SkuVault transaction reason |
| `AUTH_DISABLED` | | `1` lets everyone in as an admin: local runs only |

## Local runs and tests

`pip install -r genc-consumption\cloud_run\requirements.txt`, then from
`genc-consumption\cloud_run`, with your own Google account able to read the sheet
(`gcloud auth application-default login`):

`set AUTH_DISABLED=1&& set STORE=memory&& python main.py --port 8765`

and open <http://127.0.0.1:8765>. Lines you add live in memory and are gone when
the server stops. **Never put `SKUVAULT_EMAIL` / `SKUVAULT_PASSWORD` in a local
`.env`:** a local run with them removes real stock. Without them the "remove from
SkuVault" choice is switched off; the tests use a fake SkuVault. `GET /health?deep=1` checks the sheet, Cost Ref and the database.

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
| **!** on 出库, *SkuVault refused the removal ... Reason* | the SkuVault reason (table above) does not exist in SkuVault yet: create it, then tick 出库 again |
| **!** on 出库, *only N on hand in the allowed warehouses* | WH1 holds less than the line's quantity (often the stock sits in RCE `GCE`): move it in SkuVault, or remove it by hand and choose *Already removed in SkuVault by hand* |
| *SkuVault is busy* | the shared rate limit (5 lookups / 10 removals a minute); tick 出库 again in a minute |
| Voice: *麦克风被拒绝* | the browser was refused the microphone: allow it from the address bar (iPhone: Settings, Safari, Microphone), or type the question |
| Voice: the answer is very quiet on an iPhone | iOS plays sound through the earpiece while the microphone is open: turn the volume up, use headphones, or tap the mic off to hear the end |
| Voice: *无法连接语音助手* | the WebSocket and the HTTP fallback both failed: the reason follows the message (sign-in expired: reload; *too many voice sessions*: wait a minute) |
| Voice: it keeps interrupting itself | the phone's speaker reaches the microphone: lower the volume or use headphones |
