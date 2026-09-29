"""Live voice conversation: browser <-> this service <-> Gemini Live (Vertex AI).

The browser opens a WebSocket to /ws/voice (session cookie authenticates it) and
streams microphone audio as binary frames of 16-bit PCM, mono, 16 kHz. This
bridge forwards them to a Gemini Live session and streams the spoken answer back
as binary 16-bit PCM at 24 kHz, plus JSON text frames:

  {"type": "ready"}                       session open, start talking
  {"type": "transcript", "who": "you"|"assistant", "text": ...}
  {"type": "tool", "name": ...}           the assistant is using a tool
  {"type": "interrupted"}                 you talked over it: drop queued audio
  {"type": "turn_complete"}
  {"type": "error", "message": ...}

Client -> server JSON: {"type": "text", "text": ...} to type instead of talk,
{"type": "stop"} to hang up. Tool calls run server-side with the same Toolset
the text chat uses; finished turns are saved into the assistant's conversation.

Claude and ChatGPT have no live voice here, so a call with one of them uses
Gemini Live only as ears and voice: RelayTools gives it a single tool that
hands Manne's words to the partner (its own model, tools and conversation) and
returns the answer to be read out. The partner saves those turns itself.
"""

import asyncio
import json
import os

from store import utcnow_iso
from tools import to_gemini_schema

GCP_PROJECT = os.environ.get("GCP_PROJECT", "shp-ai-bot-2026")
LIVE_LOCATION = os.environ.get("LIVE_LOCATION", "us-central1")
LIVE_MODEL = os.environ.get("LIVE_MODEL", "gemini-3.8-live")
LIVE_VOICE = os.environ.get("LIVE_VOICE", "")  # empty = the model's default voice
IN_RATE = 16000
CONNECT_TIMEOUT = 20


def live_config(system_instruction, specs):
    from google.genai import types

    decls = []
    for s in specs:
        kwargs = {"name": s["name"], "description": s["description"]}
        if s["parameters"].get("properties"):
            kwargs["parameters"] = to_gemini_schema(s["parameters"])
        decls.append(types.FunctionDeclaration(**kwargs))
    config = {
        "response_modalities": ["AUDIO"],
        "system_instruction": system_instruction,
        "tools": [types.Tool(function_declarations=decls)] if decls else [],
        "input_audio_transcription": types.AudioTranscriptionConfig(),
        "output_audio_transcription": types.AudioTranscriptionConfig(),
        # Sliding-window compression lets a conversation run past the raw context limit.
        "context_window_compression": types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
    }
    if LIVE_VOICE:
        config["speech_config"] = types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=LIVE_VOICE)))
    return types.LiveConnectConfig(**config)


def relay_instruction(label, tool):
    return f"""\
You are the voice line between Manne and {label}. You don't answer anything yourself and you have no
opinions of your own: {label} does all the thinking.

Every time Manne says something, call {tool} exactly once with what he said, word for word, in the language
he used (fix only obvious transcription slips). Say nothing while you wait. When the answer comes back, say
it out loud exactly as written, in a natural speaking voice: don't summarize, add, soften or leave anything
out, and don't read out markdown symbols. If {label} asks a question, ask it.

If you didn't catch what Manne said, ask him to repeat it instead of calling the tool. If the tool returns
an error, tell Manne in one sentence that {label} couldn't answer, and why.
"""


class RelayTools:
    """The one tool Gemini Live gets in a call with Claude or ChatGPT: pass the words on, bring the answer back."""

    def __init__(self, partner, label, ask):
        self.name = f"ask_{partner}"
        self.label = label
        self.ask = ask  # callable(message) -> the partner's answer text
        self.log = []

    def specs(self):
        return [{"name": self.name,
                 "description": f"Give Manne's words to {self.label} and get {self.label}'s answer to say out loud.",
                 "parameters": {"type": "object", "required": ["message"], "properties": {
                     "message": {"type": "string", "description": "What Manne said, word for word."}}}}]

    def call(self, name, args):
        if name != self.name:
            return {"error": f"Unknown tool {name}; use {self.name}."}
        message = str((args or {}).get("message", "")).strip()
        if not message:
            return {"error": "There was nothing to pass on."}
        try:
            return {"answer": self.ask(message)}
        except Exception as exc:  # noqa: BLE001 - read out to Manne instead of ending the call
            return {"error": f"{self.label} couldn't answer: {str(exc)[:300]}"}


def default_connect():
    from google import genai

    client = genai.Client(vertexai=True, project=GCP_PROJECT, location=LIVE_LOCATION)
    return lambda config: client.aio.live.connect(model=LIVE_MODEL, config=config)


