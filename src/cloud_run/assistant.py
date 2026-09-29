"""The hourly pass: read new Chat messages, keep the board current, act for the owner.

For every space, group chat and DM whose lastActiveTime moved past its
watermark, fetch the new messages (plus a little earlier context), ask Gemini
for tasks and actions, apply them, and advance the watermark.

Acting is automatic, with guardrails, because replies go out under the
owner's name. An action runs unreviewed only when all of these hold:
  - auto-act is on (the pause switch in the UI)
  - it answers a NEW message from a human other than the owner
  - that message is directed at the owner and at most ACT_MAX_AGE_HOURS old
  - the owner hasn't already replied later in that thread
  - Gemini's confidence is at least ACT_CONFIDENCE
  - fewer than MAX_ACTIONS_PER_RUN actions have run this pass
Anything that fails a check becomes a suggestion with a Send button in the UI.
"""

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google_apis import Directory, GoogleApiError
from llm import build_prompt
from store import utcnow_iso

TIME_ZONE = os.environ.get("TIME_ZONE", "America/Toronto")
INITIAL_LOOKBACK_HOURS = float(os.environ.get("INITIAL_LOOKBACK_HOURS", "24"))
CONTEXT_HOURS = 24
CONTEXT_MESSAGES = 20
MAX_NEW_MESSAGES = int(os.environ.get("MAX_NEW_MESSAGES", "150"))
ACT_CONFIDENCE = float(os.environ.get("ACT_CONFIDENCE", "0.85"))
ACT_MAX_AGE_HOURS = float(os.environ.get("ACT_MAX_AGE_HOURS", "12"))
MAX_ACTIONS_PER_RUN = int(os.environ.get("MAX_ACTIONS_PER_RUN", "10"))
PARALLEL_SPACES = int(os.environ.get("PARALLEL_SPACES", "4"))

TZ = ZoneInfo(TIME_ZONE)


class ReconnectNeeded(RuntimeError):
    """The owner's Google token was revoked or expired; they must sign in again."""


def parse_ts(value):
    """RFC 3339 from Google (up to nanoseconds, trailing Z) -> aware datetime."""
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    if "." in value:
        head, rest = value.split(".", 1)
        digits = "".join(ch for ch in rest if ch.isdigit())
        value = f"{head}.{digits[:6].ljust(6, '0')}{rest[len(digits):]}"
    return datetime.fromisoformat(value)


def local(dt):
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def space_key(space_name):
    return space_name.split("/", 1)[1]


def stable_id(*parts):
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]


class Budget:
    def __init__(self, limit):
        self.left = limit
        self._lock = threading.Lock()

    def take(self):
        with self._lock:
            if self.left <= 0:
                return False
            self.left -= 1
            return True


# -- one conversation ------------------------------------------------------------

def _compact(message, directory, owner_ids):
    sender = message.get("sender") or {}
    mentions = [
        directory.name(a["userMention"]["user"]["name"])
        for a in message.get("annotations") or []
        if a.get("type") == "USER_MENTION" and a.get("userMention", {}).get("user", {}).get("name")
    ]
    text = message.get("text") or message.get("argumentText") or ""
    attachments = [a.get("contentName") or "attachment" for a in message.get("attachment") or []]
    return {
        "name": message["name"],
        "from": directory.name(sender.get("name", "")) if sender.get("type") != "BOT" else "(app/bot)",
        "from_me": sender.get("name") in owner_ids,
        "is_bot": sender.get("type") == "BOT",
        "time": local(parse_ts(message["createTime"])),
        "thread": (message.get("thread") or {}).get("name", ""),
        "mentions_me": any(
            a.get("userMention", {}).get("user", {}).get("name") in owner_ids
            for a in message.get("annotations") or []
        ),
        "mentions": mentions,
        "text": text[:4000],
        "attachments": attachments,
    }


def _label(google, space, directory, owner_ids, cache):
    if space.get("displayName"):
        return space["displayName"]
    if space["name"] in cache:
        return cache[space["name"]]
    try:
        members = google.list_members(space["name"])
        names = [directory.name(m["member"]["name"]) for m in members
                 if m.get("member", {}).get("type") == "HUMAN" and m["member"].get("name") not in owner_ids]
        label = ", ".join(names) or "(just you)"
    except GoogleApiError:
        label = space["name"]
    cache[space["name"]] = label
    return label


