#!/bin/sh
# Runs at container start (nginx image's /docker-entrypoint.d): bakes the dashboard's URL,
# which Cloud Run passes as DASHBOARD_URL, into the sign-in page. Only that one variable is
# substituted, so nothing else in the page that looks like ${...} is touched.
set -e
: "${DASHBOARD_URL:?DASHBOARD_URL must be set to the dashboard's https URL}"
case "$DASHBOARD_URL" in
  https://*) ;;
  *) echo "DASHBOARD_URL must start with https://, got: $DASHBOARD_URL" >&2; exit 1 ;;
esac
envsubst '${DASHBOARD_URL}' < /usr/share/nginx/html/index.template.html > /usr/share/nginx/html/index.html
rm -f /usr/share/nginx/html/index.template.html
