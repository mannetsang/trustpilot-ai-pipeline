# Making a new Claude cloud session productive immediately

Three layers, in order of payoff. None of them is the session's system prompt,
which doesn't persist between sessions.

## 1. `CLAUDE.md` — what the agent needs to know

Checked into the repo and loaded automatically in every session, on every
surface. This is the durable equivalent of a system prompt. Facts that cost a
round-trip to establish belong here: GCP project, secret naming, which store
hash is which storefront. See the **Data & Credentials** section of the root
`CLAUDE.md`.

## 2. Setup script — tooling ready before Claude starts

The cloud environment editor has a **Setup script** field: a bash script that
runs when a new session starts, *before* Claude Code launches.

Two platform rules shape what belongs in it. It runs **once** per environment;
the filesystem is then snapshotted and reused for about a week, and only a
change to the script text or the network allowlist triggers a rerun. And it
must **exit zero**, or the session fails to start. So: tooling only, every
step non-fatal, and nothing session-specific such as credentials (see §3).

```bash
#!/bin/bash
# Tooling only. Credentials are written per session by .claude/hooks/gcp-credentials.sh.

# The system `cryptography` package is broken in this image and breaks every
# google-cloud-* import, so keep the GCP libraries in their own venv.
if [ ! -x /opt/gcp-venv/bin/python ]; then
  python3 -m venv /opt/gcp-venv \
    && /opt/gcp-venv/bin/pip install -q --upgrade pip \
    && /opt/gcp-venv/bin/pip install -q google-cloud-secret-manager \
    || echo "setup: gcp-venv install failed" >&2
fi

# gcloud CLI (~1 min, kept by the snapshot). Non-fatal: the Python path works without it.
if ! command -v gcloud >/dev/null 2>&1; then
  (
    set -e
    curl -sSL --max-time 300 -o /tmp/gcloud.tar.gz https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz
    tar -xzf /tmp/gcloud.tar.gz -C /opt && rm -f /tmp/gcloud.tar.gz
    /opt/google-cloud-sdk/install.sh --quiet --usage-reporting=false --path-update=false --command-completion=false >/dev/null
    ln -sf /opt/google-cloud-sdk/bin/gcloud /usr/local/bin/gcloud
    gcloud config set project shp-ai-bot-2026 --quiet
  ) || echo "setup: gcloud install failed" >&2
fi
exit 0
```

Then run scripts with `/opt/gcp-venv/bin/python` instead of `python3` when they
touch Secret Manager.

### The credential is written by a SessionStart hook, not the setup script

Because the setup script is snapshotted, a key file it writes goes stale, or is
never written at all, whenever the key variable is added or rotated after the
first run. [`.claude/hooks/gcp-credentials.sh`](../.claude/hooks/gcp-credentials.sh),
wired up in [`.claude/settings.json`](../.claude/settings.json), runs at the
start of **every** cloud session instead. It decodes `GCP_SA_KEY_B64` to the
path in `GOOGLE_APPLICATION_CREDENTIALS` (default `/opt/gcp-sa.json`),
activates gcloud with it, and exits 0 no matter what so it can never block a
session. It does nothing in local sessions.

The environment's variables box therefore needs exactly two lines:

```
GOOGLE_APPLICATION_CREDENTIALS=/opt/gcp-sa.json
GCP_SA_KEY_B64=<the service-account JSON key, base64-encoded on one line>
```

Rotating the key is just replacing the second value; the next new session picks
it up with no snapshot rebuild.

## 3. Credentials — the part that actually cost the time

API credentials, where the agent proxy injects a key it never sees, aren't
available on this organization. So a cloud session can only reach GCP if a
credential is readable inside it. Two honest options:

**Keep secrets out of sessions.** Reports and syncs run in GitHub Actions or
Cloud Run, authenticating with `GCP_SA_KEY`. Nothing sensitive enters a
transcript. Best for anything recurring.

**A scoped key in the environment variables box.** If sessions need to
self-serve, create one least-privilege service account, grant it
`secretmanager.secretAccessor` on only the specific secrets sessions may read,
and put its key in the environment's variables. It is plaintext and readable by
every session in the org - but scoped that narrowly, its blast radius is "read
these three secrets", which is a far better trade than pasting a
project-Editor key into a conversation.

Never use a broad account this way. `shp-ai-bot-2026@appspot` can enumerate
every secret in the project; `304363458561-compute@` normally carries Editor.
