# Company Assistant

The chat-assistant service is growing from "read Chat, keep a board" into an AI
operator for Superhairpieces and Gen'C Beauty: it learns every project, gets
access to the company's systems, does the work, and reports back. You talk to it
in text or live voice, alongside two AI partners, Claude and ChatGPT.

This file covers what Phase 1 added. For deployment, run `setup.sh` once as a
project Owner (see its header) and deploy with the *Deploy chat assistant to
Cloud Run* workflow.

## Phase 1: learn the company, talk to it

| Tab | What it's for |
|---|---|
| **Talk** | Chat with the **Assistant** (Gemini), **Claude** or **ChatGPT**. Each one has the same company background, knowledge base and tools, and its own conversation history. **🎙 Talk** starts a live voice call with the Assistant. |
| **Board** | Tasks from your chats and conversations (unchanged). |
| **Projects** | Every project it knows, per company: goal, owner, status, deadline, next steps, linked chats and open tasks. |
| **Questions** | The interview: what it needs to know, highest priority first. Answer in the box, in Talk, or by voice; each answer is processed into projects, facts, tasks and follow-up questions. |
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
actions per run, and **Act automatically** in the header pauses everything. In Talk
or voice, it sends a Chat message only after you approve the exact text.

## AI partners

| Partner | How it's called | Setup |
|---|---|---|
| Assistant | Gemini (`TALK_MODEL`, default `gemini-2.5-pro`) on Vertex AI | none |
| Claude | Claude Opus 5.5 (`claude-opus-5-5`) on Vertex AI via the Anthropic SDK, with automatic refusal fallback to Claude Opus 4.8 | Enable **Claude Opus 5.5** and **Claude Opus 4.8** in Vertex AI Model Garden for `shp-ai-bot-2026` |
| ChatGPT | OpenAI SDK, model from Settings (default `gpt-5`) | Re-run `setup.sh` so the app can read the `CHATGPT_API_KEY` secret |

The Assistant can consult either partner (`consult_partner`), for example for a
second opinion on a plan. A consulted partner can use the knowledge tools but can't
send messages or change settings. Talking to Claude or ChatGPT sends company context
to Anthropic or OpenAI; **Let Claude and ChatGPT work with company information** in
Settings switches that off. **Test partners** in Settings checks all three.

To use the Claude API with a key instead of Vertex AI, store it as the
`ANTHROPIC_API_KEY` secret, re-run `setup.sh`, and set `CLAUDE_BACKEND=anthropic`.

## Live voice

**🎙 Talk** (Assistant only) opens a WebSocket to `/ws/voice`. The browser sends
16 kHz 16-bit PCM from the microphone (with echo cancellation), the service relays
it to **Gemini 3.8 Live** (`gemini-3.8-live` on Vertex AI), and streams the spoken
answer back at 24 kHz. You can interrupt it; the transcript appears live and is
saved into the Assistant's Talk conversation. During a call it uses the same tools as
text: it can look things up, record what you tell it, and queue or answer questions.
Use Chrome or Edge. Only your signed-in session, from the app's own page, can open
the voice socket.

## Configuration

| Env var | Default |
|---|---|
| `TALK_MODEL` | `gemini-2.5-pro` (falls back to `GEMINI_MODEL`) |
| `LIVE_MODEL` / `LIVE_VOICE` / `LIVE_LOCATION` | `gemini-3.8-live` / model default / `us-central1` |
| `CLAUDE_MODEL` / `CLAUDE_FALLBACK_MODEL` | `claude-opus-5-5` / `claude-opus-4-8` |
| `CLAUDE_BACKEND` / `CLAUDE_REGION` / `CLAUDE_EFFORT` | `vertex` / `global` / `medium` |
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
