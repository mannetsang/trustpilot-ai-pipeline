"""Live voice with ChatGPT itself: browser <-> this service <-> the OpenAI Realtime API.

The Gemini call (voice.VoiceBridge) and this one speak the same protocol to the
browser, so the page doesn't care which model is on the line. What differs is
upstream: OpenAI's Realtime models hear the audio directly (no transcript in
between), take 24 kHz PCM, and decide when you've finished with semantic turn
detection, which waits for the end of a thought rather than a fixed pause.

Tool calls run server-side with the same Toolset the typed ChatGPT chat uses,
and finished turns are saved into the ChatGPT conversation.
"""

import asyncio
import base64
import json
import os
from array import array

from voice import CONNECT_TIMEOUT, VoiceBridge

OPENAI_LIVE_MODEL = os.environ.get("OPENAI_LIVE_MODEL", "gpt-realtime-2.1")
OPENAI_LIVE_VOICE = os.environ.get("OPENAI_LIVE_VOICE", "")  # empty = the model's default voice
OPENAI_TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
OPENAI_LIVE_REASONING = os.environ.get("OPENAI_LIVE_REASONING", "low")  # minimal..xhigh; empty = model default
API_KEY_SECRET = os.environ.get("OPENAI_KEY_SECRET", "CHATGPT_API_KEY")
# Said last, where a long prompt's instructions carry most weight: unprompted, answers ran 26-37 s.
SPOKEN_REMINDER = """\
REMEMBER: this is a spoken conversation. Answer in two or three short sentences unless Manne asks for
more. Don't narrate what you're about to do ("let me think", "let me check"); just do it and say the result.
If there's more, offer it instead of reading it all out."""
RATE = 24000  # the Realtime API's PCM rate, in and out


