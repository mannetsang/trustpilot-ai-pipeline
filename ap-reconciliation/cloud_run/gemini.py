"""Gemini on Vertex AI (this project), with inline PDFs and images.

Same call shape as the review services' vertex_call(), plus JSON mode and
inline file parts. Auth is Application Default Credentials: the Cloud Run
runtime identity in production, the session or developer account locally.
"""

import base64
import json
import threading
import time

import requests


class GeminiError(RuntimeError):
    pass


class Gemini:
    def __init__(self, project, location="us-central1", extract_model="gemini-2.5-flash",
                 match_model="gemini-2.5-pro"):
        self.project, self.location = project, location
        self.extract_model, self.match_model = extract_model, match_model
        self._creds = None
        self._lock = threading.Lock()

    # -- auth ---------------------------------------------------------------
    def _token(self):
        import google.auth
        import google.auth.transport.requests
        with self._lock:
            if self._creds is None:
                self._creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            if not self._creds.valid:
                self._creds.refresh(google.auth.transport.requests.Request())
            return self._creds.token

    # -- parts --------------------------------------------------------------
    @staticmethod
    def text(value):
        return {"text": value}

    @staticmethod
    def inline(data, mime_type):
        return {"inlineData": {"mimeType": mime_type, "data": base64.b64encode(data).decode("ascii")}}

    # -- calls --------------------------------------------------------------
    def generate(self, parts, model=None, temperature=0.0, json_mode=True, timeout=300):
        """Return the model's text for a single-turn prompt made of `parts`."""
        model = model or self.extract_model
        url = (f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{self.project}"
               f"/locations/{self.location}/publishers/google/models/{model}:generateContent")
        config = {"temperature": temperature}
        if json_mode:
            config["responseMimeType"] = "application/json"
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": config}
        delays = (0, 3, 8, 15)
        last = None
        for attempt, delay in enumerate(delays):
            if delay:
                time.sleep(delay)
            try:
                r = requests.post(url, headers={"Authorization": f"Bearer {self._token()}"}, json=body, timeout=timeout)
            except requests.RequestException as exc:
                last = f"request failed: {exc}"
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"{r.status_code} {r.text[:200]}"
                continue
            if r.status_code != 200:
                raise GeminiError(f"Vertex {model} -> {r.status_code} {r.text[:300]}")
            data = r.json()
            candidates = data.get("candidates") or []
            if not candidates:
                raise GeminiError(f"Vertex {model}: no candidates ({json.dumps(data.get('promptFeedback', {}))[:200]})")
            parts_out = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts_out).strip()
            if not text:
                raise GeminiError(f"Vertex {model}: empty response (finishReason={candidates[0].get('finishReason')})")
            return text
        raise GeminiError(f"Vertex {model}: gave up after {len(delays)} attempts: {last}")

    def generate_json(self, parts, model=None, temperature=0.0, timeout=300):
        return parse_json(self.generate(parts, model=model, temperature=temperature, json_mode=True, timeout=timeout))


def parse_json(text):
    """Parse model output as JSON, tolerating code fences and leading prose."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (raw.find("{"), raw.find("[")) if i >= 0]
    if not starts:
        raise GeminiError(f"model returned no JSON: {raw[:120]!r}")
    first = min(starts)
    closer = "}" if raw[first] == "{" else "]"
    last = raw.rfind(closer)
    try:
        return json.loads(raw[first:last + 1])
    except json.JSONDecodeError as exc:
        raise GeminiError(f"model returned invalid JSON: {exc}: {raw[:120]!r}") from exc