def process_space(ctx, space, since_iso):
    """Returns (new_watermark, stats). Raises on failure so the watermark stays put."""
    google, store, llm, directory, owner = ctx["google"], ctx["store"], ctx["llm"], ctx["directory"], ctx["owner"]
    name = space["name"]
    since = parse_ts(since_iso)

    new_raw = google.list_messages_since(name, since_iso, limit=MAX_NEW_MESSAGES)
    new_raw = [m for m in new_raw if not m.get("deleteTime")]
    stats = {"space": name, "messages": len(new_raw), "tasks_created": 0, "tasks_updated": 0,
             "actions_done": 0, "actions_suggested": 0, "actions_failed": 0, "skipped": ""}
    if not new_raw:
        return space.get("lastActiveTime") or since_iso, stats
    watermark = new_raw[-1]["createTime"]

    if all((m.get("sender") or {}).get("type") == "BOT" for m in new_raw) and not ctx["settings"].get("read_bot_posts"):
        stats["skipped"] = "only app/bot posts"
        return watermark, stats

    context_start = (since - timedelta(hours=CONTEXT_HOURS)).isoformat()
    context_raw = google.list_messages_before(name, context_start, since_iso, CONTEXT_MESSAGES)
    context_raw = [m for m in context_raw if not m.get("deleteTime")]

    owner_ids = owner["ids"]
    label = _label(google, space, directory, owner_ids, ctx["label_cache"])
    new = [_compact(m, directory, owner_ids) for m in new_raw]
    context = [_compact(m, directory, owner_ids) for m in context_raw]
    by_name = {m["name"]: m for m in new}
    raw_by_name = {m["name"]: m for m in new_raw}

    participants = {}
    for m in new_raw + context_raw:
        user = (m.get("sender") or {}).get("name", "")
        if user and (m.get("sender") or {}).get("type") == "HUMAN":
            participants[user] = {"name": directory.name(user), "email": directory.email(user)}

    key = space_key(name)
    open_tasks = [t for t in ctx["tasks_by_space"].get(key, []) if t.get("status") != "done"][:30]
    header = {"space": label, "type": space.get("spaceType"), "participants": list(participants.values())}
    prompt = build_prompt(owner, ctx["now_local"], TIME_ZONE,
                          {"header": header, "context": context, "new": new},
                          [{"task_id": t["id"], "title": t.get("title"), "status": t.get("status"),
                            "owner": t.get("owner"), "due": t.get("due"), "detail": t.get("detail", "")}
                           for t in open_tasks])
    result = llm.analyze(prompt)

    where = {"space": key, "space_name": name, "space_label": label, "space_uri": space.get("spaceUri", "")}
    known_messages = set(by_name) | {m["name"] for m in context}
    _apply_tasks(store, result["tasks"], open_tasks, where, known_messages, by_name, stats)
    _apply_actions(ctx, result["actions"], where, by_name, raw_by_name, new_raw, stats)
    return watermark, stats


def _apply_tasks(store, tasks, open_tasks, where, known_messages, by_name, stats):
    open_by_id = {t["id"]: t for t in open_tasks}
    now = utcnow_iso()
    for t in tasks:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        fields = {
            "title": title[:200],
            "detail": (t.get("detail") or "")[:2000],
            "owner": (t.get("owner") or "")[:100],
            "owner_is_me": bool(t.get("owner_is_me")),
            "due": (t.get("due") or "")[:10],
            "priority": t.get("priority") if t.get("priority") in ("high", "medium", "low") else "medium",
            "status": t.get("status") if t.get("status") in ("todo", "in_progress", "done") else "todo",
            "updated_at": now,
        }
        existing = open_by_id.get(t.get("task_id") or "")
        if existing:
            if existing.get("user_edited"):
                continue  # the owner's own edits win over the assistant's
            store.save_task(existing["id"], fields)
            stats["tasks_updated"] += 1
            continue
        source = t.get("source_message") if t.get("source_message") in known_messages else ""
        task_id = stable_id(where["space"], source, title.lower())
        if store.get_task(task_id):
            continue
        excerpt = by_name.get(source, {})
        store.save_task(task_id, {
            **fields, **where,
            "source_message": source,
            "source_excerpt": excerpt.get("text", "")[:300],
            "source_from": excerpt.get("from", ""),
            "origin": "assistant",
            "user_edited": False,
            "created_at": now,
        })
        stats["tasks_created"] += 1


