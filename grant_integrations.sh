#!/usr/bin/env bash
# Lets the Company Assistant read the keys its company-system integrations use
# (src/cloud_run/integrations.py): each secret on its own, for the app's own
# service account, nothing project-wide. Run in Cloud Shell as a project Owner:
#
#   bash chat-assistant/grant_integrations.sh
#
# Keep SECRETS in step with integrations.secret_names(); a test checks it.
set -euo pipefail

PROJECT=shp-ai-bot-2026
SA="chat-assistant@${PROJECT}.iam.gserviceaccount.com"
SECRETS=(
  AIRTABLE_COMPANY_TOKEN
  BIGCOMMERCE_gmosz3ja_ACCESS_TOKEN
  BIGCOMMERCE_qet21urb3p_ACCESS_TOKEN
  FIGMA_TOKEN
  GENC_BIGCOMMERCE_PRODUCT_ACCESS_TOKEN
  GENC_BIGCOMMERCE_PRODUCT_API_PATH
  NOTION_API_KEY
  OMNISEND_API_KEY
  STAMPED_PRIVATE_KEY
  STAMPED_PUBLIC_KEY
  STAMPED_STORE_HASH
  TRUSTPILOT_API_KEY
)

for secret in "${SECRETS[@]}"; do
  gcloud secrets add-iam-policy-binding "$secret" --project "$PROJECT" \
    --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor --condition=None --quiet >/dev/null
  echo "granted: $secret"
done
echo "Done. In the app, open Access and press Check connections."
