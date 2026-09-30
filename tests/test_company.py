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


class FakeRealtime:
    """Scripted OpenAI Realtime connection: hears speech, calls one tool, then answers out loud."""

    def __init__(self, call=("record_fact", {"text": "Voice says: Jill covers outreach"})):
        from types import SimpleNamespace as NS

        self.call = call
        self.updates, self.items, self.audio_in, self.follow_ups = [], [], 0, 0
        self._events = asyncio.Queue()
        self.session = NS(update=self._update)
        self.input_audio_buffer = NS(append=self._append)
        self.conversation = NS(item=NS(create=self._item))
        self.response = NS(create=self._response)

    async def _update(self, session):
        self.updates.append(session)

    async def _append(self, audio):
        import base64
        from types import SimpleNamespace as NS

        before, self.audio_in = self.audio_in, self.audio_in + len(base64.b64decode(audio))
        if before < 1920 <= self.audio_in:  # after the first 40 ms of (24 kHz) speech
            for e in (NS(type="input_audio_buffer.speech_started"),
                      NS(type="conversation.item.input_audio_transcription.completed", transcript="Jill covers outreach"),
                      NS(type="response.function_call_arguments.done", name=self.call[0], call_id="c1",
                         arguments=json.dumps(self.call[1])),
                      NS(type="response.done", response=NS(status="completed"))):
                await self._events.put(e)

    async def _item(self, item):
        self.items.append(item)

    async def _response(self, **kw):
        import base64
        from types import SimpleNamespace as NS

        self.follow_ups += 1
        for e in (NS(type="response.output_audio.delta", delta=base64.b64encode(b"\x01\x02" * 480).decode()),
                  NS(type="response.output_audio_transcript.delta", delta="Noted."),
                  NS(type="response.done", response=NS(status="completed"))):
            await self._events.put(e)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._events.get()


class FakeRealtimeConnect:
    def __init__(self, conn):
        self.conn = conn

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


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
        self.assertIsNone(seen["config"].realtime_input_config)  # the assistant keeps Gemini's own turn-taking
        names = [d.name for d in seen["config"].tools[0].function_declarations]
        self.assertIn("record_fact", names)
        self.assertEqual(seen["config"].response_modalities, ["AUDIO"])