def _answered_later(new_raw, source, owner_ids):
    """True if the owner already posted in the source message's thread after it."""
    thread = (source.get("thread") or {}).get("name")
    after = parse_ts(source["createTime"])
    return any((m.get("sender") or {}).get("name") in owner_ids
               and (m.get("thread") or {}).get("name") == thread
               and parse_ts(m["createTime"]) > after
               for m in new_raw)


def _apply_actions(ctx, actions, where, by_name, raw_by_name, new_raw, stats):
    store, settings, directory, budget = ctx["store"], ctx["settings"], ctx["directory"], ctx["budget"]
    for a in actions:
        msg = by_name.get(a.get("source_message") or "")
        if not msg or msg["from_me"] or msg["is_bot"]:
            continue  # only ever answer a new message from another person
        kind = a.get("type")
        action_id = stable_id(kind or "", msg["name"])
        if store.get_action(action_id):
            continue
        record = {
            **where,
            "type": kind,
            "source_message": msg["name"],
            "source_excerpt": msg["text"][:300],
            "source_from": msg["from"],
            "thread": msg["thread"],
            "reason": (a.get("reason") or "")[:500],
            "confidence": float(a.get("confidence") or 0),
            "created_at": utcnow_iso(),
        }
        if kind == "chat_reply":
            text = (a.get("reply_text") or "").strip()
            if not text:
                continue
            if _answered_later(new_raw, raw_by_name[msg["name"]], ctx["owner"]["ids"]):
                continue
            record["reply_text"] = text[:4000]
        elif kind == "calendar_event":
            event = _event_fields(a, directory)
            if not event:
                continue
            record.update(event)
        else:
            continue

        blocked = _blocked_reason(a, raw_by_name[msg["name"]], settings, ctx["now"])
        if not blocked and not budget.take():
            blocked = f"hourly limit of {MAX_ACTIONS_PER_RUN} automatic actions reached"
        if blocked:
            store.save_action(action_id, {**record, "status": "suggested", "blocked_reason": blocked})
            stats["actions_suggested"] += 1
            continue
        outcome = execute_action(ctx["google"], record)
        store.save_action(action_id, outcome)
        stats["actions_done" if outcome["status"] == "done" else "actions_failed"] += 1


def _blocked_reason(action, raw_message, settings, now):
    if not settings.get("auto_act", True):
        return "automatic actions are paused"
    if not action.get("directed_at_me"):
        return "not clearly directed at you"
    confidence = float(action.get("confidence") or 0)
    if confidence < ACT_CONFIDENCE:
        return f"confidence {confidence:.2f} is below {ACT_CONFIDENCE:.2f}"
    age_hours = (now - parse_ts(raw_message["createTime"])).total_seconds() / 3600
    if age_hours > ACT_MAX_AGE_HOURS:
        return f"message is {age_hours:.0f}h old (limit {ACT_MAX_AGE_HOURS:.0f}h)"
    return ""


def _event_fields(action, directory):
    try:
        start = datetime.fromisoformat(action.get("event_start") or "")
    except ValueError:
        return None
    try:
        end = datetime.fromisoformat(action.get("event_end") or "")
    except ValueError:
        end = start + timedelta(minutes=30)
    if end <= start:
        end = start + timedelta(minutes=30)
    known = directory.known_emails()
    attendees = sorted({e.strip().lower() for e in action.get("event_attendees") or [] if e.strip().lower() in known})
    title = (action.get("event_title") or "").strip()
    if not title:
        return None
    fmt = "%Y-%m-%dT%H:%M:%S"
    return {"event_title": title[:200], "event_start": start.replace(tzinfo=None).strftime(fmt),
            "event_end": end.replace(tzinfo=None).strftime(fmt), "event_attendees": attendees}