class Upsampler:
    """16 kHz -> 24 kHz 16-bit mono, linear interpolation, continuous across chunks.

    Positions are counted in thirds of an input sample, so each output step is exactly 2 (24/16 = 3/2)
    and no rounding drift adds or drops samples at chunk edges.
    """

    def __init__(self):
        self.prev = 0  # last sample of the previous chunk
        self.t = 0     # read position in thirds of a sample, relative to self.prev

    def __call__(self, data):
        chunk = array("h")
        chunk.frombytes(data[: len(data) // 2 * 2])
        if not chunk:
            return b""
        src = [self.prev, *chunk]
        out, t, end = array("h"), self.t, 3 * (len(src) - 1)
        while t < end:
            i, frac = divmod(t, 3)
            out.append(src[i] + int((src[i + 1] - src[i]) * frac / 3))
            t += 2
        self.t, self.prev = t - end, src[-1]
        return out.tobytes()


def session_config(instructions, specs, hints=""):
    audio_in = {
        "format": {"type": "audio/pcm", "rate": RATE},
        "noise_reduction": {"type": "near_field"},
        # "low" eagerness waits for the end of a thought, so a pause mid-sentence doesn't cut Manne off.
        "turn_detection": {"type": "semantic_vad", "eagerness": "low", "create_response": True,
                           "interrupt_response": True},
        # Only for the on-screen transcript; the model itself hears the audio.
        "transcription": {"model": OPENAI_TRANSCRIBE_MODEL, **({"prompt": hints} if hints else {})},
    }
    audio_out = {"format": {"type": "audio/pcm", "rate": RATE}}
    if OPENAI_LIVE_VOICE:
        audio_out["voice"] = OPENAI_LIVE_VOICE
    config = {
        "type": "realtime",
        "instructions": f"{instructions}\n\n{SPOKEN_REMINDER}",
        "output_modalities": ["audio"],
        "audio": {"input": audio_in, "output": audio_out},
        "tools": [{"type": "function", "name": s["name"], "description": s["description"],
                   "parameters": s["parameters"]} for s in specs],
        "tool_choice": "auto",
    }
    if OPENAI_LIVE_REASONING:
        config["reasoning"] = {"effort": OPENAI_LIVE_REASONING}  # quicker answers; tools still do the lookups
    return config


def default_connect(secrets):
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=secrets.get(API_KEY_SECRET))
    return lambda: client.realtime.connect(model=OPENAI_LIVE_MODEL)


class RealtimeVoiceBridge(VoiceBridge):
    """One voice call with ChatGPT. Same browser protocol as VoiceBridge (see voice.py)."""

    def __init__(self, ws, store, toolset, instructions, connect, save_as="chatgpt", hints=""):
        super().__init__(ws, store, toolset, instructions, connect=connect, save_as=save_as)
        self.hints = hints
        self.upsample = Upsampler()

    async def _run(self):
        self.send_json(type="status", text="Connecting to ChatGPT…")
        manager = self.connect()
        try:
            conn = await asyncio.wait_for(manager.__aenter__(), timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"ChatGPT ({OPENAI_LIVE_MODEL}) didn't answer within {CONNECT_TIMEOUT} seconds") from exc
        try:
            await conn.session.update(session=session_config(self.system_instruction, self.toolset.specs(), self.hints))
            self.send_json(type="ready", model=OPENAI_LIVE_MODEL)
            upstream = asyncio.create_task(self._upstream(conn))
            downstream = asyncio.create_task(self._downstream(conn))
            done, pending = await asyncio.wait({upstream, downstream}, return_when=asyncio.FIRST_COMPLETED)
            self.stopped = True
            for task in pending:
                task.cancel()
            for task in done:
                if task.exception():
                    raise task.exception()
        finally:
            await manager.__aexit__(None, None, None)

    async def _upstream(self, conn):
        loop = asyncio.get_running_loop()
        while not self.stopped:
            try:
                data = await loop.run_in_executor(None, self.ws.receive, 1.0)
            except Exception:  # noqa: BLE001 - socket closed
                return
            if data is None:
                continue
            if isinstance(data, (bytes, bytearray)):
                pcm = self.upsample(bytes(data))
                if pcm:
                    await conn.input_audio_buffer.append(audio=base64.b64encode(pcm).decode("ascii"))
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if msg.get("type") == "stop":
                return
            if msg.get("type") == "text" and msg.get("text"):
                self.turn["you"].append(msg["text"])
                await conn.conversation.item.create(item={
                    "type": "message", "role": "user", "content": [{"type": "input_text", "text": msg["text"]}]})
                await conn.response.create()

    async def _downstream(self, conn):
        loop = asyncio.get_running_loop()
        answered_tools = False  # this response called tools: ask for the follow-up once it's done
        async for event in conn:
            kind = event.type
            if kind == "response.output_audio.delta":
                self.send_audio(base64.b64decode(event.delta))
            elif kind == "response.output_audio_transcript.delta":
                self.turn["assistant"].append(event.delta)
                self.send_json(type="transcript", who="assistant", text=event.delta)
            elif kind == "conversation.item.input_audio_transcription.completed":
                text = (event.transcript or "").strip()
                if text:
                    self.turn["you"].append(text + " ")
                    self.send_json(type="transcript", who="you", text=text + " ")
            elif kind == "input_audio_buffer.speech_started":
                self.send_json(type="interrupted")  # Manne is talking: stop playing the old answer
            elif kind == "response.function_call_arguments.done":
                self.send_json(type="tool", name=event.name)
                try:
                    args = json.loads(event.arguments or "{}")
                except ValueError:
                    result = {"error": "arguments were not valid JSON"}
                else:
                    result = await loop.run_in_executor(None, self.toolset.call, event.name, args)
                await conn.conversation.item.create(item={
                    "type": "function_call_output", "call_id": event.call_id,
                    "output": json.dumps(result, ensure_ascii=False, default=str)})
                answered_tools = True
            elif kind == "response.done":
                status = getattr(event.response, "status", None)
                if status == "failed":
                    details = getattr(event.response, "status_details", None)
                    error = getattr(details, "error", None)
                    self.send_json(type="error", message=f"ChatGPT couldn't answer: {getattr(error, 'message', None) or status}")
                if answered_tools:
                    answered_tools = False
                    await conn.response.create()  # let it speak with the tool results in hand
                    continue
                self._save_turn()
                self.send_json(type="turn_complete")
            elif kind == "error":
                error = event.error
                if getattr(error, "code", "") in ("response_cancel_not_active",):
                    continue  # harmless: an interruption raced a response that had already finished
                self.send_json(type="error", message=f"ChatGPT: {getattr(error, 'message', error)}")
            if self.stopped:
                return
