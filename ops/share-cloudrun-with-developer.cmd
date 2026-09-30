@echo off
REM ============================================================================
REM  Give a developer DEPLOY rights on the two Gen'C Cloud Run services
REM  (genc-sales-dashboard, walmart-supabase-sync) in project shp-ai-bot-2026.
REM
REM  Usage (Command Prompt, signed in as the project owner):
REM      share-cloudrun-with-developer.cmd dev@example.com
REM
REM  Part 1 first gives each service its OWN least-privilege runtime identity.
REM  Without that, "can deploy" means "can run code as the default compute
REM  account", which holds Editor + Cloud Run Admin on the whole project.
REM  Part 1 is idempotent (safe to re-run) and only moves traffic to the new
REM  revision once it is Ready, so a missing permission shows up as a failed
REM  revision with the old one still serving, not as an outage.
REM
REM  Part 2 grants the developer: deploy on both services, act-as on the two
REM  new runtime identities, and read logs + browse the project. It does NOT
REM  grant Cloud Build / Artifact Registry: the developer deploys by pushing
REM  to main (both repos deploy themselves) or with `gcloud run deploy --image`
REM  from the console; `gcloud run deploy --source .` from a laptop would need
REM  act-as on the Cloud Build account, which is the Editor account again.
REM ============================================================================
setlocal
if "%~1"=="" (
  echo Usage: %~nx0 developer@email.com
  exit /b 1
)
set DEV=user:%~1
set P=shp-ai-bot-2026
set R=us-central1
set DASH_SA=genc-dashboard-runtime@%P%.iam.gserviceaccount.com
set SYNC_SA=walmart-sync-runtime@%P%.iam.gserviceaccount.com

echo.
echo ===== Part 1: dedicated runtime identities =====
call gcloud iam service-accounts create genc-dashboard-runtime --project %P% --display-name "Gen'C dashboard runtime" --description "Least-privilege runtime identity: reads its own secrets, writes logs." 2>nul
call gcloud iam service-accounts create walmart-sync-runtime --project %P% --display-name "walmart-supabase-sync runtime" --description "Least-privilege runtime identity: reads its own secrets, writes logs." 2>nul

REM logs, metrics, traces (write-only roles)
for %%S in (%DASH_SA% %SYNC_SA%) do (
  for %%O in (roles/logging.logWriter roles/monitoring.metricWriter roles/cloudtrace.agent) do (
    call gcloud projects add-iam-policy-binding %P% --member serviceAccount:%%S --role %%O --condition=None --quiet >nul
  )
)

REM exactly the secrets each service references today
for %%X in (SKUVAULT_EMAIL SKUVAULT_PASSWORD SYNC_TOKEN DASHBOARD_PASSWORD AUTH_SECRET DELIVERY_DELETE_PASSWORD) do call gcloud secrets add-iam-policy-binding %%X --project %P% --member serviceAccount:%DASH_SA% --role roles/secretmanager.secretAccessor --quiet >nul
for %%X in (SKUVAULT_EMAIL SKUVAULT_PASSWORD SYNC_TOKEN) do call gcloud secrets add-iam-policy-binding %%X --project %P% --member serviceAccount:%SYNC_SA% --role roles/secretmanager.secretAccessor --quiet >nul

REM the two CI deployers must be able to act as the new identities
for %%S in (%DASH_SA% %SYNC_SA%) do (
  call gcloud iam service-accounts add-iam-policy-binding %%S --project %P% --member serviceAccount:github-actions-deploy@%P%.iam.gserviceaccount.com --role roles/iam.serviceAccountUser --quiet >nul
  call gcloud iam service-accounts add-iam-policy-binding %%S --project %P% --member serviceAccount:claude-sessions@%P%.iam.gserviceaccount.com --role roles/iam.serviceAccountUser --quiet >nul
)

REM switch the services (new revision each)
call gcloud run services update genc-sales-dashboard --region %R% --project %P% --service-account %DASH_SA% --quiet
call gcloud run services update walmart-supabase-sync --region %R% --project %P% --service-account %SYNC_SA% --quiet

echo.
echo ===== Part 2: the developer =====
call gcloud run services add-iam-policy-binding genc-sales-dashboard --region %R% --project %P% --member %DEV% --role roles/run.developer --quiet >nul
call gcloud run services add-iam-policy-binding walmart-supabase-sync --region %R% --project %P% --member %DEV% --role roles/run.developer --quiet >nul
call gcloud iam service-accounts add-iam-policy-binding %DASH_SA% --project %P% --member %DEV% --role roles/iam.serviceAccountUser --quiet >nul
call gcloud iam service-accounts add-iam-policy-binding %SYNC_SA% --project %P% --member %DEV% --role roles/iam.serviceAccountUser --quiet >nul
call gcloud projects add-iam-policy-binding %P% --member %DEV% --role roles/logging.viewer --condition=None --quiet >nul
call gcloud projects add-iam-policy-binding %P% --member %DEV% --role roles/browser --condition=None --quiet >nul

echo.
echo ===== Verify =====
call gcloud run services describe genc-sales-dashboard --region %R% --project %P% --format="value(spec.template.spec.serviceAccountName,status.latestReadyRevisionName,status.traffic[0].percent)"
call gcloud run services describe walmart-supabase-sync --region %R% --project %P% --format="value(spec.template.spec.serviceAccountName,status.latestReadyRevisionName,status.traffic[0].percent)"
call gcloud run services get-iam-policy genc-sales-dashboard --region %R% --project %P% --format="value(bindings.role,bindings.members)"
call gcloud run services get-iam-policy walmart-supabase-sync --region %R% --project %P% --format="value(bindings.role,bindings.members)"
echo.
echo Done. Both services should show their new runtime identity with 100%% traffic on a Ready revision,
echo and %~1 should appear under roles/run.developer on both.
endlocal
