# Trustpilot AI Review Pipeline

This repository contains the automated pipeline for processing Trustpilot reviews, extracting insights, and logging them using the Gemini 2.5 Pro model.

It features a dual-architecture design to support both real-time webhook ingestion and bulk offline processing.

## Architecture

1. **Option A: Real-Time Apps Script (Google AI Studio)**
   - **File:** `src/apps_script/trustpilot_webhook.js`
   - **Environment:** Google Apps Script
   - **Authentication:** Personal API Key (`AIza...`)
   - **Purpose:** Automatically scans the company Gmail for new incoming Trustpilot review notifications, extracts the star rating and comment, queries `gemini-2.5-pro` for an operational improvement suggestion, logs the entry into a Google Sheet, and fires a webhook to a Google Chat space.

2. **Option B: Bulk Processing (Google Cloud Vertex AI)**
   - **File:** `src/python/bulk_process_reviews.py`
   - **Environment:** Local Python
   - **Authentication:** Enterprise Service Account (`gcp_credentials.json`)
   - **Purpose:** Designed for massive CSV exports. Iterates through thousands of historical Trustpilot reviews, generates insights using the Vertex AI SDK, and appends the data robustly to a processed dataset.

## Setup Instructions

### Python (Option B)
```bash
pip install -r requirements.txt
export GOOGLE_APPLICATION_CREDENTIALS="path/to/your/gcp_credentials.json"
python src/python/bulk_process_reviews.py
```

### Apps Script (Option A)
1. Copy the code from `src/apps_script/trustpilot_webhook.js`.
2. Paste it into the Google Apps Script editor.
3. Update the `GOOGLE_SHEET_ID` and `GCHAT_WEBHOOK_URL` constants.
4. Set up a Time-driven trigger to run `processTrustpilotReviews` every 5 minutes.

## Reconciler / Backfill

`src/python/reconcile_reviews.py` pulls the full review history for the
business unit from the Trustpilot private API, diffs it against the
Google Sheet (by review ID in column **H**, with a name+rating+comment
fingerprint fallback for rows that pre-date the ID column), and replays
the missing ones through the Cloud Run `/webhook` endpoint. This both
backfills historical gaps and acts as a self-healing layer when live
webhook deliveries are dropped.

The Cloud Run handler now writes the review ID into column H and skips
any `review.created` event whose ID is already present, so re-runs are
idempotent.

### One-shot backfill
```bash
export TP_API_KEY=...
export TP_SECRET=...
export GOOGLE_SHEET_ID=...
export WEBHOOK_URL=https://<your-cloud-run-host>/webhook
# (also: GOOGLE_APPLICATION_CREDENTIALS pointing at a service account
#  with Sheets read access on GOOGLE_SHEET_ID)

# Preview first
python src/python/reconcile_reviews.py --since 2020-01-01T00:00:00Z --dry-run

# Then for real (use --max to throttle the Chat card flood)
python src/python/reconcile_reviews.py --since 2020-01-01T00:00:00Z --max 50
```

### Scheduled reconciler (primary trigger)
`deploy.sh` runs the reconciler as a **Cloud Run Job** every 10 minutes
via **Cloud Scheduler**, with a 24h lookback (`--since-hours 24`). This
is the *primary* way new reviews reach Chat and the Sheet, not a
fallback: Trustpilot exposes no public API for registering webhooks
(`/v1/private/webhooks` does not exist; `register_webhook.py` gets a
404), so the `/webhook` receiver in `src/cloud_run/main.py` is only ever
invoked by the reconciler's replays unless you configure one manually
(Trustpilot Business Admin Centre -> Integrations -> Developers ->
Webhook Notifications).