class VoiceBridge:
    """One voice call. ws is a flask-sock/simple-websocket connection (blocking receive/send)."""

    def __init__(self, ws, store, toolset, system_instruction, connect=None, save_as="assistant"):
        self.ws = ws
        self.store = store
        self.toolset = toolset
        self.system_instruction = system_instruction
        self.connect = connect or default_connect()
        self.save_as = save_as  # conversation the finished turns go to; None when a relayed partner saves its own
        self.stopped = False
        self.turn = {"you": [], "assistant": []}

    def send_json(self, **payload):
        try:
            self.ws.send(json.dumps(payload))
        except Exception:  # noqa: BLE001 - the browser went away
            self.stopped = True

    def send_audio(self, data):
        try:
            self.ws.send(data)
        except Exception:  # noqa: BLE001
            self.stopped = True

    def run(self):
        try:
            asyncio.run(self._run())
        except Exception as exc:  # noqa: BLE001 - reported to the browser, then the call ends
            import traceback

            print(f"voice call failed: {exc!r}\n{traceback.format_exc()}")
            self.send_json(type="error", message=f"Voice stopped: {str(exc)[:300]}")
        finally:
            self._save_turn()

    async def _run(self):
        config = live_config(self.system_instruction, self.toolset.specs())
        self.send_json(type="status", text="Connecting to Gemini Live…")
        manager = self.connect(config)
        try:
            session = await asyncio.wait_for(manager.__aenter__(), timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"Gemini Live ({LIVE_MODEL}) didn't answer within {CONNECT_TIMEOUT} seconds") from exc
        try:
            self.send_json(type="ready", model=LIVE_MODEL)
            upstream = asyncio.create_task(self._upstream(session))
            downstream = asyncio.create_task(self._downstream(session))
            done, pending = await asyncio.wait({upstream, downstream}, return_when=asyncio.FIRST_COMPLETED)
            self.stopped = True
            for task in pending:
                task.cancel()
            for task in done:
                if task.exception():
                    raise task.exception()
        finally:
            await manager.__aexit__(None, None, None)

    async def _upstream(self, session):
        from google.genai import types

        loop = asyncio.get_running_loop()
        while not self.stopped:
            try:
                data = await loop.run_in_executor(None, self.ws.receive, 1.0)
            except Exception:  # noqa: BLE001 - socket closed
                return
            if data is None:
                continue
            if isinstance(data, (bytes, bytearray)):
                await session.send_realtime_input(audio=types.Blob(data=bytes(data), mime_type=f"audio/pcm;rate={IN_RATE}"))
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if msg.get("type") == "stop":
                return
            if msg.get("type") == "text" and msg.get("text"):
                self.turn["you"].append(msg["text"])
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=msg["text"])]), turn_complete=True)

    async def _downstream(self, session):
        from google.genai import types

        loop = asyncio.get_running_loop()
        while not self.stopped:
            async for message in session.receive():
                if message.tool_call:
                    responses = []
                    for call in message.tool_call.function_calls or []:
                        self.send_json(type="tool", name=call.name)
                        result = await loop.run_in_executor(None, self.toolset.call, call.name, dict(call.args or {}))
                        responses.append(types.FunctionResponse(id=call.id, name=call.name, response=result))
                    if responses:
                        await session.send_tool_response(function_responses=responses)
                content = message.server_content
                if message.go_away:
                    self.send_json(type="error", message="The voice session is ending; start a new call to continue.")
                if not content:
                    continue
                if content.interrupted:
                    self.send_json(type="interrupted")
                if content.model_turn:
                    for part in content.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data:
                            self.send_audio(part.inline_data.data)
                if content.input_transcription and content.input_transcription.text:
                    self.turn["you"].append(content.input_transcription.text)
                    self.send_json(type="transcript", who="you", text=content.input_transcription.text)
                if content.output_transcription and content.output_transcription.text:
                    self.turn["assistant"].append(content.output_transcription.text)
                    self.send_json(type="transcript", who="assistant", text=content.output_transcription.text)
                if content.turn_complete:
                    self._save_turn()
                    self.send_json(type="turn_complete")

    def _save_turn(self):
        you, said = "".join(self.turn["you"]).strip(), "".join(self.turn["assistant"]).strip()
        self.turn = {"you": [], "assistant": []}
        if not (you or said) or not self.save_as:
            return
        now = utcnow_iso()
        turns = []
        if you:
            turns.append({"role": "user", "text": you, "at": now, "voice": True})
        if said:
            turns.append({"role": "assistant", "text": said, "at": now, "voice": True, "tools": self.toolset.log[-10:]})
        self.store.append_talk(self.save_as, turns)
