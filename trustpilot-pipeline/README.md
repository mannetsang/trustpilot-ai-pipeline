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

## Weekly EU Website-Feedback Digest

The Cloud Run service (`src/cloud_run/main.py`) exposes `/weekly-eu-feedback`: it reads
the website feedback Google Sheet, filters rows from the EU storefronts (`.es`, `.nl`,
`.fr`, `.de` — currently only `.es` collects feedback) logged in the last 7 days, and
posts a summary card (stats, comments, Gemini insights) to the **EU SEO/AEO Tasks**
Google Chat space via the `EU_GCHAT_WEBHOOK_URL` env var. A Cloud Scheduler job calls
it every Monday 8:00 AM Toronto time (see `.github/workflows/setup-eu-feedback.yml`
at the repo root for one-time setup). Preview without posting:
`GET /weekly-eu-feedback?dry_run=1`.

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
