"""Offline tests for the company-assistant layer: knowledge base, autonomy tiers, learning from chats,
tools, talking to partners, the interview, and the live voice bridge. No network, no real models.

    python -m unittest discover -s chat-assistant/tests
"""

import asyncio
import json
import os
import sys
import unittest
from unittest import mock

os.environ["STORE_BACKEND"] = "memory"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "cloud_run"))

import assistant  # noqa: E402
import knowledge  # noqa: E402
import talk  # noqa: E402
from store import MemorySecrets, MemoryStore  # noqa: E402
from test_assistant import NOW, FakeGoogle, FakeLLM, msg, reply, scenario, ts  # noqa: E402
from tools import SPECS, Toolset, to_gemini_schema  # noqa: E402


class KnowledgeTests(unittest.TestCase):
    def test_seeding_happens_once(self):
        store = MemoryStore()
        knowledge.ensure_seeded(store)
        systems, questions = store.list_items("systems"), store.list_items("questions")
        self.assertEqual(len(systems), len(knowledge.SEED_SYSTEMS))
        self.assertEqual(len(questions), len(knowledge.SEED_QUESTIONS))
        self.assertEqual(store.get_item("systems", "google_chat")["status"], "connected")
        store.delete_item("questions", questions[0]["id"])
        knowledge.ensure_seeded(store)  # a deleted seed question stays deleted
        self.assertEqual(len(store.list_items("questions")), len(knowledge.SEED_QUESTIONS) - 1)

    def test_projects_match_by_name_and_facts_dedupe(self):
        store = MemoryStore()
        p = knowledge.save_project(store, {"name": "Amazon–SkuVault Bridge", "owner": "Manne"})
        again = knowledge.save_project(store, {"name": "amazon–skuvault bridge", "status": "blocked"})
        self.assertEqual(p["id"], again["id"])
        self.assertEqual((again["owner"], again["status"]), ("Manne", "blocked"))
        self.assertIsNone(knowledge.save_project(store, {"summary": "no name"}))
        knowledge.add_fact(store, "Kathy runs Dufferin")
        knowledge.add_fact(store, "Kathy runs Dufferin")
        self.assertEqual(len(store.list_items("facts")), 1)

    def test_brief_mentions_everything(self):
        store = MemoryStore()
        knowledge.ensure_seeded(store)
        knowledge.save_project(store, {"name": "FBM Removal", "company": "superhairpieces", "status": "active"})
        knowledge.add_fact(store, "SkuVault is the source of truth for stock")
        text = knowledge.brief(store, store.get_settings())
        for needle in ("FBM Removal", "SkuVault is the source", "OPEN QUESTIONS", "- connected:", "AUTONOMY",
                       "Refunds, prices, discounts, ad budgets, payments: ask"):
            self.assertIn(needle, text)
        self.assertIn("FBM Removal", knowledge.brief(store, compact=True))
        self.assertIn("Superhairpieces", knowledge.company_background())

    def test_gemini_schema_conversion(self):
        converted = to_gemini_schema({"type": "object", "properties": {"a": {"type": "array", "items": {"type": "string"}}},
                                      "required": []})
        self.assertEqual(converted, {"type": "OBJECT", "properties": {"a": {"type": "ARRAY", "items": {"type": "STRING"}}}})


