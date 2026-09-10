#!/usr/bin/env bash
# One-shot deploy for the Trustpilot pipeline.
#
# Deploys:
#   - Cloud Run service `trustpilot-webhook` (Flask app in src/cloud_run/)
#   - Cloud Run Job    `trustpilot-reconciler` (src/python/reconcile_reviews.py)
#   - Cloud Scheduler  `trustpilot-reconciler-hourly` (runs the job every 10 min — the primary trigger)
#   - Secret Manager entries for the four runtime secrets
#   - A runtime service account with the roles it needs
#
# Usage (first run):
#   export PROJECT_ID=your-gcp-project
#   export REGION=us-central1                    # optional, default us-central1
#   export TP_API_KEY=tpk-...
#   export TP_SECRET=tps-...
#   export GCHAT_WEBHOOK_URL=https://chat.googleapis.com/v1/spaces/...
#   export GOOGLE_SHEET_ID=1AbC...
#   ./deploy.sh
#
# Re-runs (secrets already in Secret Manager):
#   export PROJECT_ID=your-gcp-project
#   ./deploy.sh
#
# Requires: gcloud >= 461.0.0 (for `gcloud run jobs deploy --source`).

set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID to your GCP project id}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-trustpilot-webhook}"
JOB_NAME="${JOB_NAME:-trustpilot-reconciler}"
SCHEDULER_NAME="${SCHEDULER_NAME:-trustpilot-reconciler-hourly}"
SCHEDULE="${SCHEDULE:-*/10 * * * *}"
SA_NAME="${SA_NAME:-trustpilot-runtime}"
INVOKER_SA_NAME="${INVOKER_SA_NAME:-trustpilot-scheduler}"
TP_BUSINESS_UNIT_ID="${TP_BUSINESS_UNIT_ID:-5e44f707d7d8c700011eaa10}"

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
INVOKER_SA_EMAIL="${INVOKER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

log() { printf '\n\033[1;36m>> %s\033[0m\n' "$*"; }

log "Using project=$PROJECT_ID region=$REGION"
gcloud config set project "$PROJECT_ID" >/dev/null

log "Enabling required APIs (idempotent)"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  sheets.googleapis.com

log "Ensuring runtime service account: $SA_EMAIL"
gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name "Trustpilot pipeline runtime"

log "Granting runtime roles to $SA_EMAIL"
for role in roles/aiplatform.user roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" --role="$role" --condition=None --quiet >/dev/null
done

log "Upserting Secret Manager entries"
for name in TP_API_KEY TP_SECRET GCHAT_WEBHOOK_URL GOOGLE_SHEET_ID; do
  val="${!name-}"
  if [[ -z "$val" ]]; then
    if gcloud secrets describe "$name" >/dev/null 2>&1; then
      echo "  $name: reusing existing secret version"
      continue
    fi
    echo "  $name: MISSING — set the env var and rerun, or create the secret manually" >&2
    exit 1
  fi
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    printf '%s' "$val" | gcloud secrets versions add "$name" --data-file=- >/dev/null
    echo "  $name: added new version"
  else
    printf '%s' "$val" | gcloud secrets create "$name" \
      --replication-policy=automatic --data-file=- >/dev/null
    echo "  $name: created"
  fi
done

log "Deploying Cloud Run webhook service: $SERVICE_NAME"
gcloud run deploy "$SERVICE_NAME" \
  --source src/cloud_run \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --service-account "$SA_EMAIL" \
  --set-secrets "GCHAT_WEBHOOK_URL=GCHAT_WEBHOOK_URL:latest,GOOGLE_SHEET_ID=GOOGLE_SHEET_ID:latest,TP_API_KEY=TP_API_KEY:latest,TP_SECRET=TP_SECRET:latest" \
  --quiet

SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format='value(status.url)')
WEBHOOK_URL="${SERVICE_URL}/webhook"

log "Deploying Cloud Run Job: $JOB_NAME"
gcloud run jobs deploy "$JOB_NAME" \
  --source src/python \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --set-env-vars "TP_BUSINESS_UNIT_ID=${TP_BUSINESS_UNIT_ID},WEBHOOK_URL=${WEBHOOK_URL}" \
  --set-secrets "TP_API_KEY=TP_API_KEY:latest,TP_SECRET=TP_SECRET:latest,GOOGLE_SHEET_ID=GOOGLE_SHEET_ID:latest" \
  --quiet

log "Ensuring scheduler-invoker service account: $INVOKER_SA_EMAIL"
gcloud iam service-accounts describe "$INVOKER_SA_EMAIL" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$INVOKER_SA_NAME" --display-name "Trustpilot scheduler invoker"

gcloud run jobs add-iam-policy-binding "$JOB_NAME" \
  --region "$REGION" \
  --member="serviceAccount:${INVOKER_SA_EMAIL}" \
  --role="roles/run.invoker" --quiet >/dev/null

log "Upserting Cloud Scheduler job: $SCHEDULER_NAME ($SCHEDULE)"
JOB_RUN_URL="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"
if gcloud scheduler jobs describe "$SCHEDULER_NAME" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$SCHEDULER_NAME" \
    --location "$REGION" \
    --schedule "$SCHEDULE" \
    --uri "$JOB_RUN_URL" \
    --http-method POST \
    --oauth-service-account-email "$INVOKER_SA_EMAIL" --quiet
else
  gcloud scheduler jobs create http "$SCHEDULER_NAME" \
    --location "$REGION" \
    --schedule "$SCHEDULE" \
    --uri "$JOB_RUN_URL" \
    --http-method POST \
    --oauth-service-account-email "$INVOKER_SA_EMAIL" --quiet
fi

cat <<POST

============================================================
Deploy complete.

  Webhook URL:  $WEBHOOK_URL
  Runtime SA:   $SA_EMAIL

Manual next steps (once):
  1. Share your Google Sheet with:
       $SA_EMAIL
     as Editor. Without this the sheet writes will fail.

  2. (Optional) Trustpilot has no public API for registering webhooks
     (register_webhook.py hits a non-existent endpoint). If you want
     real-time delivery in addition to the 10-minute reconciler, add a
     webhook manually in Trustpilot Business Admin Centre -> Integrations -> Developers -> Webhook Notifications
     pointing at:
       $WEBHOOK_URL

  3. Kick off a one-off historical backfill:
       gcloud run jobs execute $JOB_NAME --region $REGION \\
         --args=--since,2020-01-01T00:00:00Z,--max,50 --wait
============================================================
POST
