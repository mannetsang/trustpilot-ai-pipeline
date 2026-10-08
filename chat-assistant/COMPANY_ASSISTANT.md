# Man AI

Man AI (formerly Company Assistant; the Cloud Run service is still `chat-assistant`) is growing from "read Chat, keep a board" into an AI
operator for Superhairpieces and Gen'C Beauty: it learns every project, gets
access to the company's systems, does the work, and reports back. You talk to it
in text or live voice, alongside two AI partners, Claude and ChatGPT.

**The chat** isn't a tab: it's a window at the bottom right, over every tab, so you can talk while you look at
the board or the Access list. **–** (or Esc) minimizes it to a **💬 Man AI** button. The button gets a red dot
when a reply arrives while it's minimized, and reads **On a call** while a voice call is running (a call keeps
going while it's minimized). Open or minimized is remembered per browser; the first time, it starts open on a
computer and minimized on a phone, where the open chat fills the screen.

This file covers what Phase 1 added. For deployment, run `setup.sh` once as a
project Owner (see its header) and deploy with the *Deploy chat assistant to
Cloud Run* workflow.

## Phase 1: learn the company, talk to it

| Where | What it's for |
|---|---|
| **Chat** (bottom right) | Chat with **Man AI** (Gemini), **Claude** or **ChatGPT**. Each one has the same company background, knowledge base and tools, and its own conversation history. **🎙 Talk** starts a live voice call with whichever of the three is selected. **🔊** on a reply reads it aloud (press again to stop); **🔊 Read aloud** in the chat's header reads each new typed reply automatically. |
| **Board** | Tasks from your chats and conversations (unchanged). |
| **Projects** | Every project it knows, per company: goal, owner, status, deadline, next steps, linked chats and open tasks. |
| **Questions** | The interview: what it needs to know, highest priority first. Answer in the box, in the chat (**Discuss in chat**), or by voice; each answer is processed into projects, facts, tasks and follow-up questions. |
| **Access** | Systems it can use now, those with credentials that aren't connected yet, and those it still needs. **How do I connect this?** asks it to walk you through one. |
| **Activity** | Actions taken and waiting for approval, and the run history (now including what each run learned). |

### How it learns

- **Company background**: `src/cloud_run/knowledge/company.md`, copied from the repo's
  `CLAUDE.md` (personas, salons, staff, teams, stores). Update both when it changes.
- **Every hourly run** now also decides, per conversation, whether it's a project
  (creating or updating it and linking the chat), records up to 3 durable facts, and
  queues up to 2 questions. Tasks it creates are linked to that project.
- **Conversations and answers**: the models record what they learn with tools
  (`save_project`, `record_fact`, `ask_owner`, `request_access`, ...).
- It starts with the 8 round-1 questions and the known systems list, seeded once.

### What it may do on its own (tiered autonomy)

Settings (⚙) sets each category to **Auto** (act and log it) or **Ask** (propose in
Activity and wait). Defaults:

| Category | Default |
|---|---|
| Messages to colleagues in Google Chat | Auto |
| Calendar events and invites | Auto |
| Messages to customers or anyone outside the company | Ask |
| Refunds, prices, discounts, ad budgets, payments | Ask |
| Staff, schedules, hiring, HR | Ask |
| Deleting or overwriting data | Ask |

A reply in a conversation with anyone outside the company directory counts as a
customer message. The earlier guardrails still apply on top: only recent messages
aimed at you, not already answered, at confidence ≥ 0.85, at most 10 automatic
actions per run, and **Act automatically** in the header pauses everything. In the chat
or voice, it sends a Chat message only after you approve the exact text.

It can message a colleague directly: "Tell Sydney the tape arrives Thursday." It finds the person in the
company directory by name or email (and asks which one if two match), shows you the text and who it goes to,
and after your OK posts in your direct message with them, starting one if you've never messaged them.
Starting a direct message needs Google's `chat.spaces.create` permission; a sign-in from before it was added
shows a banner, and one **Reconnect** adds it.

## AI partners