class AutonomyAndLearningTests(unittest.TestCase):
    def test_internal_reply_auto_but_ask_tier_holds_it(self):
        store, google, llm = scenario()
        store.update_settings({"autonomy": {**store.get_settings()["autonomy"], "internal_chat": "ask"}})
        assistant.run(store, google, llm, now=NOW)
        dm = [a for a in store.list_actions() if a["space"] == "DM1"][0]
        self.assertEqual(dm["status"], "suggested")
        self.assertIn("need your OK", dm["blocked_reason"])
        self.assertEqual(google.sent, [])
        self.assertEqual(len(google.events), 1)  # calendar tier still auto

    def test_outside_person_counts_as_customer_message(self):
        store, google, llm = scenario()
        google.messages["spaces/DM1"] = [msg("spaces/DM1", 1, "users/555", 1, "Can you confirm my order?", mention="users/1")]
        assistant.run(store, google, llm, now=NOW)
        dm = [a for a in store.list_actions() if a["space"] == "DM1"][0]
        self.assertEqual((dm["status"], dm["category"]), ("suggested", "customer_messages"))
        self.assertEqual(google.sent, [])

    def test_learning_creates_project_facts_questions_and_links_tasks(self):
        store, google, llm = scenario()
        llm.by_space["spaces/DM1"].update({
            "project": {"is_project": True, "name": "Friday wholesale order", "summary": "Tier 1 order for Kathy",
                        "status": "active", "owner": "Manne"},
            "facts": ["Kathy handles Dufferin wholesale orders"],
            "questions": ["Who approves wholesale discounts?"],
        })
        summary = assistant.run(store, google, llm, now=NOW)
        project = knowledge.find_project(store, "Friday wholesale order")
        self.assertEqual(project["spaces"], ["DM1"])
        self.assertEqual(store.list_tasks()[0]["project_id"], project["id"])
        self.assertTrue(any("Dufferin wholesale" in f["text"] for f in store.list_items("facts")))
        self.assertTrue(any("wholesale discounts" in q["question"] for q in store.list_items("questions")))
        self.assertEqual((summary["projects"], summary["facts"], summary["questions"]), (1, 1, 1))
        self.assertEqual(store.get_item("spaces", "DM1")["label"], "Kathy")  # conversation cache for tools

    def test_channels_are_not_projects(self):
        store, google, llm = scenario()
        llm.by_space["spaces/S1"]["project"] = {"is_project": False, "name": "D Team"}
        assistant.run(store, google, llm, now=NOW)
        self.assertIsNone(knowledge.find_project(store, "D Team"))


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.store, self.google, _ = scenario()
        knowledge.ensure_seeded(self.store)
        assistant.run(self.store, self.google, FakeLLM({}), now=NOW)  # fills the conversation cache
        self.consulted = []
        self.tools = Toolset(self.store, self.google, caller="assistant",
                             consult=lambda p, r: self.consulted.append((p, r)) or f"{p} says hi")

    def test_every_spec_has_an_implementation(self):
        for name, _, schema in SPECS:
            self.assertTrue(hasattr(Toolset, f"_t_{name}"), name)
            self.assertEqual(schema["type"], "object")

    def test_knowledge_tools_round_trip(self):
        self.tools.call("save_project", {"name": "EU SEO", "company": "superhairpieces", "goal": "Rank in NL"})
        self.tools.call("record_fact", {"text": "Ryan leads the EU team", "topic": "person"})
        hits = self.tools.call("search_knowledge", {"query": "EU Ryan"})["results"]  # "EU" is a real search term
        self.assertTrue({"projects", "facts"} <= {h["kind"] for h in hits})
        q = self.tools.call("ask_owner", {"question": "Who owns the NL site?", "why": "routing"})
        self.tools.call("answer_question", {"question_id": q["id"], "answer": "Ryan"})
        self.assertEqual(self.store.get_item("questions", q["id"])["status"], "answered")
        self.assertIn("Ryan", self.tools.call("company_overview", {})["overview"])

    def test_tasks_systems_status_autopilot(self):
        t = self.tools.call("create_task", {"title": "Call SkuVault", "priority": "high"})["task"]
        self.tools.call("update_task", {"task_id": t["id"], "status": "done"})
        self.assertEqual(self.store.get_task(t["id"])["status"], "done")
        self.tools.call("request_access", {"system": "SkuVault", "why": "stock levels"})
        self.assertEqual(self.store.get_item("systems", "skuvault")["status"], "requested")
        self.tools.call("request_access", {"system": "Shopify", "why": "Gen'C store"})
        self.assertTrue(any(s["name"] == "Shopify" for s in self.store.list_items("systems")))
        self.assertIn("note", self.tools.call("request_access", {"system": "Google Chat", "why": "x"}))
        self.tools.call("set_autopilot", {"on": False})
        self.assertFalse(self.tools.call("assistant_status", {})["autopilot_on"])

    def test_send_needs_confirmation(self):
        first = self.tools.call("send_chat_message", {"conversation": "Kathy", "text": "Shipped!", "confirmed": False})
        self.assertTrue(first["not_sent"])
        self.assertEqual(self.google.sent, [])  # nothing goes out without confirmation
        sent = self.tools.call("send_chat_message", {"conversation": "Kathy", "text": "Shipped!", "confirmed": True})
        self.assertTrue(sent["sent"])
        self.assertEqual(self.google.sent[-1], ("spaces/DM1", None, "Shipped!"))

    def test_conversations_and_bad_calls(self):
        names = [c["name"] for c in self.tools.call("list_conversations", {})["conversations"]]
        self.assertIn("Kathy", names)
        self.assertIn("error", self.tools.call("no_such_tool", {}))
        self.assertIn("error", self.tools.call("update_task", {"task_id": "nope"}))
        self.assertIn("error", self.tools.call("create_task", {"titel": "typo"}))  # bad arguments are reported
        self.assertEqual(self.tools.call("consult_partner", {"partner": "claude", "request": "plan?"})["answer"], "claude says hi")
        self.assertEqual(self.tools.log[-1]["tool"], "consult_partner")

    def test_no_google_is_a_clear_error(self):
        tools = Toolset(self.store, None)
        self.assertIn("isn't connected", tools.call("list_calendar", {})["error"])


