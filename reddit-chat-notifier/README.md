# Reddit → Google Chat notifier

Posts a Google Chat card whenever there is **new activity in a subreddit** —
new posts, and optionally new comments. Watches `r/HairSystem` by default;
any list of subreddits can be configured.

## Why it polls RSS instead of using a real webhook

Reddit does not offer push webhooks for subreddit activity. But every
subreddit exposes public RSS (Atom) feeds that need **no API key, no OAuth,
no reddit account**:

- `https://www.reddit.com/r/<sub>/new/.rss` — new submissions
- `https://www.reddit.com/r/<sub>/comments/.rss` — new comments

So this service polls those feeds every 5 minutes, remembers the newest entry
it has already announced (watermark in Firestore), and posts a Chat card for
anything newer. To the channel it looks exactly like a webhook, just with up
to ~5 min latency.

## Architecture

```
Cloud Scheduler (*/5 * * * *)
        │  GET /poll?token=…
        ▼
Cloud Run: reddit-chat-notifier (Flask + gunicorn)
        │  GET reddit.com/r/<sub>/new/.rss  (+ /comments/.rss if enabled)
        │  ▲ reads/writes watermark in Firestore (reddit_notifier/<sub>__<kind>)
        ▼
Google Chat space (incoming webhook)  →  card per new post/comment
```

Everything mirrors the IG notifier (project `shp-ai-bot-2026`, region
`us-central1`, deployed by GitHub Actions with the `GCP_SA_KEY` secret,
Firestore Native mode already enabled).

## One-time prerequisites

1. **Google Chat incoming webhook** — in the target Chat space:
   *space name → Apps & integrations → Webhooks → Add webhook*. Copy the URL.
2. Add these **repo secrets** (Settings → Secrets and variables → Actions):

   | Secret | Value |
   |---|---|
   | `REDDIT_GCHAT_WEBHOOK_URL` | the Chat webhook URL from step 1 |
   | `POLL_TOKEN` | already exists — reused from the IG notifier |
   | `GCP_SA_KEY` | already exists — reused from the other pipelines |

   No Reddit credentials are needed at all.

## Deploy

1. **Deploy** — push to `main` (the `reddit-chat-notifier/src/cloud_run/**`
   path triggers it) or run **Deploy Reddit notifier to Cloud Run** from the
   Actions tab.
2. **Wire up** — run the **Setup Reddit notifier** workflow from the Actions
   tab. Inputs:
   - `subreddits` — comma-separated, default `HairSystem`
   - `feeds` — `posts` (default) or `posts,comments`

   It pushes the env vars onto the service and creates the
   `reddit-notifier-poll` Cloud Scheduler job (every 5 minutes, Toronto time).
   Re-run it any time to change what's watched.

## How the first run behaves

The very first `/poll` **seeds** the watermark per feed to the newest existing
entry and sends nothing — so the channel isn't spammed with the subreddit's
back catalogue. From then on, only entries created after that moment are
announced, oldest-first so cards arrive chronologically.

## Endpoints

| Route | Purpose |
|---|---|
| `GET /` | health check |
| `GET/POST /poll?token=…` | fetch feeds, announce anything new |

## Environment variables

| Var | Required | Default | Notes |
|---|---|---|---|
| `REDDIT_SUBREDDITS` | ✅ | — | comma-separated, e.g. `HairSystem,Hairloss` |
| `REDDIT_GCHAT_WEBHOOK_URL` | ✅ | — | target Chat space webhook |
| `REDDIT_FEEDS` | optional | `posts` | `posts` or `posts,comments` |
| `POLL_TOKEN` | optional | — | if set, `/poll` requires `?token=` |
| `REDDIT_USER_AGENT` | optional | descriptive UA | reddit throttles generic UAs hard |
| `REDDIT_FEED_LIMIT` | optional | `25` | entries fetched per feed per poll |

## Rate limits

Reddit rate-limits RSS by IP and is **much stricter for datacenter IPs**
(Cloud Run egress): in practice even the second request 3–13 s after the
first gets a 429, while one request every ~2+ minutes never does. So each
feed gets its **own Cloud Scheduler job at offset minutes** — `/poll` accepts
a `?feeds=posts` / `?feeds=comments` override:

- `reddit-notifier-poll` — `*/5 * * * *` → `&feeds=posts`
- `reddit-notifier-poll-comments` — `2-59/5 * * * *` → `&feeds=comments`

Never fetch two feeds in one poll from Cloud Run. A stray 429 simply skips
that cycle; the next poll catches up (each feed holds the latest 25 entries,
so nothing is missed unless a feed gets >25 new entries between polls).

## Troubleshooting

- **Nothing posts:** hit `/poll?token=…` manually; the JSON response shows a
  per-feed `seeded` / `new` count / error. Check Cloud Run logs.
- **`HTTP 429` errors:** reddit rate-limiting the Cloud Run egress IP —
  harmless if occasional; slow the scheduler if constant.
- **Duplicate cards:** shouldn't happen — the watermark + `notified_ids`
  dedupe. If Firestore state was wiped, the next run re-seeds silently.
- **Change subreddits/feeds:** re-run the Setup workflow with new inputs.