| Partner | How it's called | Setup |
|---|---|---|
| Assistant | Gemini (`TALK_MODEL`, default `gemini-2.5-pro`) on Vertex AI | none |
| Claude | Claude Opus 5.5 (`claude-opus-5-5`) via the Anthropic SDK, with automatic refusal fallback to Claude Opus 4.8. Tries the Claude API with the `ANTHROPIC_API_KEY` secret first, then Vertex AI | Either: credit on the Anthropic account behind `ANTHROPIC_API_KEY` plus Secret Accessor on it for the app's service account, or Claude quota on Vertex AI for `shp-ai-bot-2026` (IAM & Admin > Quotas, `global_online_prediction_requests_per_base_model`, base model `anthropic-claude-opus`) |
| ChatGPT | OpenAI SDK, model from Settings (default `gpt-5`) | Re-run `setup.sh` so the app can read the `CHATGPT_API_KEY` secret |

The Assistant can consult either partner (`consult_partner`), for example for a
second opinion on a plan. A consulted partner can use the knowledge tools but can't
send messages or change settings. Talking to Claude or ChatGPT sends company context
to Anthropic or OpenAI; **Let Claude and ChatGPT work with company information** in
Settings switches that off. **Test partners** in Settings checks all three.

With `CLAUDE_BACKEND=auto` (the default) a route that can't serve at all (no quota,
no credit, no readable key) is skipped, and when neither works the error says what
each needs. `CLAUDE_BACKEND=anthropic` or `=vertex` pins one route.

## Live voice

**🎙 Talk** opens a WebSocket to `/ws/voice?partner=…`. The browser sends
16 kHz 16-bit PCM from the microphone (with echo cancellation), the service relays
it to **Gemini 3.8 Live** (`gemini-3.8-live` on Vertex AI), and streams the spoken
answer back at 24 kHz. You can interrupt it; the transcript appears live and is
saved into Man AI's chat conversation. During a call it uses the same tools as
text: it can look things up, record what you tell it, and queue or answer questions.
Use Chrome or Edge. Only your signed-in session, from the app's own page, can open
the voice socket.

Each bot has one voice, on calls and when its replies are read aloud: the Assistant
**Kore** (Gemini), Claude **cedar** and ChatGPT **marin** (both OpenAI). Hovering over a
partner's button names the models behind it, and a call's bar names the model on the
line ("Listening (gpt-realtime-2.1)").

**ChatGPT** calls work the same way as the Assistant's, on OpenAI's own live model
(`gpt-realtime-2.1` through the Realtime API, with the `CHATGPT_API_KEY` secret):
ChatGPT hears your audio directly, answers in its own voice, and uses the same tools,
company background and conversation history as typed ChatGPT. The on-screen
transcript is primed with staff, salon, project and system names. Turns are saved
into the ChatGPT conversation. OpenAI bills live audio per minute, noticeably more
than typed chat. `CHATGPT_VOICE=relay` switches ChatGPT to the relay below instead.

A turn on OpenAI's line ends after 1.2 s of silence (`OPENAI_TURN_DETECTION`). Measured
with real speech, it acts 2.2-3.1 s after you stop and never split a sentence: a
longer pause mid-sentence starts an answer, which gives way as soon as you carry on.
OpenAI's "semantic" detection took up to 9 s at its most patient setting and split
sentences at its fastest.

**Claude** has no audio input in Anthropic's API, so a call with Claude uses a live
voice line only as ears and voice: by default ChatGPT's (`CLAUDE_VOICE=realtime`),
which hears the audio itself, or Gemini's (`CLAUDE_VOICE=gemini`). The line gets a
single tool (`ask_claude`) that passes your words to Claude, which answers with its
own model, tools and conversation history (in a spoken style); the line says "One
moment." and then reads Claude's answer out word for word (99% in tests). Claude saves
those turns into its own conversation. The call shows "Claude is thinking…" while it
works. The line is given the company's names and terms, so "SkuVault", "Gen'C Beauty"
and staff names reach Claude spelled right, and Claude is told the words were spoken,
so it reads misheard names charitably. On Gemini's line a turn ends after a 1.5 s pause
(`RELAY_END_SILENCE_MS`; Gemini's default split a request at a 0.9 s pause).

## Working on its own

Every partner is told to use its access before answering: look things up in the
connected systems (`list_integrations`, `call_api`), research the web (`web_search`,
`read_webpage`, through the stored Firecrawl key), and chain up to 24 tool calls per
answer. Research APIs: Firecrawl (search, read pages) and DataForSEO (SERPs, keyword
volumes, rankings; each call costs a few cents).

**Board → a task → 🤖 Give to Man AI** makes the assistant the task's owner and has
it work the task right away (`worker.py`); each hourly run works up to `WORK_PER_RUN`
(3) of its open tasks again, any it hasn't touched for six hours. It works with every
tool, but alone it can't change data or message anyone (it prepares the exact change
or text and asks for your OK). When a task is finished it marks it done. Its report
appears on the task (and a 🤖 report badge on the card) and in Man AI's chat
conversation.