class FakePartner:
    LABEL = "Fake"

    def __init__(self, text="Noted.", tool=None):
        self.text, self.tool, self.calls = text, tool, []

    def available(self, secrets):
        return True, ""

    def respond(self, system, history, toolset, secrets=None, model=None):
        self.calls.append({"system": system, "history": history, "tools": [s["name"] for s in toolset.specs()]})
        if self.tool:
            toolset.call(*self.tool)
        return {"text": self.text, "tools": toolset.log}

    def ping(self, secrets=None, **kw):
        return "ready"


class TalkTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        knowledge.ensure_seeded(self.store)
        self.fake = {name: FakePartner(f"{name} here") for name in talk.PARTNERS}
        patcher = mock.patch.dict(talk.PARTNERS, self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = talk.Talk(self.store, MemorySecrets(), lambda: None, "manne@superhairpieces.com")

    def test_each_partner_gets_its_role_context_and_tools(self):
        for name in talk.PARTNERS:
            reply_turn = self.service.ask(name, "What do you know?")
            self.assertEqual(reply_turn["text"], f"{name} here")
            call = self.fake[name].calls[-1]
            self.assertIn(talk.ROLE[name][:30], call["system"])
            self.assertIn("COMPANY BACKGROUND", call["system"])
            self.assertIn("OPEN QUESTIONS", call["system"])
            self.assertIn("consult_partner", call["tools"])
            self.assertEqual(len(self.store.get_talk(name)), 2)

    def test_history_is_kept_per_partner(self):
        self.service.ask("claude", "one")
        self.service.ask("claude", "two")
        self.assertEqual([t["text"] for t in self.fake["claude"].calls[-1]["history"]], ["one", "claude here", "two"])
        self.assertEqual(self.store.get_talk("chatgpt"), [])

    def test_consulted_partner_cannot_send_or_consult(self):
        answer = self.service.consult_fn("assistant")("claude", "Review this plan")
        self.assertEqual(answer, "claude here")
        tools = self.fake["claude"].calls[-1]["tools"]
        self.assertNotIn("send_chat_message", tools)
        self.assertNotIn("consult_partner", tools)
        self.assertIn("consulted by the company's AI assistant", self.fake["claude"].calls[-1]["system"])

    def test_partners_switch_off(self):
        self.store.update_settings({"partners_enabled": False})
        with self.assertRaises(RuntimeError):
            self.service.ask("chatgpt", "hi")
        self.assertIn("isn't available", self.service.consult_fn("assistant")("claude", "x"))
        self.service.ask("assistant", "still works")

    def test_digest_answer_uses_the_assistant_with_tools(self):
        self.fake["assistant"].tool = ("save_project", {"name": "Evolve Academy", "company": "superhairpieces"})
        q = self.store.list_items("questions")[0]
        result = self.service.digest_answer(q, "Evolve Academy trains Tier 1 stylists")
        self.assertIsNotNone(knowledge.find_project(self.store, "Evolve Academy"))
        self.assertNotIn("send_chat_message", self.fake["assistant"].calls[-1]["tools"])
        self.assertEqual(result["text"], "assistant here")


class FakeWS:
    """Stands in for a flask-sock connection: a script of inbound frames, a list of outbound ones."""

    def __init__(self, inbound):
        self.inbound = list(inbound)
        self.sent = []

    def receive(self, timeout=None):
        import time

        if self.inbound:
            item = self.inbound.pop(0)
            if isinstance(item, float):
                time.sleep(item)
                return None
            return item
        time.sleep(0.05)
        return json.dumps({"type": "stop"})

    def send(self, data):
        self.sent.append(data)


class FakeLiveSession:
    """Scripted Gemini Live session: one tool call, then spoken audio with transcripts."""

    def __init__(self, call=("record_fact", {"text": "Voice says: Jill covers outreach"})):
        self.call = call
        self.audio_in, self.tool_responses, self.texts = 0, [], []
        self._turns = asyncio.Queue()
        self._answered = False

    async def send_realtime_input(self, audio=None, **kw):
        self.audio_in += len(audio.data)
        if self.audio_in >= 1280 and not self._answered:  # answer once, after the first 40 ms of speech
            self._answered = True
            await self._turns.put("turn")

    async def send_client_content(self, turns=None, turn_complete=True):
        self.texts.append(turns.parts[0].text)

    async def send_tool_response(self, function_responses=None):
        self.tool_responses.extend(function_responses)

    async def receive(self):
        from google.genai import types

        await self._turns.get()
        yield types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[
            types.FunctionCall(id="c1", name=self.call[0], args=self.call[1])]))
        yield types.LiveServerMessage(server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text="Jill covers outreach")))
        yield types.LiveServerMessage(server_content=types.LiveServerContent(
            model_turn=types.Content(role="model", parts=[types.Part(inline_data=types.Blob(data=b"\x01\x02" * 480, mime_type="audio/pcm;rate=24000"))]),
            output_transcription=types.Transcription(text="Got it, noted.")))
        yield types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))


