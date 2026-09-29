# Chat Assistant

A personal assistant for Manne that lives on Google Chat. Every hour it reads
new messages in every space, group chat and DM Manne is in, keeps a project
board current, and acts on Manne's behalf: it replies in Chat and adds
calendar events.

```
Cloud Scheduler (hourly) ──POST /run──►  Cloud Run: chat-assistant (Flask)
                                          │  as Manne (OAuth token in Secret Manager)
                                          ├─ Chat API: spaces, messages, replies
                                          ├─ People API: directory (ids → names)
                                          ├─ Calendar API: events
                                          ├─ Vertex AI Gemini: tasks + actions per conversation
                                          └─ Firestore: board, activity, runs, watermarks
Browser ──Google sign-in (Manne only)──►  board · activity · pause switch · run now
```

## What it does each run

1. Lists every conversation and picks those with messages newer than its
   watermark (first run: the last 24 hours).
2. For each one, fetches the new messages plus up to 20 earlier ones for
   context, resolves who said what through the company directory, and asks
   Gemini for **tasks** (new or updates to open ones) and **actions**.
3. Applies tasks to the board. Tasks you've edited yourself are never
   overwritten.
4. Runs actions, or parks them as suggestions (see below).
5. Advances the watermark. If a conversation fails, its watermark stays put
   and the next run retries it.

Conversations where every new post is from an app or bot (e.g. Reddit
Notification) are skipped unless "Also read posts from apps and bots" is on.

## Acting on its own, and the guardrails

Replies go out **under Manne's name**. An action runs unreviewed only when
all of these hold; otherwise it waits in **Activity → Waiting for you** with
Review & send / Dismiss:

| Check | Default | Env var |
|---|---|---|
| "Act automatically" switch is on | on | (UI) |
| Answers a new message from another person, never Manne's own or a bot's | — | — |
| That message is directed at Manne (DM, @mention, or clearly addressed) | — | — |
| Manne hasn't already replied later in that thread | — | — |
| Message is recent | ≤ 12 h | `ACT_MAX_AGE_HOURS` |
| Gemini's confidence | ≥ 0.85 | `ACT_CONFIDENCE` |
| Automatic actions per run | ≤ 10 | `MAX_ACTIONS_PER_RUN` |

Calendar invites only go to people in the company directory. Every action,
sent or held, is logged with the message it answers and the reason.

## Security

- **Only `OWNER_EMAIL`** (manne@superhairpieces.com) can sign in; any other
  Google account is refused after Google verifies it.
- The token is **Manne's own OAuth sign-in**, so it can reach only Manne's
  chats and calendar. There is no domain-wide delegation.
- It lives in Secret Manager as `chat-assistant-user-token`, and the workflow
  grants read access on it only to the dedicated `chat-assistant@` service
  account. A new sign-in disables the old version.
- **That isolation holds only if nothing else has project-wide Secret Manager
  read access.** A project-level *Secret Manager Secret Accessor* (or Admin)
  grant lets its holder read this token and post as the owner. Keep such
  grants per secret, as `docs/credentials.md` says. Check before connecting:
  IAM & Admin → IAM, look for Secret Manager roles granted on the project.
- Scheduler calls `/run` with a shared token (`chat-assistant-run-token`);
  UI writes need the signed-in session plus a custom header (CSRF).
- Revoke everything at <https://myaccount.google.com/permissions>.

## One-time setup

Console steps, all on project `shp-ai-bot-2026`:

1. **Google Chat API → Configuration**
   (<https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=shp-ai-bot-2026>):
   app name, avatar URL, description; interactive features **off**. Save.
2. **OAuth consent screen**: audience **Internal**. External apps in Testing
   mode lose their sign-in after 7 days.
3. **Credentials → Create credentials → OAuth client ID → Web application.**
   Authorized redirect URIs, add both:
   - `https://chat-assistant-onvg62bzra-uc.a.run.app/oauth/callback`
   - `https://chat-assistant-304363458561.us-central1.run.app/oauth/callback`

   Download the JSON.
4. **Secret Manager → Create secret** named `chat-assistant-oauth-client`,
   upload that JSON file as the value, then delete the downloaded file.
5. **Deploy**: merge to `main`, or GitHub → Actions → *Chat assistant
   (deploy + hourly schedule)* → Run workflow. It enables the APIs, creates
   the service account and secrets, deploys, and creates the hourly job. The
   run summary prints the app URL.
6. Open the app, sign in, press **Connect** and tick every permission. The
   first hourly run happens on the hour; **Run now** starts one immediately.

If the workflow warns it couldn't grant `roles/datastore.user` or
`roles/aiplatform.user`, a project Owner must run the command it prints.

## Configuration

| Env var | Default |
|---|---|
| `OWNER_EMAIL` | `manne@superhairpieces.com` |
| `GEMINI_MODEL` | `gemini-2.5-pro` |
| `TIME_ZONE` | `America/Toronto` |
| `INITIAL_LOOKBACK_HOURS` | `24` |
| `PARALLEL_SPACES` | `4` |

## Local development and tests

Tests use a fake Google, a fake Gemini and an in-memory store, so they need
no network or credentials:

```
python -m unittest discover -s chat-assistant/tests
```

`STORE_BACKEND=memory python chat-assistant/src/cloud_run/main.py` runs the
app locally against the in-memory store (sign-in still needs a real OAuth
client).