## Company systems (connected for real)

Every partner (typed or on a call) has two tools for company systems:
`list_integrations` (which systems, whether each is ready, useful paths) and
`call_api` (one request). Wired up (`integrations.py`):

| How it connects | Systems |
|---|---|
| Key already in Secret Manager | BigCommerce superhairpieces.ca (`gmosz3ja`), superhairpieces.es (`qet21urb3p`) and Gen'C Beauty (`kzkmuqjqk9`), Airtable, Trustpilot, Stamped.io, Omnisend, Notion, Figma, TeamDesk (database 56554, as trustpilot-pipeline uses); SkuVault, and Amazon seller account 1 (stored logins and tokens are exchanged for access tokens on the server; Amazon's region is found from the token) |
| Paste a key in the app | BigCommerce superhairpieces.com (`cavofu`), .fr (`1tqjsol232`), .nl (`1f8t0plkkw`) and .de (`34amlu9gm`): no token was stored; Connect asks for it once, then it's in Secret Manager ; Amazon seller accounts 2 and 3 (a refresh token each, same app); Walmart Canada (the Consumer Channel Type ID; its consumer id and private key are stored, and every request is signed) |
| Your Google sign-in | Gmail (read, drafts), Drive and Sheets (read), Google Analytics and Search Console (read), Merchant Center (Merchant API v1, accounts as in CLAUDE.md) |

Trustpilot uses business unit `5e44f707d7d8c700011eaa10` and, when `TRUSTPILOT_API_SECRET`
is readable, the business-user sign-in trustpilot-pipeline uses (private endpoints:
reviewer details, replies); without it, the public API.

**Access → Connect everything** (or Connect on one row) asks Google once for your OK
as the project's owner. With that one-hour token, which is used right away and never
stored (`cloud_setup.py`), the app gives its own service account read access to each
secret it needs, one secret at a time and never project-wide; creates an empty secret
for any connector whose key you paste in the app (none right now: HubSpot and Re:amaze
were removed until needed); and switches on the Analytics, Search Console and Merchant
APIs. It then tests every system (Google can take a minute to apply new
access, so it re-checks) and marks each Connected. A **paste** row opens a form with
the steps for finding the key; what you paste goes straight into Secret Manager and is
tested at once. A **Google** row reconnects your Google sign-in with those permissions.

The assistant reads chats written by many people, so the design assumes someone may
try to talk it into misusing a key:

- Key values never reach a model. The server adds them to each request (logins are
  exchanged for tokens on the server) and scrubs them from anything a system returns.
- Each system has one pinned address; a path can't send a request (or its key)
  anywhere else.
- Reads run at once. Anything that changes data needs `confirmed=true`, which a model
  may set only after you agreed to that exact change in the conversation; a
  consulted partner can't confirm at all. Confirmed changes are listed under Activity.

Not wired up yet: Meta/Instagram, Google Ads (needs a developer token), accounting,
the .com and EU BigCommerce stores (unless `qet21urb3p` turns out to be one of them),
Walmart, and the MySQL database.

## Web browser

Man AI can open web pages in a real browser and **see** them: every `browser` call does one action (open,
click, type, press a key, pick an option, scroll, back, screenshot) and shows the model a screenshot of the
result, plus what's clickable (label and position) and the visible text. Use it for how a page looks or works:
layout, images, pop-ups, menus, the mobile view (`device=mobile`), search, product options, checkout steps,
competitors' sites. All three models see the screenshots (Gemini and ChatGPT as images after the tool result,
Claude inside it); on a live call Gemini Flash describes the screenshot instead.

The browser is its own Cloud Run service, `web-browser` (`src/browser/`): Playwright's Chromium, one instance at
most (the open tabs live in its memory, closed after 10 idle minutes; at most 4), scaled to zero when unused.
It runs as `web-browser@shp-ai-bot-2026.iam.gserviceaccount.com`, which holds **no** roles, and only Man AI's
service account may call it (Cloud Run IAM; Man AI sends an ID token). Man AI finds it through `BROWSER_URL`;
without it the tool isn't offered.

