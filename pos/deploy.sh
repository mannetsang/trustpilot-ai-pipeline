#!/bin/bash
# Deploy the register to Cloud Run from source. Idempotent: re-run to roll out
# a new revision. Works from a Claude cloud session, GitHub Actions or a
# workstation, as long as the active gcloud identity holds the roles listed in
# pos/README.md.
#
# The database URL is mounted from Secret Manager. The till code is read by the
# service itself from the POS_ACCESS_CODE secret, so it can be created or
# rotated without a redeploy.
set -euo pipefail

PROJECT="${PROJECT:-shp-ai-bot-2026}"
REGION="${REGION:-northamerica-northeast1}"     # Montreal
SERVICE="${SERVICE:-shp-pos}"
SOURCE="$(cd "$(dirname "$0")" && pwd)/src/cloud_run"

gcloud run deploy "$SERVICE" \
  --source "$SOURCE" \
  --region "$REGION" \
  --project "$PROJECT" \
  --allow-unauthenticated \
  --set-secrets "SUPABASE_DB_URL=SUPABASE_DB_URL:latest" \
  --set-env-vars "GCP_PROJECT=$PROJECT" \
  --min-instances 0 \
  --max-instances 3 \
  --memory 512Mi \
  --quiet

gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format 'value(status.url)'