class VoiceTests(unittest.TestCase):
    def test_bridge_streams_audio_runs_tools_and_saves_the_turn(self):
        import voice

        store = MemoryStore()
        session = FakeLiveSession()

        class Connect:
            def __init__(self, config):
                self.config = config

            async def __aenter__(self):
                return session

            async def __aexit__(self, *exc):
                return False

        seen = {}

        def connect(config):
            seen["config"] = config
            return Connect(config)

        ws = FakeWS([b"\x00\x00" * 640, 0.05, b"\x00\x00" * 640, 0.3, json.dumps({"type": "text", "text": "hello"}), 0.2])
        bridge = voice.VoiceBridge(ws, store, Toolset(store, None, caller="assistant (voice)"), "system", connect=connect)
        bridge.run()

        frames = [json.loads(f) for f in ws.sent if isinstance(f, str)]
        audio = [f for f in ws.sent if isinstance(f, bytes)]
        self.assertEqual([f["type"] for f in frames[:2]], ["status", "ready"])
        self.assertIn({"type": "tool", "name": "record_fact"}, frames)
        self.assertIn({"type": "transcript", "who": "assistant", "text": "Got it, noted."}, frames)
        self.assertTrue(any(f["type"] == "turn_complete" for f in frames))
        self.assertEqual(sum(len(a) for a in audio), 960)
        self.assertEqual(session.audio_in, 2560)
        self.assertEqual(session.tool_responses[0].id, "c1")
        self.assertTrue(any("Jill covers outreach" in f["text"] for f in store.list_items("facts")))
        turns = store.get_talk("assistant")
        self.assertEqual([(t["role"], t["text"]) for t in turns][:2],
                         [("user", "Jill covers outreach"), ("assistant", "Got it, noted.")])
        self.assertTrue(turns[0]["voice"])
        self.assertEqual(session.texts, ["hello"])
        names = [d.name for d in seen["config"].tools[0].function_declarations]
        self.assertIn("record_fact", names)
        self.assertEqual(seen["config"].response_modalities, ["AUDIO"])


