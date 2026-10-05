# HR ATS — jobs dashboard, Indeed pipeline, offer packets

Superhairpieces' applicant tracking: Indeed applications land in the
`office@superhairpieces.com` inbox, get parsed and scored, and HR reviews them
in the jobs dashboard. Built by Alina (2026-07); moved from Airtable to
Supabase in 2026-10 after the Airtable workspace hit its record limit and its
monthly API quota and ingestion stopped on 2026-09-05.

| Piece | What | Where |
|---|---|---|
| `pipeline/indeed_pipeline.py` | New Indeed application emails → Gemini résumé parse + match score → Supabase `candidates`, résumé to Storage and to Drive | Cloud Run **job** `indeed-pipeline`, us-central1, Scheduler every minute |
| `pipeline/offer_packet.py` | Candidate's reply to the hire email with their address → offer letter + confidentiality agreement as a Gmail **draft** | Cloud Run **job** `offer-packet`, us-central1, Scheduler every 10 min |
| `dashboard/` | React UI + small Node server; `db.mjs` is its Supabase API | Cloud Run **service** `jobs-dashboard`, northamerica-northeast2 — https://jobs-dashboard-304363458561.northamerica-northeast2.run.app |
| `supabase/schema.sql` | Tables, run lock, résumé bucket | Supabase project `shp-ats` (ref `qxmwygkwctyksfcmsqsf`, ca-central-1) |
| `migrate/import_airtable.py` | One-time import of the old Airtable base | run by hand |

`dashboard/PRD.md` describes the product (written against the Airtable
version; the behaviour is unchanged apart from soft-deleted jobs).

## Data

Supabase `shp-ats`, schema in `supabase/schema.sql` (idempotent; apply with
the Supabase SQL editor or the management API). Tables: `jobs`, `candidates`,
`employees`, `company_offices`, `insurance_eligibility`, plus `job_locks` and
`ingest_failures` for the pipeline. Résumés live in the private `resumes`
bucket; the dashboard serves them through `/api/resume/<id>` as 5-minute
signed links.

RLS is on with no policies: only the secret key can read or write. Candidate
data is personal data — never use the publishable/anon key from a browser.

Candidates join to jobs by title (`candidates.job_title_applied = jobs.title`),
as on Airtable. When an application names a job that isn't in `jobs`, the
pipeline creates the job itself, status **Open** (HR posts every role on
Indeed, so a role that gets applications is live).

Deleting a job in the dashboard sets `jobs.deleted_at`; restore with
`update jobs set deleted_at = null where id = '…'`.

## Pipeline safeguards

What went wrong on Airtable, and what stops it now:

- Scheduler fires `indeed-pipeline` every minute but a run can take minutes,
  so up to five ran at once. Each run now takes a lease in `job_locks`
  (`try_job_lock`) and exits if another run holds it.
- Every run re-read the whole Candidates table to find processed emails. Now
  it looks up only the ≤200 Gmail ids in hand.
- An email that failed to save stayed unlabelled and was re-parsed by Gemini
  every minute, forever. Now each failure is counted in `ingest_failures`;
  after 3 the email is skipped. To retry one:
  `delete from ingest_failures where gmail_message_id = '…'`.

## Credentials (Secret Manager, project shp-ai-bot-2026)

| Secret | Env var | Used by |
|---|---|---|
| `shp-ats-supabase-secret-key` | `SUPABASE_SECRET_KEY` | all three (Supabase secret key `hr_ats_services`; revoke/rotate in Supabase → Project settings → API keys) |
| `hr-ats-pipeline-gmail-token` | `PIPELINE_OAUTH_JSON` | both jobs — base64 token JSON, office@ mailbox, scopes gmail.modify + drive |
| `jobs-dashboard-gmail-token` | `GMAIL_TOKEN_JSON_B64` | dashboard — base64 token JSON, office@ mailbox, gmail.modify + Chat |
| `jobs-dashboard-password` | `DASHBOARD_PASSWORD` | dashboard login |
| `hr-ats-maps-api-key` | `MAPS_API_KEY` | offer-packet (postal-code lookup) |

The Cloud Run runtime identity (`304363458561-compute@`) has
`secretAccessor` on each. The two Gmail tokens were issued by an OAuth client
in Cloud project **226023136119**, which is not `shp-ai-bot-2026` and was set
up by Alina. If that project or client goes away, both tokens stop working;
re-issuing them under an OAuth client in `shp-ai-bot-2026` removes that
dependency.

## Deploy

Merging to `main` runs `.github/workflows/deploy-hr-ats.yml`, which deploys
all three. Manual equivalent: the `gcloud` commands in that file.

## Importing the Airtable data

**Done 2026-10-05** over the API (`--api --yes`): 19 jobs (+4 pipeline
placeholders merged, 17 duplicate placeholders dropped), 875 candidates
(2,418 duplicates from overlapping old-pipeline runs dropped, 58 already
ingested by the new pipeline), 852 résumés, 19 employees, 9 offices, 12
insurance rows. Don't re-run it now that HR edits in Supabase; the steps
below are kept for reference.

The workspace is over its monthly API quota until the 1st, but the web UI
still works:

1. In Airtable, for each table — **Jobs, Candidates, Employee, Company
   Office, Insurance Eligibility** — open a view that shows every field and
   has no filters, then **Download CSV**. Do it in one sitting: the résumé
   links in the CSV expire after a few hours.
2. Put the five files in one folder and run, with a venv that has
   `pipeline/requirements.txt` plus `google-cloud-secret-manager`:

   ```
   python hr-ats/migrate/import_airtable.py --csv-dir <folder> --dry-run
   python hr-ats/migrate/import_airtable.py --csv-dir <folder> --yes
   ```

   Or, once the quota resets: `--api --yes`.

The import merges the pipeline's placeholder jobs into the real ones and
skips candidates the new pipeline already ingested. Re-running replaces
previously imported rows, so don't re-run after HR starts editing them.

## Local development

```
cd hr-ats/dashboard
npm ci
npm run dev        # reads SUPABASE_SECRET_KEY / DASHBOARD_PASSWORD from ../.env
```

The pipeline reads `hr-ats/pipeline/.env` and falls back to Secret Manager
for the Supabase key (`gcloud auth application-default login`).