Guards in the service itself, whatever the model asks:
- only http(s) to public addresses: localhost, private and link-local ranges (the metadata server), and
  `*.internal` / `*.local` are blocked for the page and every request it makes, names resolving to them too;
- no typing into password or payment-card fields; no downloads, file pickers or device permissions; dialogs
  are dismissed (and reported).

And in Man AI: working alone on a task it may look and click but not type, press keys or pick options. The
model is told never to log in, enter personal or payment details, place orders or send a form without your
OK; that rule is an instruction, not a technical block (Manne accepted this risk on 2026-10-07).

Deploy (once the `web-browser` service account exists):

    gcloud run deploy web-browser --source chat-assistant/src/browser --region us-central1 --project shp-ai-bot-2026 --service-account web-browser@shp-ai-bot-2026.iam.gserviceaccount.com --no-allow-unauthenticated --memory 2Gi --cpu 2 --concurrency 8 --max-instances 1 --timeout 120
    gcloud run services add-iam-policy-binding web-browser --region us-central1 --project shp-ai-bot-2026 --member serviceAccount:chat-assistant@shp-ai-bot-2026.iam.gserviceaccount.com --role roles/run.invoker

then deploy Man AI with `BROWSER_URL=<the web-browser URL>` added to `--set-env-vars`.

## Read aloud

**🔊** on any reply, or the **Read replies aloud** switch (remembered in this browser),
speaks a typed reply out loud in that bot's call voice: the Assistant with Gemini
text-to-speech on Vertex AI (`gemini-2.5-flash-tts`), Claude and ChatGPT with OpenAI's
(`gpt-4o-mini-tts`). If one provider fails the other reads instead, except for
ChatGPT: clicking ChatGPT means OpenAI's models only, so its reading fails with a
message rather than switch to Gemini. Speech is
streamed, so it starts in about two seconds whatever the length; markdown and links
are cleaned out first. A finished reading is cached, so replaying it is free.
Starting a voice call or switching partners stops a reading.

## Configuration

| Env var | Default |
|---|---|
| `TALK_MODEL` | `gemini-2.5-pro` (falls back to `GEMINI_MODEL`) |
| `LIVE_MODEL` / `LIVE_LOCATION` | `gemini-3.8-live` / `us-central1` |
| `RELAY_END_SILENCE_MS` | `1500` (pause that ends your turn in a relayed call) |
| `CHATGPT_VOICE` / `CLAUDE_VOICE` | `realtime` / `realtime` (OpenAI's line; `relay` / `gemini` for Gemini's) |
| `OPENAI_TURN_DETECTION` | `server:1200` (or `semantic:low`..`high`) |
| `OPENAI_LIVE_MODEL` / `OPENAI_LIVE_REASONING` | `gpt-realtime-2.1` / `low` |
| `OPENAI_TRANSCRIBE_MODEL` | `gpt-4o-transcribe` (on-screen transcript of ChatGPT calls) |
| `GEMINI_TTS_MODEL` / `OPENAI_TTS_MODEL` | `gemini-2.5-flash-tts` / `gpt-4o-mini-tts` (read aloud) |
| `VOICE_ASSISTANT` / `VOICE_CLAUDE` / `VOICE_CHATGPT` | `Kore` / `cedar` / `marin` (calls and read aloud) |
| `CLAUDE_MODEL` / `CLAUDE_FALLBACK_MODEL` | `claude-opus-5-5` / `claude-opus-4-8` |
| `CLAUDE_BACKEND` / `CLAUDE_REGION` / `CLAUDE_EFFORT` | `auto` / `global` / `medium` |
| `OPENAI_MODEL` / `OPENAI_KEY_SECRET` | `gpt-5` / `CHATGPT_API_KEY` |

## Next: Phase 2, working on its own

Integrations, one system at a time in the order the interview makes most useful
(each needs code in this service, since the assistant runs inside the app), then a
per-project work loop (plan, do, report) and monitors with alerts.

## Tests

`python -m unittest discover -s chat-assistant/tests` runs everything offline with a
fake Google, fake models and a scripted Gemini Live session: the knowledge base,
autonomy tiers, learning from chats, every tool, the partners, the interview, and the
voice bridge's audio, transcript and tool-call path.
