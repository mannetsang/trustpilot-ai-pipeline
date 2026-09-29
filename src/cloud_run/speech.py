"""Read replies aloud: text -> speech for the 🔊 button and the "Read replies aloud" switch.

Each bot reads in the same voice it has on a live call (VOICES): the Assistant with
Gemini text-to-speech on Vertex AI, Claude and ChatGPT with OpenAI's. If a provider
fails before speaking, the other one reads instead, except for ChatGPT, which is
OpenAI only.

Speech is streamed: the page starts playing as the first audio arrives (about 1.6 s
with Gemini, 0.5 s with OpenAI) instead of waiting for a whole reply, which took
5-8 s. Both providers generate faster than real time, so playback never catches
up. Replies are cleaned for listening (no markdown symbols or URLs) and read in
paragraph-sized parts, one after another, in the same stream. Finished readings
are cached, so pressing 🔊 again on the same reply costs nothing.
"""

import hashlib
import os
import re
import threading
from collections import OrderedDict

GCP_PROJECT = os.environ.get("GCP_PROJECT", "shp-ai-bot-2026")
GEMINI_TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-2.5-flash-tts")
GEMINI_TTS_LOCATION = os.environ.get("GEMINI_TTS_LOCATION", "global")
OPENAI_TTS_MODEL = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_KEY_SECRET = os.environ.get("OPENAI_KEY_SECRET", "CHATGPT_API_KEY")
RATE = 24000  # both providers' PCM rate

# Each bot has one voice, on calls and when a reply is read aloud (main.py passes these to the live bridges).
# partner -> (provider, voice); if that provider fails, FALLBACK reads instead.
VOICES = {
    "assistant": ("gemini", os.environ.get("VOICE_ASSISTANT", "Kore")),   # Gemini Live on calls
    "claude": ("openai", os.environ.get("VOICE_CLAUDE", "cedar")),        # ChatGPT's voice line on calls
    "chatgpt": ("openai", os.environ.get("VOICE_CHATGPT", "marin")),      # OpenAI Realtime on calls
}
# ChatGPT has none: clicking ChatGPT means OpenAI only, so a failed reading says so rather than use Gemini.
FALLBACK = {"assistant": ("openai", "sage"), "claude": ("gemini", "Charon"), "chatgpt": None}
STYLE = "Read this aloud in a warm, natural, conversational voice, like a helpful colleague:"
PART_CHARS = 800   # per provider request; parts are read back to back in one stream
MAX_CHARS = 20000


def clean_for_speech(text):
    """Markdown and links read badly out loud: keep the words, drop the symbols."""
    text = re.sub(r"```.*?```", " (There's code here; it's on screen.) ", text or "", flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)          # [label](url) -> label
    text = re.sub(r"https?://\S+", "the link on screen", text)
    text = re.sub(r"(\*\*|__|~~)", "", text)
    text = re.sub(r"(?<!\w)[*_](\S[^*_\n]*?)[*_](?!\w)", r"\1", text)  # *emphasis* / _emphasis_
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(#{1,6}\s+|[-*•]\s+|\d+[.)]\s+|>\s*)", "", line).strip()
        if line.startswith("|"):
            line = " ".join(c.strip() for c in line.strip("|").split("|") if c.strip() and not set(c.strip()) <= set("-:"))
        if line:
            lines.append(line if line[-1] in ".!?:;," else line + ".")  # a pause between list items
    return re.sub(r"\s+", " ", " ".join(lines)).strip()[:MAX_CHARS]


def split_for_speech(text):
    """Sentence-aligned parts of at most PART_CHARS, for one provider request each."""
    parts, current = [], ""
    for sentence in (s for s in re.split(r"(?<=[.!?])\s+", clean_for_speech(text)) if s):
        if current and len(current) + 1 + len(sentence) > PART_CHARS:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
        while len(current) > PART_CHARS:  # one enormous "sentence": cut at a space
            cut = current.rfind(" ", 0, PART_CHARS)
            cut = cut if cut > 0 else PART_CHARS
            parts.append(current[:cut])
            current = current[cut:].strip()
    if current:
        parts.append(current)
    return parts


def _gemini(text, voice, secrets):
    """Yields 24 kHz 16-bit mono PCM as Gemini generates it."""
    from google import genai
    from google.genai import types

    client = genai.Client(vertexai=True, project=GCP_PROJECT, location=GEMINI_TTS_LOCATION)
    stream = client.models.generate_content_stream(
        model=GEMINI_TTS_MODEL, contents=f"{STYLE}\n\n{text}",
        config=types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)))))
    for chunk in stream:
        content = chunk.candidates[0].content if chunk.candidates else None
        for part in (content.parts or []) if content else []:
            data = part.inline_data
            if data and data.data:
                rate = re.search(r"rate=(\d+)", data.mime_type or "")
                if rate and int(rate[1]) != RATE:
                    raise RuntimeError(f"unexpected sample rate {rate[1]}")
                yield data.data


def _openai(text, voice, secrets):
    """Yields 24 kHz 16-bit mono PCM as OpenAI generates it."""
    from openai import OpenAI

    key = secrets.get(OPENAI_KEY_SECRET) if secrets else None
    if not key:
        raise RuntimeError(f"no readable {OPENAI_KEY_SECRET} secret")
    with OpenAI(api_key=key).audio.speech.with_streaming_response.create(
            model=OPENAI_TTS_MODEL, voice=voice, input=text, response_format="pcm",
            instructions="Speak warmly and naturally, like a helpful colleague.") as response:
        yield from response.iter_bytes(4800)


PROVIDERS = {"gemini": _gemini, "openai": _openai}


class Speaker:
    def __init__(self, secrets, cache_size=32):
        self.secrets = secrets
        self.cache_size = cache_size
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def stream(self, partner, text):
        """Checks the request, then returns an iterator of PCM chunks whose first chunk is already made.

        Making the first chunk up front means a provider failure (after trying the other one) raises
        here, while an error can still be an HTTP status, rather than halfway through a stream.
        Raises ValueError for a bad request, RuntimeError when neither provider can speak.
        """
        parts = split_for_speech(text)
        if not parts:
            raise ValueError("There's nothing to read out.")
        provider, voice = VOICES.get(partner, VOICES["assistant"])
        key = hashlib.sha256(f"{provider}|{voice}|{' '.join(parts)}".encode()).hexdigest()
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return iter([self._cache[key]])
        attempts = [(provider, voice)] + ([FALLBACK[partner]] if FALLBACK.get(partner) else [])
        problems = []
        for name, name_voice in attempts:
            chunks = PROVIDERS[name](parts[0], name_voice, self.secrets)
            try:
                first = next(chunks)
            except Exception as exc:  # noqa: BLE001 - StopIteration included: no audio at all
                problems.append(f"{name}: {str(exc)[:200] or 'no audio'}")
                continue
            return self._rest(key, name, name_voice, parts, first, chunks)
        raise RuntimeError("Couldn't make speech. " + " ".join(problems))

    def _rest(self, key, name, voice, parts, first, chunks):
        made, complete = [first], False
        try:
            yield first
            for chunk in chunks:
                made.append(chunk)
                yield chunk
            for text in parts[1:]:
                for chunk in PROVIDERS[name](text, voice, self.secrets):
                    made.append(chunk)
                    yield chunk
            complete = True
        finally:
            if complete:  # a reading cut short (stopped, or failed) isn't worth replaying
                with self._lock:
                    self._cache[key] = b"".join(made)
                    while len(self._cache) > self.cache_size:
                        self._cache.popitem(last=False)
