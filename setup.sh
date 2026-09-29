#!/bin/bash
# One-time setup for chat-assistant. Run by a project Owner in Cloud Shell; safe to re-run.
#
# It does only what the deploying identity (claude-sessions, used by GitHub Actions and
# Claude sessions) isn't allowed to: create the app's own service account and its IAM
# grants, give it the secrets, let claude-sessions deploy as it, and create the hourly job.
# Nothing here prints a secret.
set -euo pipefail
PROJECT=shp-ai-bot-2026
REGION=us-central1
SA=chat-assistant@$PROJECT.iam.gserviceaccount.com
DEPLOYER=claude-sessions@$PROJECT.iam.gserviceaccount.com
URL=https://chat-assistant-onvg62bzra-uc.a.run.app

gcloud services enable chat.googleapis.com people.googleapis.com calendar-json.googleapis.com \
  aiplatform.googleapis.com firestore.googleapis.com cloudscheduler.googleapis.com --project $PROJECT

gcloud iam service-accounts describe $SA --project $PROJECT >/dev/null 2>&1 \
  || gcloud iam service-accounts create chat-assistant --project $PROJECT --display-name "Chat assistant (Cloud Run)"
for role in roles/datastore.user roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$SA --role $role --condition=None --quiet >/dev/null
done
# Lets GitHub Actions / Claude sessions deploy the service to run as this account.
gcloud iam service-accounts add-iam-policy-binding $SA --project $PROJECT \
  --member serviceAccount:$DEPLOYER --role roles/iam.serviceAccountUser --quiet >/dev/null

for s in chat-assistant-oauth-client chat-assistant-user-token chat-assistant-run-token; do
  gcloud secrets describe $s --project $PROJECT >/dev/null 2>&1 \
    || gcloud secrets create $s --replication-policy automatic --project $PROJECT >/dev/null
  gcloud secrets add-iam-policy-binding $s --project $PROJECT \
    --member serviceAccount:$SA --role roles/secretmanager.secretAccessor --quiet >/dev/null
done
# The app writes the owner's token after sign-in and disables older versions.
gcloud secrets add-iam-policy-binding chat-assistant-user-token --project $PROJECT \
  --member serviceAccount:$SA --role roles/secretmanager.secretVersionManager --quiet >/dev/null

TOKEN=$(gcloud secrets versions access latest --secret chat-assistant-run-token --project $PROJECT 2>/dev/null || true)
if [ -z "$TOKEN" ]; then
  TOKEN=$(openssl rand -hex 32)
  printf %s "$TOKEN" | gcloud secrets versions add chat-assistant-run-token --data-file=- --project $PROJECT >/dev/null
fi

args=(--location $REGION --project $PROJECT --schedule "0 * * * *" --time-zone America/Toronto
      --uri $URL/run --http-method POST --attempt-deadline 1800s --quiet)
gcloud scheduler jobs update http chat-assistant-hourly "${args[@]}" --update-headers "X-Run-Token=$TOKEN" >/dev/null 2>&1 \
  || gcloud scheduler jobs create http chat-assistant-hourly "${args[@]}" --headers "X-Run-Token=$TOKEN" >/dev/null

echo "chat-assistant setup done: service account, secrets and hourly job are in place."
