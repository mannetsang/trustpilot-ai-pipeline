"""Man AI's browser tool: calls to the browser service, and screenshots reaching each model."""

import copy
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ["STORE_BACKEND"] = "memory"  # never the real database, even when this file runs alone
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "cloud_run"))
import tools  # noqa: E402
from store import MemorySecrets, MemoryStore  # noqa: E402
from tools import Toolset  # noqa: E402

PAGE = {"session": "s1", "device": "desktop", "url": "https://www.superhairpieces.com/", "title": "Superhairpieces",
        "viewport": {"width": 1280, "height": 800}, "scroll": {"y": 0, "height": 4000}, "image": "SU1BR0U=",
        "elements": [{"kind": "a", "label": "Toupees", "x": 120, "y": 40}], "text": "Hair systems for men",
        "notes": []}


class BrowserToolTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_call(body):
            self.calls.append(body)
            return copy.deepcopy(PAGE)

        for patch in (mock.patch.object(tools, "BROWSER_URL", "https://web-browser.example"),
                      mock.patch.object(tools, "_browser_call", side_effect=fake_call),
                      mock.patch.dict(tools._browser_sessions, clear=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def toolset(self, **kw):
        return Toolset(MemoryStore(), None, caller="assistant", **kw)

    def test_open_attaches_the_screenshot_and_reuses_the_tab(self):
        ts = self.toolset()
        self.assertIn("browser", [s["name"] for s in ts.specs()])
        result = ts.call("browser", {"action": "open", "url": "superhairpieces.com"})
        self.assertNotIn("_images", result)  # the image goes to the model, not into the JSON
        self.assertEqual(result["screenshot"], "attached: look at it")
        self.assertEqual(result["elements"], ["a 'Toupees' at 120,40"])
        self.assertEqual(ts.take_images(), [{"mime": "image/jpeg", "data": "SU1BR0U="}])
        self.assertEqual(ts.take_images(), [])
        ts.call("browser", {"action": "click", "target": "Toupees"})
        self.assertNotIn("session", self.calls[0])
        self.assertEqual(self.calls[1]["session"], "s1")  # same tab

    def test_working_alone_it_looks_and_clicks_but_does_not_type(self):
        ts = self.toolset(may_change=False)
        self.assertNotIn("error", ts.call("browser", {"action": "open", "url": "https://example.com"}))
        self.assertNotIn("error", ts.call("browser", {"action": "click", "x": 10, "y": 20}))
        for action in ("type", "press", "select"):
            self.assertIn("error", ts.call("browser", {"action": action, "target": "Search", "text": "wig"}))
        self.assertEqual(len(self.calls), 2)

    def test_a_live_call_gets_a_description_instead(self):
        import partner_gemini

        with mock.patch.object(partner_gemini, "describe_image", return_value="A hero banner with a man smiling."):
            result = self.toolset(images=False).call("browser", {"action": "open", "url": "https://example.com"})
        self.assertEqual(result["screenshot"], "A hero banner with a man smiling.")

    def test_not_offered_without_the_service(self):
        with mock.patch.object(tools, "BROWSER_URL", ""):
            self.assertNotIn("browser", [s["name"] for s in self.toolset().specs()])


class LiveViewTests(unittest.TestCase):
    """People can watch: each step shows as it starts, then with the screenshot it ended on."""

    def setUp(self):
        self.store = MemoryStore()
        self.seen_during_call = []

        def fake_call(body):
            self.seen_during_call.append(copy.deepcopy(self.store.get_browser_view()))
            if body["action"] == "type":
                return {"error": "no field called Search"}
            return copy.deepcopy(PAGE)

        for patch in (mock.patch.object(tools, "BROWSER_URL", "https://web-browser.example"),
                      mock.patch.object(tools, "_browser_call", side_effect=fake_call),
                      mock.patch.dict(tools._browser_sessions, clear=True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.toolset = Toolset(self.store, None, caller="assistant")

    def test_steps_and_the_latest_screenshot_are_recorded(self):
        self.toolset.call("browser", {"action": "open", "url": "superhairpieces.com"})
        working = self.seen_during_call[0]["steps"][-1]
        self.assertEqual((working["action"], working["status"], working["detail"]), ("open", "working", "superhairpieces.com"))
        view = self.store.get_browser_view()
        self.assertEqual(view["steps"][-1]["status"], "done")
        self.assertEqual((view["url"], view["title"]), (PAGE["url"], PAGE["title"]))
        frame = self.store.get_browser_frame()
        self.assertEqual((frame["image"], frame["at"]), (PAGE["image"], view["frame_at"]))
        self.toolset.call("browser", {"action": "click", "target": "Toupees"})
        self.toolset.call("browser", {"action": "type", "target": "Search", "text": "wig"})
        view = self.store.get_browser_view()
        self.assertEqual([(s["action"], s["status"]) for s in view["steps"]],
                         [("open", "done"), ("click", "done"), ("type", "error")])
        self.assertEqual(view["steps"][1]["detail"], "Toupees")
        self.assertIn("no field", view["steps"][2]["error"])
        session = view["session_started_at"]
        self.toolset.call("browser", {"action": "open", "url": "https://example.com"})  # a new page: a new session
        view = self.store.get_browser_view()
        self.assertNotEqual(view["session_started_at"], session)
        self.assertEqual([s["action"] for s in view["steps"]], ["open"])

    def test_a_person_can_take_control_and_hand_back(self):
        self.toolset.call("browser", {"action": "open", "url": "superhairpieces.com"})
        self.assertEqual(self.store.get_browser_view()["session_id"], "s1")
        tools.set_control(self.store, True)
        calls = len(self.seen_during_call)
        refused = self.toolset.call("browser", {"action": "click", "target": "Toupees"})
        self.assertIn("taken over the browser", refused["error"])
        self.assertEqual(len(self.seen_during_call), calls)  # Man AI didn't touch the browser
        with mock.patch.object(tools, "_browser_call", side_effect=lambda body: (self.sent.append(body), copy.deepcopy(PAGE))[1]):
            self.sent = []
            out = tools.user_browser(self.store, "click", x=640, y=120)
            tools.user_browser(self.store, "type", text="lace front")
        self.assertEqual(self.sent[0], {"action": "click", "session": "s1", "x": 640, "y": 120, "direction": "down",
                                        "person": True})
        self.assertEqual(out["view"]["steps"][-1]["caller"], "you")
        self.assertEqual(out["frame"]["image"], PAGE["image"])
        tools.set_control(self.store, False)
        after = self.toolset.call("browser", {"action": "scroll", "direction": "down"})
        self.assertNotIn("error", after)
        self.assertIn("Manne used the browser directly", after["notes"][-1])
        self.assertIn("type “lace front”", after["notes"][-1])

    def test_a_person_may_type_a_password_and_it_is_never_shown(self):
        self.toolset.call("browser", {"action": "open", "url": "app.wiz.io"})
        sent = []

        def fake(body):
            sent.append(body)
            page = copy.deepcopy(PAGE)
            if body["action"] == "type" and body["text"] == "hunter2-secret":
                page["notes"] = ["secret field"]
            return page
        with mock.patch.object(tools, "_browser_call", side_effect=fake):
            tools.user_browser(self.store, "type", text="manne@superhairpieces.com")
            tools.user_browser(self.store, "type", text="hunter2-secret")
            tools.user_browser(self.store, "type", text="also-private", hidden=True)
        self.assertTrue(all(b["person"] for b in sent))  # only the page's person path says so
        stored = json.dumps([self.store.get_browser_view(), self.store.get_browser_frame()])
        self.assertIn("manne@superhairpieces.com", stored)
        self.assertNotIn("hunter2-secret", stored)
        self.assertNotIn("also-private", stored)
        details = [s["detail"] for s in self.store.get_browser_view()["steps"] if s["caller"] == "you"]
        self.assertEqual(details[1:], ["•••••••••••• (hidden)", "•••••••••••• (hidden)"])
        tools.set_control(self.store, False)
        with mock.patch.object(tools, "_browser_call", side_effect=fake):
            self.toolset.call("browser", {"action": "type", "text": "x", "target": "Email"})
        self.assertEqual(sent[-1]["text"], "x")
        self.assertNotIn("person", sent[-1])  # Man AI never types as a person

    def test_closing_the_tab_forgets_it(self):
        self.toolset.call("browser", {"action": "open", "url": "app.wiz.io"})
        with mock.patch.object(tools, "_browser_call", return_value={"session": "s1", "closed": True}):
            out = tools.user_browser(self.store, "close")
        self.assertNotIn("session_id", out["view"])
        self.assertEqual(out["view"]["steps"][-1]["action"], "close")
        with mock.patch.object(tools, "_browser_call", return_value={**copy.deepcopy(PAGE), "session": "s9"}) as call:
            again = tools.user_browser(self.store, "click", x=1, y=1)  # a fresh, signed-out tab on the last page
        self.assertEqual(call.call_args[0][0]["action"], "open")
        self.assertIn("reopened", again["note"])

    def _visited(self, *urls):
        for url in urls:
            with mock.patch.object(tools, "_browser_call", return_value={**copy.deepcopy(PAGE), "url": url}):
                self.toolset.call("browser", {"action": "goto" if self.store.get_browser_view() else "open", "url": url})

    def test_an_ended_tab_reopens_by_itself_for_man_ai(self):
        self._visited("https://app.wiz.io/login", "https://accounts.google.com/v3/signin/challenge/dp")
        sent = []

        def service(body):
            sent.append(body)
            if body["action"] == "back":
                return {"error": "That browser session has ended (closed or idle too long). Use action=open again."}
            return {**copy.deepcopy(PAGE), "session": "s2", "url": body["url"]}
        with mock.patch.object(tools, "_browser_call", side_effect=service):
            out = self.toolset.call("browser", {"action": "back"})
        self.assertNotIn("error", out)
        self.assertEqual(sent[-1], {"action": "open", "url": "https://app.wiz.io/login", "device": "desktop"})
        self.assertIn("old tab had closed", out["notes"][-1])
        self.assertIn("https://accounts.google.com/v3/signin/challenge/dp", out["notes"][-1])  # where it had been
        view = self.store.get_browser_view()
        self.assertEqual(view["session_id"], "s2")
        self.assertEqual((view["steps"][-1]["action"], view["steps"][-1]["status"]), ("open", "done"))

    def test_an_ended_tab_reopens_for_a_person_too(self):
        self._visited("https://app.wiz.io/login")
        view = self.store.get_browser_view()
        view["session_used_at"] = "2020-01-01T00:00:00+00:00"  # long unused: the service has closed it
        self.store.set_browser_view(view)
        with mock.patch.object(tools, "_browser_call", return_value={**copy.deepcopy(PAGE), "session": "s3",
                                                                      "url": "https://app.wiz.io/login"}) as call:
            out = tools.user_browser(self.store, "click", x=10, y=10)
        self.assertEqual(call.call_args[0][0]["action"], "open")  # the old click isn't replayed on a new page
        self.assertIn("reopened https://app.wiz.io/login", out["note"])
        self.assertEqual(out["view"]["session_id"], "s3")

    def test_a_look_refreshes_the_screenshot_without_a_step(self):
        self.toolset.call("browser", {"action": "open", "url": "app.wiz.io"})
        before = self.store.get_browser_view()
        after_phone = {**copy.deepcopy(PAGE), "url": "https://app.wiz.io/dashboard", "image": "TkVX"}
        with mock.patch.object(tools, "_browser_call", return_value=after_phone) as call:
            out = tools.user_browser(self.store, "screenshot")
        self.assertEqual(call.call_args[0][0], {"action": "screenshot", "session": "s1"})
        self.assertEqual((out["frame"]["image"], out["view"]["url"]), ("TkVX", "https://app.wiz.io/dashboard"))
        self.assertEqual(len(out["view"]["steps"]), len(before["steps"]))  # not a step
        self.assertNotIn("control_until", out["view"])                     # and nobody took control by looking

    def test_control_runs_out_by_itself(self):
        view = tools.set_control(self.store, True)
        self.assertTrue(tools._user_in_control(view))
        view["control_until"] = "2020-01-01T00:00:00+00:00"
        self.assertFalse(tools._user_in_control(view))

    def test_a_person_needs_an_address_when_no_page_is_open(self):
        self.assertIn("Type an address", tools.user_browser(self.store, "click", x=1, y=1)["error"])
        self.assertIn("unknown action", tools.user_browser(self.store, "open", url="x.com")["error"])

    def test_the_page_endpoints_for_control(self):
        import main

        client = main.app.test_client()
        with mock.patch.object(main, "store", self.store):
            with client.session_transaction() as s:
                s["email"] = main.OWNER_EMAIL
            headers = {"X-Chat-Assistant": "1"}
            on = client.post("/api/browser/control", json={"on": True}, headers=headers).get_json()
            self.assertIn("control_until", on["view"])
            r = client.post("/api/browser/act", json={"action": "goto", "url": "superhairpieces.com"}, headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["view"]["steps"][-1]["caller"], "you")
            self.assertEqual(client.post("/api/browser/act", json={"action": "open"}, headers=headers).status_code, 400)
            self.assertEqual(client.post("/api/browser/act", json={"action": "goto"}).status_code, 403)  # no CSRF header
            off = client.post("/api/browser/control", json={"on": False}, headers=headers).get_json()
            self.assertNotIn("control_until", off["view"])
            self.assertTrue(client.get("/api/browser").get_json()["available"])

    def test_a_store_failure_never_stops_the_browsing(self):
        with mock.patch.object(self.store, "set_browser_view", side_effect=RuntimeError("firestore down")):
            result = self.toolset.call("browser", {"action": "open", "url": "https://example.com"})
        self.assertNotIn("error", result)
        self.assertEqual(result["title"], PAGE["title"])

    def test_the_page_gets_the_screenshot_only_when_it_changed(self):
        import main

        self.toolset.call("browser", {"action": "open", "url": "https://example.com"})
        client = main.app.test_client()
        with mock.patch.object(main, "store", self.store):
            with client.session_transaction() as s:
                s["email"] = main.OWNER_EMAIL
            first = client.get("/api/browser").get_json()
            self.assertEqual(first["frame"]["image"], PAGE["image"])
            again = client.get("/api/browser", query_string={"since": first["view"]["frame_at"]}).get_json()
            self.assertIsNone(again["frame"])
            self.assertEqual(again["view"]["steps"][-1]["status"], "done")
            self.assertEqual(main.app.test_client().get("/api/browser").status_code, 401)  # signed out


class ImagesReachEachModelTests(unittest.TestCase):
    """One browser call, then an answer: the screenshot must be in what the model is sent next."""

    def setUp(self):
        patch = mock.patch.object(tools, "BROWSER_URL", "https://web-browser.example")
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(tools, "_browser_call", return_value=copy.deepcopy(PAGE))
        patch.start()
        self.addCleanup(patch.stop)
        self.toolset = Toolset(MemoryStore(), None, caller="assistant")

    def test_claude_gets_it_inside_the_tool_result(self):
        import partner_claude

        sent = []
        replies = [SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
                       type="tool_use", id="t1", name="browser", input={"action": "open", "url": "https://x.com"})]),
                   SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Looks fine.")])]

        def create(secrets, **kwargs):
            sent.append(copy.deepcopy(kwargs["messages"]))
            return replies[len(sent) - 1]

        with mock.patch.object(partner_claude, "_create", side_effect=create):
            out = partner_claude.respond("sys", [{"role": "user", "text": "look"}], self.toolset, MemorySecrets())
        self.assertEqual(out["text"], "Looks fine.")
        result = sent[1][-1]["content"][0]
        self.assertEqual(result["type"], "tool_result")
        self.assertEqual(result["content"][1]["type"], "image")
        self.assertEqual(result["content"][1]["source"]["data"], "SU1BR0U=")

    def test_chatgpt_gets_it_right_after_the_tool_messages(self):
        import partner_openai

        sent = []
        call = SimpleNamespace(id="c1", function=SimpleNamespace(name="browser", arguments='{"action":"open","url":"x.com"}'))
        first = SimpleNamespace(tool_calls=[call], content=None,
                                model_dump=lambda **k: {"role": "assistant", "tool_calls": [{"id": "c1"}]})
        second = SimpleNamespace(tool_calls=None, content="Looks fine.")

        def create(**kwargs):
            sent.append(copy.deepcopy(kwargs["messages"]))
            return SimpleNamespace(choices=[SimpleNamespace(message=[first, second][len(sent) - 1])])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch("openai.OpenAI", return_value=client):
            out = partner_openai.respond("sys", [{"role": "user", "text": "look"}], self.toolset,
                                         MemorySecrets({"CHATGPT_API_KEY": "k"}))
        self.assertEqual(out["text"], "Looks fine.")
        roles = [m["role"] for m in sent[1]]
        self.assertEqual(roles[-2:], ["tool", "user"])
        self.assertTrue(sent[1][-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,SU1BR0U="))

    def test_gemini_gets_it_in_the_function_response_turn(self):
        import partner_gemini

        sent = []
        replies = [{"candidates": [{"content": {"role": "model", "parts": [
                       {"functionCall": {"name": "browser", "args": {"action": "open", "url": "x.com"}}}]}}]},
                   {"candidates": [{"content": {"role": "model", "parts": [{"text": "Looks fine."}]}}]}]

        def post(url, headers=None, json=None, timeout=None):
            sent.append(copy.deepcopy(json))
            return SimpleNamespace(status_code=200, json=lambda: replies[len(sent) - 1])

        with mock.patch.object(partner_gemini, "_token", return_value="tok"), \
                mock.patch.object(partner_gemini.requests, "post", side_effect=post):
            out = partner_gemini.respond("sys", [{"role": "user", "text": "look"}], self.toolset)
        self.assertEqual(out["text"], "Looks fine.")
        parts = sent[1]["contents"][-1]["parts"]
        self.assertIn("functionResponse", parts[0])
        self.assertEqual(parts[1]["inlineData"], {"mimeType": "image/jpeg", "data": "SU1BR0U="})


if __name__ == "__main__":
    unittest.main()
