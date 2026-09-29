"""Gemini (Vertex AI) reads one conversation and returns tasks and actions as JSON.

Same call pattern as trustpilot-pipeline: Vertex generateContent with the
runtime service account's Application Default Credentials, no API key. The
response is constrained to RESPONSE_SCHEMA, so parsing never depends on the
model's formatting.
"""

import json
import os

import requests

GCP_PROJECT = os.environ.get("GCP_PROJECT", "shp-ai-bot-2026")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")

_S, _N, _B = {"type": "STRING"}, {"type": "NUMBER"}, {"type": "BOOLEAN"}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "tasks": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "task_id": {**_S, "description": "Existing task id to update, or empty for a new task"},
                    "title": _S,
                    "detail": _S,
                    "owner": {**_S, "description": "Name of the person responsible"},
                    "owner_is_me": _B,
                    "due": {**_S, "description": "YYYY-MM-DD, or empty"},
                    "priority": {"type": "STRING", "enum": ["high", "medium", "low"]},
                    "status": {"type": "STRING", "enum": ["todo", "in_progress", "done"]},
                    "source_message": {**_S, "description": "name of the message this comes from"},
                },
                "required": ["task_id", "title", "owner", "owner_is_me", "priority", "status", "source_message"],
            },
        },
        "actions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "type": {"type": "STRING", "enum": ["chat_reply", "calendar_event"]},
                    "source_message": _S,
                    "directed_at_me": _B,
                    "confidence": {**_N, "description": "0 to 1: how sure you are this is right and safe to do unreviewed"},
                    "reason": _S,
                    "reply_text": _S,
                    "event_title": _S,
                    "event_start": {**_S, "description": "Local datetime YYYY-MM-DDTHH:MM:SS"},
                    "event_end": _S,
                    "event_attendees": {"type": "ARRAY", "items": _S},
                },
                "required": ["type", "source_message", "directed_at_me", "confidence", "reason"],
            },
        },
        "project": {
            "type": "OBJECT",
            "description": "The project this conversation is about, if it is one",
            "properties": {
                "is_project": {**_B, "description": "False for day-to-day channels, notifications, social chat"},
                "project_id": {**_S, "description": "Id from KNOWN PROJECTS if it matches one, else empty"},
                "name": _S,
                "summary": {**_S, "description": "One or two sentences: goal and current state"},
                "status": {"type": "STRING", "enum": ["active", "paused", "blocked", "done", "unknown"]},
                "owner": _S,
            },
            "required": ["is_project"],
        },
        "facts": {"type": "ARRAY", "description": "Up to 3 durable facts worth remembering (not tasks)",
                  "items": _S},
        "questions": {"type": "ARRAY", "description": "Up to 2 questions for the owner about important gaps",
                      "items": _S},
    },
    "required": ["tasks", "actions"],
}

INSTRUCTIONS = """\
You are the personal assistant of {owner_name} ({owner_email}) at Superhairpieces, a
hairpiece e-commerce company with GTA salons. The team runs on Google Chat. You read one
conversation at a time and keep {owner_name}'s project board current, and you act for them.

Today is {now_local} ({time_zone}).

TASKS
- Extract concrete work items from the NEW messages: requests, commitments, follow-ups,
  deadlines. Include work {owner_name} owes others and work others owe {owner_name} or the
  team. Skip greetings, FYIs, chit-chat and anything already finished.
- To change an existing open task (status, due date, owner, detail), return it with its
  task_id. Mark it "done" only when the conversation shows it was completed.
- One task per distinct piece of work. Short imperative titles. Due dates only when stated
  or clearly implied (resolve "tomorrow", "Friday" against today's date).

ACTIONS (things you do as {owner_name}, sent under their name)
- chat_reply: only when a NEW message from someone else is directed at {owner_name} (a DM,
  an @mention, or a question clearly addressed to them) and a reply from them is expected.
  Write in {owner_name}'s voice: brief, warm, professional, same language as the thread.
  Good replies: acknowledging a request and saying it's on the board, confirming a meeting,
  answering from facts stated in this conversation.
- calendar_event: only when a meeting with a specific date and time was agreed in the new
  messages and {owner_name} is expected to attend. Attendees are the emails of the people
  involved, from the participant list.
- Never promise money, prices, discounts, refunds, deadlines, hiring or legal outcomes that
  {owner_name} hasn't stated. Never invent facts. Never reply to bots or to {owner_name}'s
  own messages, or when {owner_name} already answered.
- confidence: 0.9+ only when the action is clearly right and harmless to send unreviewed.
  If anything is ambiguous, use a low confidence; it goes to {owner_name} for approval.
- No action is a fine answer. Most conversations need none.

LEARNING (you are building a picture of every project in both companies)
- project: say whether this conversation is about a project (a piece of work with a goal) or a day-to-day
  channel. If it matches one in KNOWN PROJECTS, give its project_id; otherwise name it.
- facts: at most 3 durable things worth remembering (who owns what, decisions, how a process works,
  which systems are used). Skip anything already known and anything transient.
- questions: at most 2 questions for {owner_name} about important gaps you can't answer from the chat.

Refer to messages by their "name" field exactly as given.
"""


def build_prompt(owner, now_local, time_zone, conversation, open_tasks, known_projects=""):
    head = INSTRUCTIONS.format(owner_name=owner["name"], owner_email=owner["email"],
                               now_local=now_local, time_zone=time_zone)
    return (
        f"{head}\n"
        f"KNOWN {known_projects or 'PROJECTS: none yet'}\n\n"
        f"CONVERSATION\n{json.dumps(conversation['header'], ensure_ascii=False)}\n\n"
        f"EARLIER MESSAGES (context only; already processed)\n"
        f"{json.dumps(conversation['context'], ensure_ascii=False, indent=1)}\n\n"
        f"NEW MESSAGES (act on these)\n"
        f"{json.dumps(conversation['new'], ensure_ascii=False, indent=1)}\n\n"
        f"OPEN TASKS FROM THIS CONVERSATION\n{json.dumps(open_tasks, ensure_ascii=False, indent=1)}\n"
    )


class Gemini:
    def __init__(self):
        import google.auth

        self._creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self._url = (f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT}"
                     f"/locations/{VERTEX_LOCATION}/publishers/google/models/{GEMINI_MODEL}:generateContent")

    def analyze(self, prompt):
        from google.auth.transport.requests import Request

        if not self._creds.valid:
            self._creds.refresh(Request())
        resp = requests.post(
            self._url,
            headers={"Authorization": f"Bearer {self._creds.token}"},
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "responseMimeType": "application/json",
                    "responseSchema": RESPONSE_SCHEMA,
                },
            },
            timeout=240,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
        parts = resp.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        result = json.loads(text)
        return {"tasks": result.get("tasks") or [], "actions": result.get("actions") or [],
                "project": result.get("project") or {}, "facts": result.get("facts") or [],
                "questions": result.get("questions") or []}
