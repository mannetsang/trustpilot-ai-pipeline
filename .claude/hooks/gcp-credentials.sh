#!/bin/bash
# SessionStart hook: writes this session's GCP service-account key from
# GCP_SA_KEY_B64 so the gcloud CLI and the Python Secret Manager client work.
#
# Why a hook and not the environment's setup script: the setup script runs
# once per environment and its filesystem is snapshotted for roughly a week,
# so a key file written there goes stale, or is never written at all, whenever
# GCP_SA_KEY_B64 is added or rotated afterwards. Hooks run at the start of
# every session and see that session's environment variables.
#
# Inputs, from the cloud environment's Environment variables box:
#   GCP_SA_KEY_B64                  service-account JSON key, base64, one line
#   GOOGLE_APPLICATION_CREDENTIALS  where to write it (default /opt/gcp-sa.json)
#
# Never prints the key. Always exits 0 so a problem here cannot block a session.

case "${CLAUDE_CODE_REMOTE:-}" in
  true|1) ;;
  *) exit 0 ;;  # local sessions use .env or `gcloud auth application-default login`
esac

if [ -z "${GCP_SA_KEY_B64:-}" ]; then
  echo "gcp-credentials: GCP_SA_KEY_B64 is not set; Secret Manager is unavailable this session."
  exit 0
fi

key_file="${GOOGLE_APPLICATION_CREDENTIALS:-/opt/gcp-sa.json}"
key_dir=$(dirname "$key_file")
mkdir -p "$key_dir" 2>/dev/null || { echo "gcp-credentials: cannot create $key_dir; key not written."; exit 0; }

umask 077
tmp=$(mktemp "$key_dir/.gcp-sa.XXXXXX" 2>/dev/null) || { echo "gcp-credentials: cannot write to $key_dir; key not written."; exit 0; }

if ! printf '%s' "$GCP_SA_KEY_B64" | tr -d '[:space:]' | base64 -d > "$tmp" 2>/dev/null; then
  rm -f "$tmp"
  echo "gcp-credentials: GCP_SA_KEY_B64 is not valid base64; key not written."
  exit 0
fi

email=$(python3 -c 'import json, sys
d = json.load(open(sys.argv[1]))
assert d["type"] == "service_account"
print(d["client_email"])' "$tmp" 2>/dev/null) || {
  rm -f "$tmp"
  echo "gcp-credentials: decoded value is not a service-account JSON key; key not written."
  exit 0
}

mv -f "$tmp" "$key_file" && chmod 600 "$key_file"

if command -v gcloud >/dev/null 2>&1; then
  gcloud auth activate-service-account --key-file="$key_file" --quiet >/dev/null 2>&1 \
    || echo "gcp-credentials: gcloud activation failed; the Python client still works through ADC."
  gcloud config set project shp-ai-bot-2026 --quiet >/dev/null 2>&1 || true
fi

echo "gcp-credentials: $key_file ready for $email."
exit 0
