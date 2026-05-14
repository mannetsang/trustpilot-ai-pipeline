@echo off
REM Deploy the Trustpilot dashboard to Cloud Run.
REM Prereq: gcloud CLI installed + authenticated (gcloud auth login)
REM        + project selected   (gcloud config set project shp-ai-bot-2026)

cd /d "%~dp0"

echo.
echo Deploying trustpilot-dashboard to Cloud Run (region: us-central1)...
echo.

gcloud run deploy trustpilot-dashboard ^
  --source . ^
  --region us-central1 ^
  --allow-unauthenticated ^
  --port 8080 ^
  --cpu 1 ^
  --memory 256Mi ^
  --min-instances 0 ^
  --max-instances 2

echo.
echo Done. The service URL is printed above.
pause