class RealtimeVoiceTests(unittest.TestCase):
    def test_chatgpt_hears_runs_tools_answers_and_saves(self):
        import voice_openai

        store, conn = MemoryStore(), FakeRealtime()
        ws = FakeWS([b"\x00\x00" * 640, b"\x00\x00" * 640, 0.3])
        voice_openai.RealtimeVoiceBridge(ws, store, Toolset(store, None, caller="chatgpt (voice)"), "system",
                                         FakeRealtimeConnect(conn), hints="SkuVault, Ruvy").run()
        frames = [json.loads(f) for f in ws.sent if isinstance(f, str)]
        self.assertEqual([f["type"] for f in frames[:2]], ["status", "ready"])
        self.assertIn({"type": "tool", "name": "record_fact"}, frames)
        self.assertIn({"type": "transcript", "who": "assistant", "text": "Noted."}, frames)
        self.assertEqual(sum(len(f) for f in ws.sent if isinstance(f, bytes)), 960)
        self.assertEqual(conn.audio_in, 3840)  # 2 x 40 ms at 16 kHz arrive as 2 x 40 ms at 24 kHz
        self.assertEqual(conn.items[0]["type"], "function_call_output")
        self.assertEqual(conn.items[0]["call_id"], "c1")
        self.assertEqual(conn.follow_ups, 1)  # asked to answer once the tool result was in
        self.assertTrue(any("Jill covers outreach" in f["text"] for f in store.list_items("facts")))
        turns = store.get_talk("chatgpt")
        self.assertEqual([(t["role"], t["text"]) for t in turns], [("user", "Jill covers outreach"), ("assistant", "Noted.")])
        session = conn.updates[0]
        self.assertEqual(session["audio"]["input"]["turn_detection"], {
            "type": "server_vad", "silence_duration_ms": 1200, "create_response": True, "interrupt_response": True})
        self.assertEqual(session["audio"]["input"]["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertEqual(session["audio"]["input"]["transcription"]["prompt"], "SkuVault, Ruvy")
        self.assertIn("record_fact", [t["name"] for t in session["tools"]])
        self.assertTrue(session["instructions"].endswith(voice_openai.SPOKEN_REMINDER))

    def test_upsampler_is_continuous_across_chunks(self):
        from array import array

        import voice_openai

        ramp = array("h", range(0, 3200, 2))  # 1600 samples = 100 ms at 16 kHz
        up, out = voice_openai.Upsampler(), array("h")
        for i in range(0, len(ramp), 160):
            out.frombytes(up(ramp[i:i + 160].tobytes()))
        self.assertAlmostEqual(len(out), 2400, delta=2)                # 100 ms at 24 kHz
        self.assertTrue(all(b >= a for a, b in zip(out, out[1:])))    # no jumps back at chunk edges


class SpeechTests(unittest.TestCase):
    REPLY = ("## Brampton launch\n\nHere's where it stands:\n\n- **Ruvy** starts Monday\n- Evana needs [her badge](https://x.io/b)\n"
             "1. Check `SkuVault` at https://skuvault.com/app\n\n```\nprint('hi')\n```\nWant me to message Sydney?")

    def test_clean_for_speech_keeps_words_not_symbols(self):
        import speech

        spoken = speech.clean_for_speech(self.REPLY)
        for gone in ("#", "**", "- ", "`", "https://", "[", "print("):
            self.assertNotIn(gone, spoken)
        for kept in ("Brampton launch.", "Ruvy starts Monday.", "her badge", "SkuVault", "the link on screen",
                     "Want me to message Sydney?"):
            self.assertIn(kept, spoken)

    def test_split_keeps_every_word_in_bounded_parts(self):
        import speech

        text = " ".join(f"Sentence number {i} is about the Brampton salon launch and who runs it." for i in range(60))
        parts = speech.split_for_speech(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= speech.PART_CHARS for p in parts))
        self.assertEqual(" ".join(parts).split(), speech.clean_for_speech(text).split())
        self.assertEqual(speech.split_for_speech("  "), [])

    def test_voices_fallback_streaming_and_cache(self):
        import speech

        calls = []

        def fake(name, fail=False):
            def make(text, voice, secrets):
                calls.append((name, voice))
                if fail:
                    raise RuntimeError(f"{name} is down")
                yield b"\x01\x00" * 100
                yield b"\x02\x00" * 100
            return make

        long_reply = " ".join(["The Brampton launch is on track and Ruvy starts on Monday."] * 30)
        with mock.patch.dict(speech.PROVIDERS, {"gemini": fake("gemini"), "openai": fake("openai", fail=True)}):
            speaker = speech.Speaker(MemorySecrets())
            audio = b"".join(speaker.stream("assistant", long_reply))
            parts = len(speech.split_for_speech(long_reply))
            self.assertEqual(len(audio), 400 * parts)                  # every part, back to back
            self.assertEqual(calls, [("gemini", "Kore")] * parts)      # the Assistant's own (call) voice
            self.assertEqual(b"".join(speaker.stream("assistant", long_reply)), audio)
            self.assertEqual(len(calls), parts)                        # the second press is served from cache
            b"".join(speaker.stream("claude", "Hi from Claude."))
            self.assertEqual(calls[parts:], [("openai", "cedar"), ("gemini", "Charon")])  # OpenAI down: Gemini reads
            with self.assertRaises(RuntimeError):
                speaker.stream("chatgpt", "Hi from ChatGPT.")
            self.assertEqual(calls[-1], ("openai", "marin"))           # ChatGPT is OpenAI only: no Gemini stand-in
            with self.assertRaises(ValueError):
                speaker.stream("assistant", " ")
        with mock.patch.dict(speech.PROVIDERS, {"gemini": fake("gemini", True), "openai": fake("openai", True)}):
            with self.assertRaises(RuntimeError) as ctx:
                speech.Speaker(MemorySecrets()).stream("assistant", "Hello there.")
            self.assertIn("gemini is down", str(ctx.exception))
            self.assertIn("openai is down", str(ctx.exception))

    def test_a_stopped_reading_is_not_cached(self):
        import speech

        def slow(text, voice, secrets):
            yield from (b"\x00\x00" * 50 for _ in range(10))

        with mock.patch.dict(speech.PROVIDERS, {"gemini": slow}):
            speaker = speech.Speaker(MemorySecrets())
            chunks = speaker.stream("assistant", "Hello there.")
            next(chunks)
            chunks.close()  # the page hung up halfway
            self.assertEqual(len(speaker._cache), 0)


class IntegrationTests(unittest.TestCase):
    """Company systems through keys in Secret Manager: the key goes to its own host only and never to a model."""

    KEYS = {"BIGCOMMERCE_gmosz3ja_ACCESS_TOKEN": "bc-secret-token-ca", "NOTION_API_KEY": "notion-secret-key",
            "GENC_BIGCOMMERCE_PRODUCT_ACCESS_TOKEN": "genc-secret-token",
            "GENC_BIGCOMMERCE_PRODUCT_API_PATH": "https://api.bigcommerce.com/stores/genc123/v3/"}

    class Session:
        def __init__(self, status=200, body='{"domain": "superhairpieces.ca", "currency": "CAD"}'):
            self.calls, self.status, self.body = [], status, body

        def request(self, method, url, headers=None, auth=None, json=None, timeout=None, **kw):
            from types import SimpleNamespace as NS

            self.calls.append({"method": method, "url": url, "headers": headers, "json": json, **kw})
            body = self.routes.get(url, self.body) if hasattr(self, "routes") else self.body
            return NS(status_code=self.status, ok=self.status < 400, text=body)

    def setUp(self):
        import integrations

        self.integrations = integrations
        integrations._exchanged.clear()
        self.secrets = MemorySecrets(dict(self.KEYS))

    def test_read_goes_to_its_own_host_with_the_key_and_never_returns_it(self):
        session = self.Session(body='{"id": 7, "echo": "bc-secret-token-ca"}')
        result = self.integrations.call("bigcommerce_gmosz3ja", "GET", "/v2/orders", self.secrets,
                                        query={"limit": 5}, session=session)
        call = session.calls[0]
        self.assertEqual(call["url"], "https://api.bigcommerce.com/stores/gmosz3ja/v2/orders?limit=5")
        self.assertEqual(call["headers"]["X-Auth-Token"], "bc-secret-token-ca")
        self.assertEqual(result["data"], {"id": 7, "echo": "[secret]"})  # scrubbed even if a system echoes it
        self.assertNotIn("bc-secret-token-ca", json.dumps(result))

    def test_paths_cannot_leave_the_system(self):
        from urllib.parse import urlsplit

        session = self.Session()
        for path in ("https://evil.example/x", "/v2/../../x", "/v2/orders@evil.example", "/a b"):
            result = self.integrations.call("bigcommerce_gmosz3ja", "GET", path, self.secrets, session=session)
            self.assertIn("error", result, path)
        self.assertEqual(session.calls, [])
        # A protocol-relative path is just a path on the store's own host: the key still only goes there.
        self.integrations.call("bigcommerce_gmosz3ja", "GET", "//evil.example/x", self.secrets, session=session)
        self.assertEqual({urlsplit(c["url"]).netloc for c in session.calls}, {"api.bigcommerce.com"})

    def test_changes_wait_for_confirmation_but_searches_do_not(self):
        session = self.Session(body="{}")
        held = self.integrations.call("bigcommerce_gmosz3ja", "PUT", "/v3/catalog/products/1", self.secrets,
                                      body={"price": 1}, session=session)
        self.assertTrue(held["not_sent"])
        self.assertEqual(session.calls, [])
        self.integrations.call("notion", "POST", "/v1/search", self.secrets, body={"query": "launch"}, session=session)
        self.assertEqual(session.calls[-1]["method"], "POST")  # a search is a read
        self.integrations.call("bigcommerce_gmosz3ja", "PUT", "/v3/catalog/products/1", self.secrets,
                               body={"price": 1}, confirmed=True, session=session)
        self.assertEqual(session.calls[-1]["json"], {"price": 1})

    def test_a_key_the_app_cannot_read_is_named_not_guessed(self):
        session = self.Session()
        result = self.integrations.call("airtable", "GET", "/v0/meta/bases", self.secrets, session=session)
        self.assertIn("can't read the AIRTABLE_COMPANY_TOKEN secret", result["error"])
        self.assertEqual(session.calls, [])
        described = {d["id"]: d for d in self.integrations.describe(self.secrets)}
        self.assertEqual(described["bigcommerce_gmosz3ja"]["status"], "ready")
        self.assertEqual(described["airtable"]["status"], "no_access")
        self.assertNotIn("bc-secret-token-ca", json.dumps(described))

    def test_genc_store_path_comes_from_its_secret(self):
        session = self.Session()
        self.integrations.call("bigcommerce_genc", "GET", "/v2/store", self.secrets, session=session)
        self.assertEqual(session.calls[0]["url"], "https://api.bigcommerce.com/stores/genc123/v2/store")

    def test_check_all_updates_the_access_tab(self):
        store = MemoryStore()
        knowledge.ensure_seeded(store)
        results = self.integrations.check_all(self.secrets, store, session=self.Session())
        self.assertTrue(results["bigcommerce_gmosz3ja"]["ok"])
        self.assertEqual(results["bigcommerce_gmosz3ja"]["system"], "BigCommerce: superhairpieces.ca (CAD)")
        self.assertEqual(store.get_item("systems", "bigcommerce_ca")["status"], "connected")
        self.assertEqual(store.get_item("systems", "airtable")["status"], "no_access")
        self.assertIn("AIRTABLE_COMPANY_TOKEN", store.get_item("systems", "airtable")["why"])

    def test_tools_record_changes_and_consulted_partners_cannot_confirm(self):
        store = MemoryStore()
        session = self.Session(body="{}")
        with mock.patch.object(self.integrations.requests, "request", session.request):
            owner = Toolset(store, None, caller="chatgpt", secrets=self.secrets)
            owner.call("call_api", {"system": "bigcommerce_gmosz3ja", "method": "PUT", "path": "/v3/catalog/products/1",
                                    "body": {"price": 1}, "confirmed": True})
            self.assertEqual(store.list_actions()[0]["type"], "system_change")
            consulted = Toolset(store, None, caller="claude", secrets=self.secrets, may_change=False)
            held = consulted.call("call_api", {"system": "bigcommerce_gmosz3ja", "method": "DELETE",
                                               "path": "/v3/catalog/products/1", "confirmed": True})
        self.assertTrue(held["not_sent"])
        self.assertEqual(len(session.calls), 1)

    def test_skuvault_login_becomes_tokens_on_the_server(self):
        secrets = MemorySecrets({"SKUVAULT_EMAIL": "ops@superhairpieces.com", "SKUVAULT_PASSWORD": "pw-secret-123"})
        session = self.Session()
        session.routes = {"https://app.skuvault.com/api/gettokens": '{"TenantToken": "tenant-tok-1", "UserToken": "user-tok-1"}',
                          "https://app.skuvault.com/api/inventory/getWarehouses": '{"Warehouses": [{"Code": "TOR"}], "t": "tenant-tok-1"}'}
        result = self.integrations.call("skuvault", "POST", "/api/inventory/getWarehouses", secrets, body={}, session=session)
        self.assertEqual(session.calls[0]["json"], {"Email": "ops@superhairpieces.com", "Password": "pw-secret-123"})
        self.assertEqual(session.calls[1]["json"], {"TenantToken": "tenant-tok-1", "UserToken": "user-tok-1"})
        self.assertEqual(result["data"]["Warehouses"], [{"Code": "TOR"}])
        self.assertNotIn("tenant-tok-1", json.dumps(result))  # the exchanged tokens are scrubbed too
        self.integrations.call("skuvault", "POST", "/api/inventory/getWarehouses", secrets, body={}, session=session)
        self.assertEqual(len(session.calls), 3)                  # logged in once, tokens reused
        held = self.integrations.call("skuvault", "POST", "/api/products/updateProducts", secrets, body={}, session=session)
        self.assertTrue(held["not_sent"])                        # not a get*: a change

    def test_amazon_refresh_token_becomes_an_access_token(self):
        secrets = MemorySecrets({"AMAZON_TOKEN": "Atzr|refresh", "AMAZON_CLIENT_IDENTIFIER": "amzn1.app", "AMAZON_CLIENT_SECRET": "sec"})
        session = self.Session()
        session.routes = {"https://api.amazon.com/auth/o2/token": '{"access_token": "Atza|access", "expires_in": 3600}',
                          "https://sellingpartnerapi-na.amazon.com/sellers/v1/marketplaceParticipations": '{"payload": []}'}
        result = self.integrations.call("amazon", "GET", "/sellers/v1/marketplaceParticipations", secrets, session=session)
        self.assertEqual(session.calls[0]["data"]["grant_type"], "refresh_token")
        self.assertEqual(session.calls[1]["headers"]["x-amz-access-token"], "Atza|access")
        self.assertTrue(result["ok"])

    def test_google_tools_use_the_owner_sign_in(self):
        class Google:
            def auth_header(self):
                return {"Authorization": "Bearer ya29.owner"}

        session = self.Session(body='{"emailAddress": "manne@superhairpieces.com"}')
        result = self.integrations.call("gmail", "GET", "/gmail/v1/users/me/profile", None, session=session, google=Google())
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer ya29.owner")
        self.assertTrue(result["ok"])
        missing = self.integrations.call("gmail", "GET", "/gmail/v1/users/me/profile", None, session=session)
        self.assertIn("Google isn't connected", missing["error"])

    def test_a_system_with_several_parts_is_connected_only_when_all_work(self):
        class Google:
            def auth_header(self):
                return {"Authorization": "Bearer ya29.owner"}

        store = MemoryStore()
        session = self.Session()
        session.routes = {"https://analyticsadmin.googleapis.com/v1beta/accountSummaries": '{"accountSummaries": []}'}
        real_request = session.request

        def request(method, url, **kw):
            response = real_request(method, url, **kw)
            if "searchconsole" in url:
                response.status_code, response.ok, response.text = 403, False, '{"error": "API not enabled"}'
            return response

        session.request = request
        self.integrations.check_all(None, store, session=session, google=Google(), only={"ga4_admin", "search_console"})
        self.assertEqual(store.get_item("systems", "analytics")["status"], "error")
        self.assertIn("403", store.get_item("systems", "analytics")["why"])

    def test_bigcommerce_token_without_store_settings_still_connects(self):
        from types import SimpleNamespace as NS

        calls = []

        class Session:
            def request(self, method, url, headers=None, **kw):
                calls.append((url, headers["X-Auth-Token"]))
                if url.endswith("/v2/store"):
                    return NS(status_code=403, ok=False, text='{"title": "You don\'t have a required scope"}')
                return NS(status_code=200, ok=True, text='{"data": {"inventory_count": 812}}')

        store = MemoryStore()
        knowledge.ensure_seeded(store)
        secrets = MemorySecrets({"BIGCOMMERCE_gmosz3ja_ACCESS_TOKEN": "  bc-token-with-newline\r\n"})
        results = self.integrations.check_all(secrets, store, session=Session(), only={"bigcommerce_gmosz3ja"})
        self.assertTrue(results["bigcommerce_gmosz3ja"]["ok"])
        self.assertEqual([u.split("/gmosz3ja")[1] for u, _ in calls], ["/v2/store", "/v3/catalog/summary"])
        self.assertEqual(calls[0][1], "bc-token-with-newline")  # trimmed before it goes in a header
        self.assertEqual(store.get_item("systems", "bigcommerce_ca")["status"], "connected")

    def test_a_refused_key_stops_at_the_first_check(self):
        from types import SimpleNamespace as NS

        calls = []

        class Session:
            def request(self, method, url, **kw):
                calls.append(url)
                return NS(status_code=401, ok=False, text='{"title": "Unauthorized"}')

        result = self.integrations.probe(self.integrations.BY_ID["bigcommerce_gmosz3ja"], self.secrets, session=Session())
        self.assertEqual((result["status"], len(calls)), (401, 1))

    def test_teamdesk_needs_no_paste(self):
        info = self.integrations.connect_info("teamdesk")
        self.assertEqual(info["kind"], "key")
        session = self.Session(body="{}")
        self.integrations.call("teamdesk", "GET", "/describe.json", MemorySecrets({"TEAMDESK_TOKEN": "td-tok"}), session=session)
        self.assertEqual(session.calls[0]["url"], "https://www.teamdesk.net/secure/api/v2/56554/td-tok/describe.json")

    def test_connect_info_for_the_access_tab(self):
        hubspot = self.integrations.connect_info("hubspot")
        self.assertEqual((hubspot["kind"], hubspot["fields"][0]["secret"]), ("paste", "HUBSPOT_ACCESS_TOKEN"))
        self.assertIn("Private Apps", hubspot["help"])
        self.assertEqual(self.integrations.connect_info("bigcommerce_ca")["kind"], "key")
        self.assertEqual(self.integrations.connect_info("gmail")["kind"], "google")
        self.assertIsNone(self.integrations.connect_info("meta"))


class ResearchAndWorkTests(unittest.TestCase):
    """Web research through Firecrawl, and the assistant working its own board tasks."""

    def setUp(self):
        import integrations

        integrations._exchanged.clear()
        self.store = MemoryStore()
        knowledge.ensure_seeded(self.store)
        self.secrets = MemorySecrets({"FIRECRAWL_API": "fc-secret"})
        self.session = IntegrationTests.Session()
        self.session.routes = {
            "https://api.firecrawl.dev/v1/search": json.dumps({"success": True, "data": [
                {"title": "Hair System Prices 2026", "url": "https://example.com/prices", "description": "Compare..."}]}),
            "https://api.firecrawl.dev/v1/scrape": json.dumps({"success": True, "data": {
                "markdown": "# Prices\nA base costs $300.", "metadata": {"title": "Prices"}}}),
        }
        patcher = mock.patch.object(integrations.requests, "request", self.session.request)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_web_search_and_read_webpage(self):
        tools = Toolset(self.store, None, secrets=self.secrets)
        found = tools.call("web_search", {"query": "hair system prices canada", "limit": 50})
        self.assertEqual(found["results"][0]["url"], "https://example.com/prices")
        self.assertEqual(self.session.calls[0]["json"], {"query": "hair system prices canada", "limit": 10})
        self.assertEqual(self.session.calls[0]["headers"]["Authorization"], "Bearer fc-secret")
        page = tools.call("read_webpage", {"url": "https://example.com/prices"})
        self.assertIn("$300", page["text"])
        self.assertIn("error", tools.call("read_webpage", {"url": "file:///etc/passwd"}))
        self.assertIn("can't read the FIRECRAWL_API", Toolset(self.store, None).call("web_search", {"query": "x"})["error"])

    def test_working_alone_never_sends_a_message(self):
        _, google, _ = scenario()
        tools = Toolset(self.store, google, secrets=self.secrets, may_change=False)
        result = tools.call("send_chat_message", {"conversation": "spaces/S1", "text": "hi", "confirmed": True})
        self.assertTrue(result.get("not_sent"))

    def test_the_assistant_works_a_task_and_reports(self):
        import worker

        task = self.store.save_task("t1", {"title": "Find the three cheapest lace suppliers", "status": "todo",
                                           "priority": "high", "owner": "Assistant", "created_at": "2026-09-29T10:00:00"})
        fake = FakePartner("Found three suppliers: A ($12/m), B ($14/m), C ($15/m). Nothing needs your OK.",
                           tool=("update_task", {"task_id": "t1", "status": "done"}))
        with mock.patch.dict(talk.PARTNERS, {"assistant": fake}):
            service = talk.Talk(self.store, self.secrets, lambda: None, "manne@superhairpieces.com")
            outcome = worker.work_on(service, self.store, "t1")
        self.assertTrue(outcome["done"])
        prompt = fake.calls[-1]["history"][0]["text"]
        self.assertIn("Find the three cheapest lace suppliers", prompt)
        self.assertIn("web_search", fake.calls[-1]["tools"])
        saved = self.store.get_task("t1")
        self.assertEqual((saved["status"], saved["working_since"]), ("done", ""))
        self.assertIn("Found three suppliers", saved["result"])
        self.assertIn("update_task", saved["work_tools"])
        said = self.store.get_talk("assistant")[-1]
        self.assertEqual(said["task_id"], "t1")
        self.assertIn("(done)", said["text"])

    def test_the_hourly_run_picks_only_the_assistants_open_tasks(self):
        import worker
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=1)).isoformat()
        stale = (now - timedelta(hours=7)).isoformat()
        for tid, fields in {"mine": {"owner": "Assistant", "status": "todo"},
                            "stale": {"owner": "assistant", "status": "in_progress", "worked_at": stale},
                            "recent": {"owner": "Assistant", "status": "in_progress", "worked_at": recent},
                            "busy": {"owner": "Assistant", "status": "in_progress", "working_since": recent[:-6] and now.isoformat()},
                            "done": {"owner": "Assistant", "status": "done"},
                            "sydney": {"owner": "Sydney", "status": "todo"}}.items():
            self.store.save_task(tid, {"title": tid, **fields})
        due = sorted(t["id"] for t in self.store.list_tasks() if worker.due_for_work(t, now))
        self.assertEqual(due, ["mine", "stale"])
        with mock.patch.dict(talk.PARTNERS, {"assistant": FakePartner("Report.")}):
            service = talk.Talk(self.store, self.secrets, lambda: None, "manne@superhairpieces.com")
            worked = worker.work_due(service, self.store, limit=1)
        self.assertEqual(len(worked), 1)

    def test_partners_are_told_to_use_their_access(self):
        prompt = talk.system_prompt(self.store, "chatgpt")
        self.assertIn("live access to the company's systems and the web", prompt)
        self.assertIn("never say you\n  can't see something until you've tried", prompt)


