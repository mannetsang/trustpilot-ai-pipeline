#!/bin/bash
# SessionStart hook: make sure the CLIs this repo's work depends on are present.
#
#   gcloud  Cloud Run deploys, Secret Manager, Merchant API troubleshooting.
#           Already baked into the Claude cloud image AND preconfigured for the
#           agent proxy, so this hook only verifies it. It deliberately does not
#           reinstall: a fresh SDK would lose that proxy configuration.
#   gh      Pull requests, run logs and issues from the shell. Not in the image.
#           Ubuntu ships it, and the package lists are already cached, so the
#           install is a few seconds with no third-party apt repo to add.
#
# gh authenticates through the same agent proxy as git, using the placeholder in
# GH_TOKEN. When the org's Claude GitHub App connection is healthy it just works;
# when it is not, `gh api` reports that in plain words, which is far easier to act
# on than git's "could not read Username".
#
# Never blocks a session: every path exits 0, and nothing here is fatal.

set -u

# Only meaningful on the Debian/Ubuntu cloud image. Manne's local machine is
# Windows, where none of this applies.
command -v apt-get >/dev/null 2>&1 || exit 0

missing=()

if command -v gh >/dev/null 2>&1; then
  gh_state="gh $(gh --version 2>/dev/null | head -1 | awk '{print $3}')"
else
  if DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends gh >/tmp/dev-clis-gh.log 2>&1 \
     && command -v gh >/dev/null 2>&1; then
    gh_state="gh $(gh --version 2>/dev/null | head -1 | awk '{print $3}') (installed)"
  else
    gh_state="gh unavailable (see /tmp/dev-clis-gh.log)"
    missing+=(gh)
  fi
fi

if command -v gcloud >/dev/null 2>&1; then
  gcloud_state="gcloud $(gcloud version 2>/dev/null | awk '/Google Cloud SDK/ {print $4}')"
else
  gcloud_state="gcloud MISSING - install from https://cloud.google.com/sdk/docs/install"
  missing+=(gcloud)
fi

echo "dev-clis: ${gcloud_state}; ${gh_state}."
if [ ${#missing[@]} -gt 0 ]; then
  echo "dev-clis: unavailable this session: ${missing[*]}. Commands needing them will fail."
fi

exit 0