class RobustnessTests(unittest.TestCase):
    def test_event_times_with_and_without_offsets(self):
        from google_apis import Directory

        d = Directory({"users/3": {"name": "Sydney", "email": "sydney@superhairpieces.com"}})
        mixed = assistant._event_fields({"event_title": "Launch", "event_start": "2026-10-01T14:00:00-04:00",
                                         "event_end": "2026-10-01T15:00:00", "event_attendees": []}, d)
        self.assertEqual((mixed["event_start"], mixed["event_end"]), ("2026-10-01T14:00:00", "2026-10-01T15:00:00"))
        utc = assistant._event_fields({"event_title": "Launch", "event_start": "2026-10-01T18:00:00Z",
                                       "event_end": "", "event_attendees": []}, d)
        self.assertEqual((utc["event_start"], utc["event_end"]), ("2026-10-01T14:00:00", "2026-10-01T14:30:00"))

    def test_live_connect_timeout_is_reported(self):
        import voice

        class Hang:
            async def __aenter__(self):
                await asyncio.sleep(60)

            async def __aexit__(self, *exc):
                return False

        ws = FakeWS([0.05] * 40)
        with mock.patch.object(voice, "CONNECT_TIMEOUT", 0.3):
            voice.VoiceBridge(ws, MemoryStore(), Toolset(MemoryStore(), None), "s", connect=lambda c: Hang()).run()
        frames = [json.loads(f) for f in ws.sent]
        self.assertEqual(frames[0], {"type": "status", "text": "Connecting to Gemini Live…"})
        self.assertEqual(frames[-1]["type"], "error")
        self.assertIn("didn't answer", frames[-1]["message"])

    def test_voice_setup_failure_reaches_the_browser(self):
        import main

        class WS:
            def __init__(self):
                self.sent = []

            def send(self, data):
                self.sent.append(data)

        ws = WS()
        with main.app.test_request_context("/ws/voice", base_url="https://app.example",
                                           headers={"Origin": "https://app.example"}):
            from flask import session

            session["email"] = main.OWNER_EMAIL
            with mock.patch.object(main, "talk_service", side_effect=RuntimeError("store unavailable")):
                main.voice_ws(ws)
        messages = [json.loads(m) for m in ws.sent]
        self.assertEqual(messages[-1]["type"], "error")
        self.assertIn("store unavailable", messages[-1]["message"])


class ClaudeBackendTests(unittest.TestCase):
    """The Claude API first, Vertex AI second; a backend that can't serve at all is skipped."""

    @staticmethod
    def error(cls, status, message):
        import httpx

        return cls(message, response=httpx.Response(status, request=httpx.Request("POST", "https://x")), body=None)

    def clients(self, vertex, api):
        import partner_claude
        from types import SimpleNamespace

        def client(outcome):
            calls = []

            def create(**kwargs):
                calls.append(kwargs)
                if isinstance(outcome, Exception):
                    raise outcome
                return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=outcome)])
            return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)), calls=calls)

        fakes = {"vertex": client(vertex), "anthropic": client(api)}
        for patch in (mock.patch.object(partner_claude, "_clients", fakes),
                      mock.patch.object(partner_claude, "_working", None),
                      mock.patch.object(partner_claude, "CLAUDE_BACKEND", "auto")):
            patch.start()
            self.addCleanup(patch.stop)
        return partner_claude, fakes

    def test_the_claude_api_is_tried_first(self):
        claude, fakes = self.clients("vertex", "api")
        self.assertEqual(claude.ping(MemorySecrets()), "api")
        self.assertIn("fallbacks", fakes["anthropic"].calls[0])   # server-side fallback only on the Claude API
        self.assertEqual(fakes["vertex"].calls, [])

    def test_no_credit_falls_back_to_vertex(self):
        import anthropic

        claude, fakes = self.clients("ready", self.error(anthropic.BadRequestError, 400, "Your credit balance is too low"))
        self.assertEqual(claude.ping(MemorySecrets()), "ready")
        self.assertNotIn("fallbacks", fakes["vertex"].calls[0])
        claude.ping(MemorySecrets())
        self.assertEqual(len(fakes["anthropic"].calls), 1)         # the working backend is tried first next time

    def test_both_blocked_says_what_each_needs(self):
        import anthropic

        claude, _ = self.clients(self.error(anthropic.RateLimitError, 429, "Quota exceeded for aiplatform"),
                                 self.error(anthropic.BadRequestError, 400, "Your credit balance is too low"))
        with self.assertRaises(RuntimeError) as ctx:
            claude.ping(MemorySecrets())
        self.assertIn("quota", str(ctx.exception))
        self.assertIn("out of credit", str(ctx.exception))

    def test_an_ordinary_request_error_is_not_hidden(self):
        import anthropic

        claude, fakes = self.clients("ready", self.error(anthropic.BadRequestError, 400, "messages: invalid"))
        with self.assertRaises(anthropic.BadRequestError):
            claude.ping(MemorySecrets())
        self.assertEqual(fakes["vertex"].calls, [])

    def test_relay_tool_reports_a_partner_failure(self):
        import voice

        def ask(message):
            raise RuntimeError("Claude can't be reached. Vertex AI: no quota.")

        relay = voice.RelayTools("claude", "Claude", ask)
        self.assertIn("no quota", relay.call("ask_claude", {"message": "hi"})["error"])
        self.assertIn("error", relay.call("ask_claude", {"message": " "}))
        self.assertIn("error", relay.call("record_fact", {"text": "x"}))


