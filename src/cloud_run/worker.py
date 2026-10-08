"""The assistant working board tasks on its own.

A task whose owner is "Assistant" (the board's "Give to assistant" button, or the
assistant assigning itself) gets worked: right away when it's handed over, and
again in each hourly run until it's done. The assistant uses every tool it has:
company systems (call_api), the web (web_search, read_webpage), chats, calendar
and the knowledge base.

No one is in the conversation while it works, so it can't change data in a system
or message anyone (the toolset's may_change is off): it prepares the exact change
or text and asks for Manne's OK in its report. The report goes on the task and into
the Assistant's chat conversation.
"""

import os
from datetime import datetime, timedelta, timezone

from store import utcnow_iso

OWNER = "Assistant"
WORK_PER_RUN = int(os.environ.get("WORK_PER_RUN", "3"))   # tasks the hourly run works, at most
REWORK_HOURS = 6        # a task in progress is picked up again after this long
BUSY_MINUTES = 15       # one task isn't worked twice at the same time

PROMPT = """\
You're working on a board task on your own. No one else is in this conversation.

Task: {title}
{detail}Priority: {priority}. Due: {due}. Status: {status}.{project}{previous}

Do as much of it as you can with your tools, for real: look things up in company systems (list_integrations,
then call_api), research the web (web_search, read_webpage, and browser to see and click through a page), read
the relevant chats, check the calendar and
the knowledge base. Chain as many calls as it takes.

This is your whole turn: do the work now and deliver what the task asks for (the summary, the numbers, the
draft) in your report. Don't say you'll do something later or "keep him updated". If a tool fails, try another
way once; if a source isn't connected, say which one (Manne can press Connect on the Access tab) and finish
everything else.

You can't change data in a system or message anyone while working alone. If the task needs that, prepare it
exactly (the request with its body, or the message text) and say it needs Manne's OK.

When the task is complete, call update_task with status done. If only Manne can unblock it, ask_owner with
one precise question. Record anything worth keeping (record_fact, save_project).

Finish with a short report for Manne: what you found or did (numbers, names, links), what's waiting for his OK,
and what's left."""


def is_mine(task):
    return (task.get("owner") or "").strip().lower() in ("assistant", "ai", "ai assistant") and task.get("status") != "done"


def _busy(task, now):
    since = task.get("working_since")
    if not since:
        return False
    try:
        started = datetime.fromisoformat(since)
    except ValueError:
        return False
    return now - started < timedelta(minutes=BUSY_MINUTES)


def due_for_work(task, now):
    if not is_mine(task) or _busy(task, now):
        return False
    if not task.get("worked_at"):
        return True
    try:
        return now - datetime.fromisoformat(task["worked_at"]) >= timedelta(hours=REWORK_HOURS)
    except ValueError:
        return True


class Busy(RuntimeError):
    pass


def work_on(service, store, task_id, partner="assistant"):
    """Work one task now. Returns the report (text) and the tools used."""
    import talk
    from tools import Toolset

    now = datetime.now(timezone.utc)
    task = store.get_task(task_id)
    if not task:
        raise KeyError(task_id)
    if _busy(task, now):
        raise Busy("The assistant is already working on this task.")
    store.save_task(task_id, {"working_since": utcnow_iso(), "owner": OWNER, "owner_is_me": False,
                              **({"status": "in_progress"} if task.get("status") == "todo" else {})})
    project = store.get_item("projects", task["project_id"]) if task.get("project_id") else None
    prompt = PROMPT.format(
        title=task.get("title", ""), detail=f"Details: {task['detail']}\n" if task.get("detail") else "",
        priority=task.get("priority", "medium"), due=task.get("due") or "none", status=task.get("status", "todo"),
        project=f" Project: {project['name']}." if project else "",
        previous=f"\n\nYour report last time ({task.get('worked_at', '')[:16]}):\n{task['result'][:3000]}"
        if task.get("result") else "")
    toolset = Toolset(store, service._google(), caller=f"{partner} (task)", secrets=service.secrets,
                      consult=service.consult_fn(partner), may_change=False)
    try:
        result = talk.PARTNERS[partner].respond(talk.system_prompt(store, partner, owner_email=service.owner_email),
                                                [{"role": "user", "text": prompt}], toolset, service.secrets)
    except Exception as exc:
        store.save_task(task_id, {"working_since": "", "work_error": str(exc)[:300], "worked_at": utcnow_iso()})
        raise
    report = (result.get("text") or "").strip() or "(no report)"
    tools = result.get("tools", [])
    finished = store.get_task(task_id) or {}
    store.save_task(task_id, {"working_since": "", "work_error": "", "worked_at": utcnow_iso(), "result": report[:6000],
                              "work_tools": sorted({t.get("tool") for t in tools if t.get("tool")})})
    status = "done" if finished.get("status") == "done" else "still open"
    store.append_talk(partner, [{"role": "assistant", "at": utcnow_iso(), "task_id": task_id, "tools": tools[-15:],
                                 "text": f"Worked on the task “{task.get('title', '')}” ({status}).\n\n{report}"}])
    return {"report": report, "tools": tools, "done": finished.get("status") == "done"}


def work_due(service, store, limit=WORK_PER_RUN):
    """The hourly run's part: work the assistant's own tasks that are due, oldest first."""
    now = datetime.now(timezone.utc)
    due = [t for t in store.list_tasks() if due_for_work(t, now)]
    due.sort(key=lambda t: (t.get("worked_at") or "", {"high": 0, "medium": 1, "low": 2}.get(t.get("priority"), 1)))
    worked = []
    for task in due[:limit]:
        try:
            outcome = work_on(service, store, task["id"])
            worked.append({"task": task["id"], "title": task.get("title", ""), "done": outcome["done"]})
        except Exception as exc:  # noqa: BLE001 - one task's failure mustn't stop the others
            worked.append({"task": task["id"], "title": task.get("title", ""), "error": str(exc)[:200]})
    return worked
