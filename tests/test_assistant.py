"""Offline tests: fake Google + fake Gemini, in-memory store. No network, no real chats.

    python -m unittest discover -s chat-assistant/tests
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ["STORE_BACKEND"] = "memory"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "cloud_run"))

import assistant  # noqa: E402
from google_apis import Directory  # noqa: E402
from store import MemoryStore  # noqa: E402

NOW = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)
OWNER = {"user": "users/1", "email": "manne@superhairpieces.com"}


def ts(hours_ago):
    """Google-style timestamp: fractional seconds to the nanosecond, trailing Z."""
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z"


def msg(space, n, sender, hours_ago, text, thread="t1", bot=False, mention=None):
    m = {"name": f"{space}/messages/{n}", "sender": {"name": sender, "type": "BOT" if bot else "HUMAN"},
         "createTime": ts(hours_ago), "text": text, "thread": {"name": f"{space}/threads/{thread}"}}
    if mention:
        m["annotations"] = [{"type": "USER_MENTION", "userMention": {"user": {"name": mention}}}]
    return m


class FakeGoogle:
    def __init__(self, spaces, messages, members=None):
        self.spaces, self.messages, self.members = spaces, messages, members or {}
        self.sent, self.events = [], []
        self.owner_chat_id = "users/1"

    def chat_user_id(self, space, email):
        return self.owner_chat_id

    def list_spaces(self):
        return self.spaces

    def list_members(self, space):
        return [{"member": {"name": u, "type": "HUMAN"}} for u in self.members.get(space, [])]

    def list_messages_since(self, space, since_iso, limit=300):
        since = assistant.parse_ts(since_iso)
        return [m for m in self.messages.get(space, []) if assistant.parse_ts(m["createTime"]) > since][:limit]

    def list_messages_before(self, space, start_iso, end_iso, limit):
        return []

    def reply_in_thread(self, space, thread, text):
        self.sent.append((space, thread, text))
        return {"name": f"{space}/messages/reply{len(self.sent)}"}

    def create_event(self, title, start, end, tz, attendees, description=""):
        self.events.append((title, start, end, tz, attendees))
        return {"htmlLink": "https://calendar.google.com/event?eid=x"}

    def load_directory(self):
        return Directory({
            "users/1": {"name": "Manne", "email": "manne@superhairpieces.com"},
            "users/2": {"name": "Kathy", "email": "kathy@superhairpieces.com"},
            "users/3": {"name": "Sydney", "email": "sydney@superhairpieces.com"},
        })


class FakeLLM:
    """Returns canned results keyed by space name found in the prompt."""

    def __init__(self, by_space, fail_for=()):
        self.by_space, self.fail_for, self.calls = by_space, set(fail_for), []

    def analyze(self, prompt):
        for space, result in self.by_space.items():
            if f"{space}/messages/" in prompt:
                self.calls.append(space)
                if space in self.fail_for:
                    raise RuntimeError("Gemini exploded")
                return json.loads(json.dumps(result))
        self.calls.append("?")
        return {"tasks": [], "actions": []}


def reply(source, confidence=0.95, directed=True, text="Thanks, on it!"):
    return {"type": "chat_reply", "source_message": source, "directed_at_me": directed,
            "confidence": confidence, "reason": "asked directly", "reply_text": text}


def scenario():
    spaces = [
        {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE", "lastActiveTime": ts(1), "spaceUri": "https://chat/dm1"},
        {"name": "spaces/S1", "spaceType": "SPACE", "displayName": "D Team", "lastActiveTime": ts(0.5)},
        {"name": "spaces/BOT", "spaceType": "SPACE", "displayName": "Reddit Notification", "lastActiveTime": ts(0.2)},
        {"name": "spaces/QUIET", "spaceType": "SPACE", "displayName": "Old", "lastActiveTime": ts(72)},
    ]
    messages = {
        "spaces/DM1": [msg("spaces/DM1", 1, "users/2", 1, "Can you confirm the Friday order?", mention="users/1")],
        "spaces/S1": [msg("spaces/S1", 1, "users/3", 0.5, "Meeting Thursday 2pm about the Brampton launch?", thread="t9")],
        "spaces/BOT": [msg("spaces/BOT", 1, "users/99", 0.2, "New post in r/HairSystem", bot=True)],
    }
    llm = FakeLLM({
        "spaces/DM1": {"tasks": [{"task_id": "", "title": "Confirm Friday order", "owner": "Manne", "owner_is_me": True,
                                  "due": "2026-10-02", "priority": "high", "status": "todo",
                                  "source_message": "spaces/DM1/messages/1"}],
                       "actions": [reply("spaces/DM1/messages/1")]},
        "spaces/S1": {"tasks": [], "actions": [
            reply("spaces/S1/messages/1", confidence=0.5, text="Maybe?"),
            {"type": "calendar_event", "source_message": "spaces/S1/messages/1", "directed_at_me": True,
             "confidence": 0.95, "reason": "meeting agreed", "event_title": "Brampton launch",
             "event_start": "2026-10-01T14:00:00", "event_end": "2026-10-01T15:00:00",
             "event_attendees": ["sydney@superhairpieces.com", "stranger@evil.example"]}]},
    })
    google = FakeGoogle(spaces, messages, members={"spaces/DM1": ["users/1", "users/2"]})
    store = MemoryStore()
    store.set_owner(OWNER)
    return store, google, llm


class ParseTsTests(unittest.TestCase):
    def test_google_and_python_formats_are_all_aware_utc(self):
        cases = {
            "2026-09-29T05:12:34.123456789Z": datetime(2026, 9, 29, 5, 12, 34, 123456, tzinfo=timezone.utc),
            "2026-09-29T05:12:34Z": datetime(2026, 9, 29, 5, 12, 34, tzinfo=timezone.utc),
            "2026-09-28T06:10:00.123456+00:00": datetime(2026, 9, 28, 6, 10, 0, 123456, tzinfo=timezone.utc),
            "2026-09-29T01:12:34.5-04:00": datetime(2026, 9, 29, 5, 12, 34, 500000, tzinfo=timezone.utc),
        }
        for raw, expected in cases.items():
            parsed = assistant.parse_ts(raw)
            self.assertIsNotNone(parsed.tzinfo, raw)
            self.assertEqual(parsed, expected, raw)

    def test_first_run_default_watermark_compares(self):
        # The exact failure seen live: an isoformat() watermark with microseconds vs Google's lastActiveTime.
        since = (NOW - timedelta(hours=24, microseconds=1)).isoformat()
        self.assertLess(assistant.parse_ts(since), assistant.parse_ts("2026-09-29T15:00:00.000000123Z"))


class RunTests(unittest.TestCase):
    def test_tasks_actions_and_watermarks(self):
        store, google, llm = scenario()
        summary = assistant.run(store, google, llm, now=NOW)

        self.assertEqual(summary["spaces_total"], 4)
        self.assertEqual(summary["spaces_processed"], 3)  # QUIET is older than the lookback
        self.assertNotIn("spaces/BOT", llm.calls)          # bot-only posts skip Gemini
        tasks = store.list_tasks()
        self.assertEqual([t["title"] for t in tasks], ["Confirm Friday order"])
        self.assertEqual(tasks[0]["space_label"], "Kathy")  # DM labelled by the other member
        self.assertEqual(google.sent, [("spaces/DM1", "spaces/DM1/threads/t1", "Thanks, on it!")])
        self.assertEqual(len(google.events), 1)
        self.assertEqual(google.events[0][4], ["sydney@superhairpieces.com"])  # unknown email dropped
        statuses = sorted(a["status"] for a in store.list_actions())
        self.assertEqual(statuses, ["done", "done", "suggested"])  # low-confidence reply waits
        self.assertEqual(store.get_watermarks()["DM1"], ts(1))

    def test_rerun_does_not_duplicate(self):
        store, google, llm = scenario()
        assistant.run(store, google, llm, now=NOW)
        store.set_watermarks({"DM1": ts(48), "S1": ts(48)})  # force a re-read of the same messages
        assistant.run(store, google, llm, now=NOW)
        self.assertEqual(len(store.list_tasks()), 1)
        self.assertEqual(len(store.list_actions()), 3)
        self.assertEqual(len(google.sent), 1)

    def test_paused_everything_waits(self):
        store, google, llm = scenario()
        store.update_settings({"auto_act": False})
        assistant.run(store, google, llm, now=NOW)
        self.assertEqual(google.sent, [])
        self.assertEqual(google.events, [])
        self.assertTrue(all(a["status"] == "suggested" for a in store.list_actions()))
        self.assertTrue(all("paused" in a["blocked_reason"] for a in store.list_actions()))

    def test_old_message_is_only_suggested(self):
        store, google, llm = scenario()
        google.messages["spaces/DM1"][0]["createTime"] = ts(20)
        google.spaces[0]["lastActiveTime"] = ts(20)
        assistant.run(store, google, llm, now=NOW)
        dm = [a for a in store.list_actions() if a["space"] == "DM1"][0]
        self.assertEqual(dm["status"], "suggested")
        self.assertIn("old", dm["blocked_reason"])

    def test_no_reply_when_owner_already_answered(self):
        store, google, llm = scenario()
        google.messages["spaces/DM1"].append(msg("spaces/DM1", 2, "users/1", 0.5, "Yes, confirmed."))
        assistant.run(store, google, llm, now=NOW)
        self.assertEqual(google.sent, [])
        self.assertFalse([a for a in store.list_actions() if a["space"] == "DM1"])

    def test_never_answers_own_or_bot_messages(self):
        store, google, llm = scenario()
        google.messages["spaces/DM1"] = [msg("spaces/DM1", 1, "users/1", 1, "Note to self")]
        assistant.run(store, google, llm, now=NOW)
        self.assertEqual(google.sent, [])

    def test_owner_recognised_even_if_sign_in_id_differs(self):
        # Sign-in said users/1, but Chat knows the owner as users/77: still never answer them.
        store, google, llm = scenario()
        google.owner_chat_id = "users/77"
        google.messages["spaces/DM1"] = [msg("spaces/DM1", 1, "users/77", 1, "Reminder to self: call supplier")]
        assistant.run(store, google, llm, now=NOW)
        self.assertEqual(google.sent, [])
        self.assertFalse([a for a in store.list_actions() if a["space"] == "DM1"])

    def test_hourly_cap(self):
        store, google, llm = scenario()
        with mock.patch.object(assistant, "MAX_ACTIONS_PER_RUN", 1):
            assistant.run(store, google, llm, now=NOW)
        done = [a for a in store.list_actions() if a["status"] == "done"]
        capped = [a for a in store.list_actions() if "limit" in a.get("blocked_reason", "")]
        self.assertEqual(len(done), 1)
        self.assertEqual(len(capped), 1)

    def test_owner_edits_win(self):
        store, google, llm = scenario()
        assistant.run(store, google, llm, now=NOW)
        task = store.list_tasks()[0]
        store.save_task(task["id"], {"status": "in_progress", "user_edited": True})
        llm.by_space["spaces/DM1"]["tasks"] = [dict(llm.by_space["spaces/DM1"]["tasks"][0], task_id=task["id"], status="done")]
        store.set_watermarks({"DM1": ts(48)})
        assistant.run(store, google, llm, now=NOW)
        self.assertEqual(store.get_task(task["id"])["status"], "in_progress")

    def test_gemini_failure_keeps_watermark(self):
        store, google, llm = scenario()
        llm.fail_for = {"spaces/S1"}
        summary = assistant.run(store, google, llm, now=NOW)
        self.assertNotIn("S1", store.get_watermarks())
        self.assertIn("DM1", store.get_watermarks())
        self.assertEqual(summary["errors"][0]["space"], "D Team")

    def test_progress_is_live_during_the_run(self):
        store, google, llm = scenario()
        seen = []
        real_analyze = llm.analyze

        def analyze(prompt):  # snapshot what the UI would see while Gemini is working
            seen.append(store.get_progress())
            return real_analyze(prompt)

        llm.analyze = analyze
        assistant.run(store, google, llm, now=NOW)
        self.assertTrue(all(s["running"] and s["phase"] == "Reading conversations" and s["total"] == 3 for s in seen))
        self.assertIn("Kathy", {label for s in seen for label in s["current"]})  # DM shown by the other person's name
        final = store.get_progress()
        self.assertEqual((final["done"], final["total"], final["tasks_created"], final["actions_done"]), (3, 3, 1, 2))
        self.assertEqual(final["current"], [])

    def test_progress_counts_problems(self):
        store, google, llm = scenario()
        llm.fail_for = {"spaces/S1"}
        assistant.run(store, google, llm, now=NOW)
        self.assertEqual((store.get_progress()["done"], store.get_progress()["problems"]), (3, 1))

    def test_no_owner_means_reconnect(self):
        store, google, llm = scenario()
        store._delete("chat_assistant", "owner")
        with self.assertRaises(assistant.ReconnectNeeded):
            assistant.run(store, google, llm, now=NOW)


class WebTests(unittest.TestCase):
    def setUp(self):
        import main

        self.main = main
        main.store = MemoryStore()
        main.secret_store = main.make_secrets()
        main.secret_store.put(main.RUN_TOKEN_SECRET_ID, "run-secret")
        self.store, self.google, self.llm = scenario()
        main.store = self.store
        main.app.config["MAKE_GOOGLE"] = lambda: self.google
        main.app.config["MAKE_LLM"] = lambda: self.llm
        main.app.config["TESTING"] = True
        self.client = main.app.test_client()

    def sign_in(self):
        with self.client.session_transaction() as s:
            s["email"] = self.main.OWNER_EMAIL

    def call(self, method, path, body=None, csrf=True):
        headers = {"X-Chat-Assistant": "1"} if csrf else {}
        return self.client.open(path, method=method, json=body, headers=headers)

    def test_signed_out(self):
        self.assertEqual(self.call("GET", "/api/state").status_code, 401)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"isn't set up yet", page.data)  # no OAuth client stored

    def test_board_page_for_owner(self):
        self.sign_in()
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'id="board-view"', page.data)
        self.assertIn(b"manne@superhairpieces.com", page.data)
        self.assertEqual(page.headers["X-Frame-Options"], "DENY")

    def test_other_account_session_is_rejected(self):
        with self.client.session_transaction() as s:
            s["email"] = "someone@superhairpieces.com"
        self.assertEqual(self.call("GET", "/api/state").status_code, 401)

    def test_csrf_header_required(self):
        self.sign_in()
        self.assertEqual(self.call("POST", "/api/tasks", {"title": "x"}, csrf=False).status_code, 403)
        self.assertEqual(self.call("POST", "/api/tasks", {"title": "x"}).status_code, 200)

    def test_task_crud(self):
        self.sign_in()
        task = self.call("POST", "/api/tasks", {"title": "Order wig tape", "priority": "bogus"}).get_json()["task"]
        self.assertEqual(task["priority"], "medium")
        updated = self.call("PATCH", f"/api/tasks/{task['id']}", {"status": "done"}).get_json()["task"]
        self.assertEqual(updated["status"], "done")
        self.assertTrue(updated["user_edited"])
        self.assertEqual(self.call("DELETE", f"/api/tasks/{task['id']}").status_code, 200)
        self.assertIsNone(self.store.get_task(task["id"]))

    def test_scheduled_run_needs_token(self):
        self.assertEqual(self.client.post("/run").status_code, 403)
        self.assertEqual(self.client.post("/run", headers={"X-Run-Token": "wrong"}).status_code, 403)
        with mock.patch.object(assistant, "datetime") as dt:
            dt.now.return_value = NOW
            dt.fromisoformat = datetime.fromisoformat
            resp = self.client.post("/run", headers={"X-Run-Token": "run-secret"})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.get_json()["trigger"], "schedule")
        self.assertEqual(self.store.get_status()["last_run"]["spaces_processed"], 3)
        self.assertEqual(self.call("GET", "/api/progress").status_code, 401)  # signed out
        self.sign_in()
        progress = self.call("GET", "/api/progress").get_json()
        self.assertEqual((progress["running"], progress["phase"], progress["trigger"]), (False, "Done", "schedule"))
        self.assertEqual(progress["done"], progress["total"])

    def test_failed_run_marks_progress_failed(self):
        self.sign_in()
        self.main.app.config["MAKE_GOOGLE"] = lambda: None  # not connected
        self.assertEqual(self.call("POST", "/api/run").status_code, 503)
        progress = self.call("GET", "/api/progress").get_json()
        self.assertEqual((progress["running"], progress["phase"]), (False, "Failed"))
        self.assertIn("isn't connected", progress["error"])

    def test_send_suggestion_with_edit(self):
        self.sign_in()
        self.store.update_settings({"auto_act": False})
        with mock.patch.object(assistant, "datetime") as dt:
            dt.now.return_value = NOW
            dt.fromisoformat = datetime.fromisoformat
            self.call("POST", "/api/run")
        waiting = [a for a in self.store.list_actions() if a["type"] == "chat_reply" and a["space"] == "DM1"][0]
        resp = self.call("POST", f"/api/actions/{waiting['id']}/send", {"reply_text": "Confirmed for Friday."})
        self.assertEqual(resp.get_json()["action"]["status"], "done")
        self.assertEqual(self.google.sent[-1][2], "Confirmed for Friday.")
        # a second send of the same suggestion is refused
        self.assertEqual(self.call("POST", f"/api/actions/{waiting['id']}/send").status_code, 409)

    def test_settings_only_known_keys(self):
        self.sign_in()
        s = self.call("PATCH", "/api/settings", {"auto_act": False, "evil": True}).get_json()["settings"]
        self.assertFalse(s["auto_act"])
        self.assertNotIn("evil", s)

    def test_callback_rejects_bad_state(self):
        self.assertEqual(self.client.get("/oauth/callback?state=x&code=y").status_code, 400)

    def test_login_redirects_to_google_for_owner(self):
        self.main.secret_store.put(self.main.OAUTH_CLIENT_SECRET_ID, json.dumps({"web": {
            "client_id": "cid.apps.googleusercontent.com", "client_secret": "s",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}))
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 302)
        loc = resp.headers["Location"]
        self.assertIn("accounts.google.com", loc)
        self.assertIn("login_hint=manne%40superhairpieces.com", loc)
        self.assertIn("chat.messages.create", loc)       # no token yet -> full connect
        self.assertIn("access_type=offline", loc)
        self.assertIn("code_challenge=", loc)             # PKCE


if __name__ == "__main__":
    unittest.main()