class WebTests(unittest.TestCase):
    def setUp(self):
        import main

        self.main = main
        self.store, self.google, self.llm = scenario()
        knowledge.ensure_seeded(self.store)
        main.store = self.store
        main.secret_store = MemorySecrets()
        main.app.config["MAKE_GOOGLE"] = lambda: self.google
        main.app.config["TESTING"] = True
        self.fake = {name: FakePartner(f"{name} here") for name in talk.PARTNERS}
        patcher = mock.patch.dict(talk.PARTNERS, self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = main.app.test_client()
        with self.client.session_transaction() as s:
            s["email"] = main.OWNER_EMAIL

    def call(self, method, path, body=None):
        return self.client.open(path, method=method, json=body, headers={"X-Chat-Assistant": "1"})

    def test_knowledge_endpoint(self):
        data = self.call("GET", "/api/knowledge").get_json()
        self.assertEqual(len(data["questions"]), len(knowledge.SEED_QUESTIONS))
        self.assertEqual([p["name"] for p in data["partners"]], ["assistant", "claude", "chatgpt"])
        self.assertIn("money", [k for k, _ in data["autonomy_categories"]])

    def test_talk_endpoints(self):
        r = self.call("POST", "/api/talk/claude", {"message": "hello"}).get_json()
        self.assertEqual(r["reply"]["text"], "claude here")
        self.assertEqual(len(self.call("GET", "/api/talk/claude").get_json()["turns"]), 2)
        self.assertEqual(self.call("POST", "/api/talk/nobody", {"message": "x"}).status_code, 404)
        self.assertEqual(self.call("POST", "/api/talk/claude", {"message": " "}).status_code, 400)
        self.call("DELETE", "/api/talk/claude")
        self.assertEqual(self.call("GET", "/api/talk/claude").get_json()["turns"], [])

    def test_answering_a_question(self):
        q = self.store.list_items("questions")[0]
        r = self.call("POST", f"/api/questions/{q['id']}/answer", {"answer": "Gen'C sells cosmetics in Canada"}).get_json()
        self.assertEqual(r["question"]["status"], "answered")
        self.assertEqual(r["note"], "assistant here")
        self.assertTrue(any("cosmetics" in f["text"] for f in self.store.list_items("facts")))
        self.assertEqual(self.call("POST", f"/api/questions/{q['id']}/answer", {"answer": ""}).status_code, 400)

    def test_projects_systems_settings(self):
        p = self.call("POST", "/api/projects", {"name": "Brampton launch", "company": "superhairpieces"}).get_json()["project"]
        self.call("PATCH", f"/api/projects/{p['id']}", {"status": "blocked"})
        self.assertEqual(self.store.get_item("projects", p["id"])["status"], "blocked")
        self.call("PATCH", "/api/systems/hubspot", {"status": "connected"})
        self.assertEqual(self.store.get_item("systems", "hubspot")["status"], "connected")
        s = self.call("PATCH", "/api/settings", {"autonomy": {"money": "auto", "bogus": "auto", "calendar": "maybe"},
                                                 "openai_model": "gpt-x", "partners_enabled": False}).get_json()["settings"]
        self.assertEqual(s["autonomy"]["money"], "auto")
        self.assertNotIn("bogus", s["autonomy"])
        self.assertEqual(s["autonomy"]["calendar"], "auto")  # invalid value ignored, default kept
        self.assertEqual((s["openai_model"], s["partners_enabled"]), ("gpt-x", False))

    def test_partner_test_endpoint(self):
        results = self.call("POST", "/api/partners/test").get_json()["results"]
        self.assertTrue(all(r["ok"] for r in results.values()))

    def test_voice_socket_rejects_other_origins(self):
        class WS:
            sent = []

            def send(self, data):
                self.sent.append(data)

        ws = WS()
        with self.main.app.test_request_context("/ws/voice", headers={"Origin": "https://evil.example"}):
            from flask import session

            session["email"] = self.main.OWNER_EMAIL
            self.main.voice_ws(ws)
        self.assertIn("its own address", ws.sent[0])
        self.assertEqual(len(ws.sent), 1)  # nothing else happens for a foreign page

    def _voice_call(self, partner, session, inbound):
        class Connect:
            def __init__(self, config):
                seen["config"] = config

            async def __aenter__(self):
                return session

            async def __aexit__(self, *exc):
                return False

        seen = {}
        ws = FakeWS(inbound)
        with self.main.app.test_request_context(f"/ws/voice?partner={partner}", base_url="https://app.example",
                                                headers={"Origin": "https://app.example"}), \
                mock.patch.dict(self.main.app.config, {"VOICE_CONNECT": Connect}):
            from flask import session as flask_session

            flask_session["email"] = self.main.OWNER_EMAIL
            self.main.voice_ws(ws)
        return [json.loads(f) for f in ws.sent if isinstance(f, str)], seen.get("config")

    def test_voice_with_claude_is_relayed_to_claude(self):
        self.fake["claude"].LABEL = "Claude"
        session = FakeLiveSession(call=("ask_claude", {"message": "What should we tackle first?"}))
        frames, config = self._voice_call("claude", session, [b"\x00\x00" * 640, 0.3])
        self.assertIn({"type": "status", "text": "Preparing Claude…"}, frames)
        self.assertIn({"type": "tool", "name": "ask_claude"}, frames)
        self.assertEqual([d.name for d in config.tools[0].function_declarations], ["ask_claude"])
        self.assertEqual(session.tool_responses[0].response, {"answer": "claude here"})
        # Claude answered with its own prompt (in voice style) and saved the turns; the bridge saved nothing.
        self.assertIn("speaking out loud", self.fake["claude"].calls[-1]["system"])
        turns = self.store.get_talk("claude")
        self.assertEqual([(t["role"], t["text"], t["voice"]) for t in turns],
                         [("user", "What should we tackle first?", True), ("assistant", "claude here", True)])
        self.assertEqual(self.store.get_talk("assistant"), [])

    def test_voice_with_an_unavailable_partner_says_why(self):
        self.store.update_settings({"partners_enabled": False})
        frames, config = self._voice_call("chatgpt", FakeLiveSession(), [])
        self.assertIsNone(config)
        self.assertEqual(frames[-1]["type"], "error")
        self.assertIn("switched off", frames[-1]["message"])

    def test_page_and_state_carry_the_same_version(self):
        # An open tab compares these to notice a new deploy and reload itself.
        page = self.client.get("/").get_data(as_text=True)
        version = self.call("GET", "/api/state").get_json()["version"]
        self.assertIn(f'data-version="{version}"', page)


if __name__ == "__main__":
    unittest.main()