class CloudSetupTests(unittest.TestCase):
    """The one owner approval: grant per secret, create the ones to paste, switch on Google APIs."""

    class Cloud:
        def __init__(self, existing, granted=()):
            self.existing, self.policies, self.calls = set(existing), {}, []
            for name in granted:
                self.policies[name] = {"bindings": [{"role": "roles/secretmanager.secretAccessor",
                                                     "members": ["serviceAccount:chat-assistant@shp-ai-bot-2026.iam.gserviceaccount.com"]}]}

        def request(self, method, url, headers=None, timeout=None, params=None, json=None):
            from types import SimpleNamespace as NS
            import json as _json

            self.calls.append((method, url.split("/v1/", 1)[-1], params, json))
            name = url.split("/secrets/", 1)[-1].split(":")[0] if "/secrets/" in url else None
            if url.endswith(":getIamPolicy"):
                body = self.policies.get(name, {"etag": "e"})
            elif url.endswith(":setIamPolicy"):
                self.policies[name] = json["policy"]
                body = json["policy"]
            elif url.endswith("/secrets") and method == "POST":
                self.existing.add(params["secretId"])
                body = {"name": params["secretId"]}
            elif name is not None:
                if name not in self.existing:
                    return NS(status_code=404, text='{"error": {"message": "not found"}}')
                body = {"name": name}
            else:
                body = {"name": "operations/1"}
            return NS(status_code=200, text=_json.dumps(body))

    def test_setup_grants_creates_and_enables(self):
        import cloud_setup

        existing = [n for n, pasted in cloud_setup.plan().items() if not pasted and n != "FIGMA_TOKEN"]
        cloud = self.Cloud(existing, granted=["AIRTABLE_COMPANY_TOKEN"])
        report = cloud_setup.run("ya29.owner-token", http=cloud)
        self.assertIn("HUBSPOT_ACCESS_TOKEN", report["created"])
        self.assertIn("FIGMA_TOKEN", report["missing"])          # should exist but doesn't: reported, not created
        self.assertIn("AIRTABLE_COMPANY_TOKEN", report["already"])
        self.assertIn("BIGCOMMERCE_gmosz3ja_ACCESS_TOKEN", report["granted"])
        self.assertEqual(report["errors"], [])
        roles = {b["role"] for b in cloud.policies["HUBSPOT_ACCESS_TOKEN"]["bindings"]}
        self.assertEqual(roles, {"roles/secretmanager.secretAccessor", "roles/secretmanager.secretVersionManager"})
        roles = {b["role"] for b in cloud.policies["BIGCOMMERCE_gmosz3ja_ACCESS_TOKEN"]["bindings"]}
        self.assertEqual(roles, {"roles/secretmanager.secretAccessor"})  # a stored key is only read
        self.assertIn("searchconsole.googleapis.com", report["enabled"])
        self.assertFalse(any(":setIamPolicy" in c[1] and "AIRTABLE" in c[1] for c in cloud.calls))  # nothing to change
        self.assertFalse(any("projects/shp-ai-bot-2026:setIamPolicy" in c[1] for c in cloud.calls))  # never project-wide


    def test_logins_are_used_on_the_server_only(self):
        import integrations

        self.assertIn("SKUVAULT_PASSWORD", integrations.secret_names())
        described = json.dumps(integrations.describe(MemorySecrets({"SKUVAULT_PASSWORD": "pw-secret-123"})))
        self.assertNotIn("pw-secret-123", described)


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
        models = {p["name"]: p["models"] for p in data["partners"]}
        self.assertEqual(models["chatgpt"], "OpenAI: gpt-5 when typing, gpt-realtime-2.1 on calls")
        self.assertNotIn("gemini", models["chatgpt"].lower())
        self.assertTrue(models["claude"].startswith("Anthropic: claude-opus-5-5"))
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
        with mock.patch.object(self.main, "CLAUDE_VOICE", "gemini"):  # Claude's call on Gemini's voice line
            frames, config = self._voice_call("claude", session, [b"\x00\x00" * 640, 0.3])
        self.assertIn({"type": "status", "text": "Preparing Claude…"}, frames)
        self.assertIn({"type": "tool", "name": "ask_claude"}, frames)
        self.assertEqual([d.name for d in config.tools[0].function_declarations], ["ask_claude"])
        self.assertEqual(session.tool_responses[0].response, {"answer": "claude here"})
        # A mid-sentence pause must not split one request into two messages to Claude.
        import voice

        self.assertEqual(config.realtime_input_config.automatic_activity_detection.silence_duration_ms,
                         voice.RELAY_END_SILENCE_MS)
        # The relay gets the company's names so it writes them down right.
        self.assertIn("Ridgeway", str(config.system_instruction))
        self.assertIn("SkuVault", str(config.system_instruction))
        # Claude answered with its own prompt (in voice style, warned about misheard names) and saved the
        # turns; the bridge saved nothing.
        self.assertIn("speaking out loud", self.fake["claude"].calls[-1]["system"])
        self.assertIn("speech recognition", self.fake["claude"].calls[-1]["system"])
        turns = self.store.get_talk("claude")
        self.assertEqual([(t["role"], t["text"], t["voice"]) for t in turns],
                         [("user", "What should we tackle first?", True), ("assistant", "claude here", True)])
        self.assertEqual(self.store.get_talk("assistant"), [])

    def test_voice_with_chatgpt_goes_to_chatgpt_itself(self):
        conn = FakeRealtime()
        with mock.patch.dict(self.main.app.config, {"OPENAI_VOICE_CONNECT": FakeRealtimeConnect(conn)}):
            frames, gemini_config = self._voice_call("chatgpt", FakeLiveSession(), [b"\x00\x00" * 640] * 2 + [0.3])
        self.assertIsNone(gemini_config)                       # Gemini isn't on this call at all
        self.assertIn({"type": "status", "text": "Connecting to ChatGPT…"}, frames)
        self.assertIn({"type": "ready", "model": "gpt-realtime-2.1"}, frames)
        self.assertIn("ChatGPT, made by OpenAI", conn.updates[0]["instructions"])
        self.assertEqual(conn.updates[0]["audio"]["output"]["voice"], "marin")  # the voice replies are read in
        self.assertNotIn("speech recognition", conn.updates[0]["instructions"])  # it hears the audio itself
        self.assertIn("Ruvy", conn.updates[0]["audio"]["input"]["transcription"]["prompt"])
        self.assertEqual([t["text"] for t in self.store.get_talk("chatgpt")], ["Jill covers outreach", "Noted."])

    def test_voice_with_claude_on_chatgpts_voice_line(self):
        self.fake["claude"].LABEL = "Claude"
        conn = FakeRealtime(call=("ask_claude", {"message": "What should we tackle first?"}))
        with mock.patch.dict(self.main.app.config, {"OPENAI_VOICE_CONNECT": FakeRealtimeConnect(conn)}):
            frames, gemini_config = self._voice_call("claude", FakeLiveSession(), [b"\x00\x00" * 640] * 2 + [0.3])
        self.assertIsNone(gemini_config)
        self.assertIn({"type": "status", "text": "Connecting to Claude…"}, frames)
        self.assertIn({"type": "ready", "model": "Claude, voice by gpt-realtime-2.1"}, frames)
        session = conn.updates[0]
        self.assertEqual([t["name"] for t in session["tools"]], ["ask_claude"])   # it can only pass words on
        self.assertIn("voice line between Manne and Claude", session["instructions"])
        self.assertNotIn("two or three short sentences", session["instructions"])  # reads Claude's answer in full
        self.assertEqual(session["audio"]["output"]["voice"], "cedar")             # Claude's own voice
        self.assertEqual(json.loads(conn.items[0]["output"]), {"answer": "claude here"})
        self.assertEqual([(t["role"], t["text"]) for t in self.store.get_talk("claude")],
                         [("user", "What should we tackle first?"), ("assistant", "claude here")])  # saved once

    def test_voice_with_an_unavailable_partner_says_why(self):
        self.store.update_settings({"partners_enabled": False})
        frames, config = self._voice_call("chatgpt", FakeLiveSession(), [])
        self.assertIsNone(config)
        self.assertEqual(frames[-1]["type"], "error")
        self.assertIn("switched off", frames[-1]["message"])

    def test_speak_endpoint(self):
        class FakeSpeaker:
            def stream(self, partner, text):
                if not text.strip():
                    raise ValueError("There's nothing to read out.")
                if partner == "chatgpt":
                    raise RuntimeError("Couldn't make speech. openai: down gemini: down")
                return iter([b"\x01\x00", b"\x02\x00"])

        with mock.patch.object(self.main, "_speaker", FakeSpeaker()):
            r = self.call("POST", "/api/speak", {"partner": "claude", "text": "Hello."})
            self.assertEqual((r.status_code, r.mimetype, r.headers["X-Sample-Rate"]), (200, "audio/pcm", "24000"))
            self.assertEqual(r.data, b"\x01\x00\x02\x00")
            failed = self.call("POST", "/api/speak", {"partner": "chatgpt", "text": "Hello."})
            self.assertEqual(failed.status_code, 502)
            self.assertIn("openai: down", failed.get_json()["error"])
            self.assertEqual(self.call("POST", "/api/speak", {"partner": "nobody", "text": "x"}).status_code, 404)
            self.assertEqual(self.call("POST", "/api/speak", {"partner": "claude", "text": " "}).status_code, 400)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("/static/speaker.js", page)

    def test_pasting_a_key_saves_it_and_tests_the_system(self):
        import integrations

        session = IntegrationTests.Session(body='{"results": []}')
        with mock.patch.object(integrations.requests, "request", session.request):
            r = self.call("POST", "/api/connect/hubspot", {"values": {"HUBSPOT_ACCESS_TOKEN": "pat-na1-secret"}})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["result"]["ok"])
        self.assertNotIn("pat-na1-secret", r.get_data(as_text=True))  # never echoed
        self.assertEqual(self.main.secret_store.get("HUBSPOT_ACCESS_TOKEN"), "pat-na1-secret")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer pat-na1-secret")
        self.assertEqual(self.store.get_item("systems", "hubspot")["status"], "connected")
        self.assertEqual(self.call("POST", "/api/connect/hubspot", {"values": {}}).status_code, 400)
        self.assertEqual(self.call("POST", "/api/connect/airtable", {"values": {"x": "y"}}).status_code, 404)

    def test_pasting_before_setup_asks_for_the_owner_approval(self):
        class PermissionDenied(Exception):
            pass

        with mock.patch.object(self.main.secret_store, "put", side_effect=PermissionDenied("no")):
            r = self.call("POST", "/api/connect/hubspot", {"values": {"HUBSPOT_ACCESS_TOKEN": "pat-na1-secret"}})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["url"], "/connect/setup?then=hubspot")

    def test_connect_everything_asks_google_for_owner_access(self):
        client = {"web": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "x",
                          "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}
        self.main.secret_store.put(self.main.OAUTH_CLIENT_SECRET_ID, json.dumps(client))
        r = self.client.get("/connect/setup?then=hubspot")
        self.assertEqual(r.status_code, 302)
        self.assertIn("accounts.google.com", r.location)
        self.assertIn("cloud-platform", r.location)
        self.assertNotIn("offline", r.location)  # no long-term owner token is ever asked for
        with self.client.session_transaction() as s:
            self.assertEqual((s["oauth"]["mode"], s["oauth"]["then"]), ("setup", "hubspot"))
        self.assertEqual(self.client.get("/connect/setup?then=../evil").status_code, 302)
        with self.client.session_transaction() as s:
            self.assertEqual(s["oauth"]["then"], "")

    def test_owner_approval_runs_setup_once_and_keeps_nothing(self):
        import cloud_setup
        from types import SimpleNamespace as NS

        class Flow:
            code_verifier = None
            client_config = {"client_id": "cid"}
            credentials = NS(token="ya29.owner-cloud-token", id_token="idt", refresh_token=None)

            def fetch_token(self, code):
                self.code = code

        with self.client.session_transaction() as s:
            s["oauth"] = {"state": "st8", "verifier": "v", "connect": False, "mode": "setup", "then": "hubspot"}
        report = {"granted": ["AIRTABLE_COMPANY_TOKEN"], "created": ["HUBSPOT_ACCESS_TOKEN"], "errors": []}
        with mock.patch.object(self.main, "make_flow", return_value=Flow()), \
                mock.patch("google.oauth2.id_token.verify_oauth2_token",
                           return_value={"email": self.main.OWNER_EMAIL, "email_verified": True, "sub": "1"}), \
                mock.patch.object(cloud_setup, "run", return_value=report) as run:
            r = self.client.get("/oauth/callback?state=st8&code=abc")
        run.assert_called_once_with("ya29.owner-cloud-token")
        self.assertEqual(r.location, "/?tab=access&setup=done&open=hubspot")
        self.assertEqual(self.store.get_flag("connect_setup")["created"], ["HUBSPOT_ACCESS_TOKEN"])
        with self.client.session_transaction() as s:
            self.assertEqual(s.get("email"), self.main.OWNER_EMAIL)   # still signed in
            self.assertNotIn("ya29.owner-cloud-token", json.dumps(dict(s)))  # the owner token is gone
        self.assertNotIn("ya29.owner-cloud-token", json.dumps(self.store.get_flag("connect_setup")))

    def test_give_a_task_to_the_assistant(self):
        self.store.save_task("t9", {"title": "Check yesterday's CA orders", "status": "todo"})
        self.fake["assistant"].text = "12 orders yesterday, CAD 4,380."
        r = self.call("POST", "/api/tasks/t9/work")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["task"]["owner"], "Assistant")
        self.assertEqual(body["report"], "12 orders yesterday, CAD 4,380.")
        self.assertEqual(self.call("POST", "/api/tasks/nope/work").status_code, 404)
        self.store.save_task("t9", {"working_since": __import__("store").utcnow_iso()})
        self.assertEqual(self.call("POST", "/api/tasks/t9/work").status_code, 409)  # already on it

    def test_access_tab_rows_carry_how_to_connect(self):
        systems = {s["id"]: s for s in self.call("GET", "/api/knowledge").get_json()["systems"]}
        self.assertEqual(systems["hubspot"]["connect"]["kind"], "paste")
        self.assertEqual(systems["skuvault"]["connect"]["kind"], "key")
        self.assertEqual(systems["gmail"]["connect"]["kind"], "google")

    def test_hidden_elements_stay_hidden(self):
        # .voicebar sets display: flex, which beats the browser's own [hidden] rule without this.
        self.assertIn("[hidden] { display: none !important; }", self.client.get("/").get_data(as_text=True))

    def test_a_failed_run_says_where(self):
        with mock.patch.object(self.main.assistant, "run", side_effect=lambda *a, **k: self.main.assistant.parse_ts(5)):
            body, status = self.main.do_run("manual")
        self.assertEqual(status, 500)
        self.assertRegex(body["error"], r"\(at assistant\.py:\d+\)$")
        self.assertIn("Traceback", body["trace"])

    def test_page_and_state_carry_the_same_version(self):
        # An open tab compares these to notice a new deploy and reload itself.
        page = self.client.get("/").get_data(as_text=True)
        version = self.call("GET", "/api/state").get_json()["version"]
        self.assertIn(f'data-version="{version}"', page)


if __name__ == "__main__":
    unittest.main()