def execute_action(google, record):
    """Perform a reply or calendar event; returns the record with its outcome."""
    record = dict(record)
    try:
        if record["type"] == "chat_reply":
            sent = google.reply_in_thread(record["space_name"], record.get("thread"), record["reply_text"])
            record["result"] = sent.get("name", "")
        else:
            desc = f"Created by Chat Assistant from \"{record.get('space_label')}\": {record.get('source_excerpt', '')}"
            event = google.create_event(record["event_title"], record["event_start"], record["event_end"],
                                        TIME_ZONE, record.get("event_attendees", []), desc)
            record["result"] = event.get("htmlLink", "")
        record["status"] = "done"
    except Exception as exc:  # noqa: BLE001 - recorded for the activity log
        record["status"] = "failed"
        record["error"] = str(exc)[:500]
    record["executed_at"] = utcnow_iso()
    return record


def _resolve_owner(google, spaces, directory, owner):
    """Pin down every id that means "the owner", so the assistant never answers itself.

    Chat is asked directly (membership lookup by email alias) rather than trusting
    that the sign-in's account id equals the Chat id; directory entries with the
    owner's email count too.
    """
    ids = {owner["user"]}
    for space in spaces[:5]:
        try:
            chat_id = google.chat_user_id(space["name"], owner["email"])
        except GoogleApiError:
            continue
        if chat_id:
            ids.add(chat_id)
            break
    ids |= {user for user, entry in directory.entries.items() if entry.get("email") == owner["email"].lower()}
    primary = next((i for i in ids if i in directory.entries), owner["user"])
    name = directory.name(primary) if primary in directory.entries else owner["email"]
    return {**owner, "user": primary, "ids": ids, "name": name}


# -- the whole pass ---------------------------------------------------------------

def run(store, google, llm, now=None):
    now = now or datetime.now(timezone.utc)
    summary = {"started_at": now.isoformat(), "spaces_total": 0, "spaces_processed": 0, "messages": 0,
               "tasks_created": 0, "tasks_updated": 0, "actions_done": 0, "actions_suggested": 0,
               "actions_failed": 0, "errors": []}
    owner = store.get_owner()
    if not owner:
        raise ReconnectNeeded("no owner has signed in yet")

    from google.auth.exceptions import RefreshError

    try:
        spaces = google.list_spaces()
    except RefreshError as exc:
        raise ReconnectNeeded(f"Google sign-in expired or was revoked: {exc}") from exc
    except GoogleApiError as exc:
        if exc.status in (401, 403):
            raise ReconnectNeeded(str(exc)) from exc
        raise
    try:
        directory = google.load_directory()
    except GoogleApiError as exc:
        directory = Directory()
        summary["errors"].append({"space": "(directory)", "error": str(exc)[:300]})
    owner = _resolve_owner(google, spaces, directory, owner)
    store.set_owner({"user": owner["user"], "email": owner["email"]})

    watermarks = store.get_watermarks()
    default_since = (now - timedelta(hours=INITIAL_LOOKBACK_HOURS)).isoformat()
    due = []
    for space in spaces:
        since = watermarks.get(space_key(space["name"]), default_since)
        last = parse_ts(space.get("lastActiveTime"))
        if last and last > parse_ts(since):
            due.append((space, since))
    summary["spaces_total"] = len(spaces)

    tasks_by_space = {}
    for t in store.list_tasks():
        tasks_by_space.setdefault(t.get("space", ""), []).append(t)

    ctx = {"google": google, "store": store, "llm": llm, "directory": directory, "owner": owner,
           "settings": store.get_settings(), "now": now, "now_local": now.astimezone(TZ).strftime("%A %Y-%m-%d %H:%M"),
           "budget": Budget(MAX_ACTIONS_PER_RUN), "tasks_by_space": tasks_by_space, "label_cache": {}}

    with ThreadPoolExecutor(max_workers=PARALLEL_SPACES) as pool:
        futures = {pool.submit(process_space, ctx, s, since): s for s, since in due}
        for fut in as_completed(futures):
            space = futures[fut]
            try:
                watermark, stats = fut.result()
            except Exception as exc:  # noqa: BLE001 - one bad space must not stop the rest
                label = space.get("displayName") or space["name"]
                summary["errors"].append({"space": label, "error": str(exc)[:300]})
                continue
            store.set_watermarks({space_key(space["name"]): watermark})
            summary["spaces_processed"] += 1
            for k in ("messages", "tasks_created", "tasks_updated", "actions_done", "actions_suggested",
                      "actions_failed"):
                summary[k] += stats[k]

    summary["finished_at"] = utcnow_iso()
    return summary
