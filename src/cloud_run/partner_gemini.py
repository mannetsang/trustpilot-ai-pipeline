"""The assistant's own voice in text: Gemini on Vertex AI with function calling.

Same REST call pattern as llm.py (service-account credentials, no API key). The
loop runs tool calls until Gemini answers in text or MAX_STEPS is reached. Model
turns are appended unchanged so thought signatures survive between steps.
"""

import os

import requests

from tools import to_gemini_schema

GCP_PROJECT = os.environ.get("GCP_PROJECT", "shp-ai-bot-2026")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
TALK_MODEL = os.environ.get("TALK_MODEL", os.environ.get("GEMINI_MODEL", "gemini-2.5-pro"))
VISION_MODEL = os.environ.get("VISION_MODEL", "gemini-2.5-flash")  # describes screenshots on live calls
MAX_STEPS = 24  # research chains many lookups: search, read, query, compare

LABEL = "Man AI (Gemini)"
_creds = None


def available(secrets):
    return True, ""


def _token():
    global _creds
    import google.auth
    from google.auth.transport.requests import Request

    if _creds is None:
        _creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not _creds.valid:
        _creds.refresh(Request())
    return _creds.token


def declarations(specs):
    out = []
    for s in specs:
        decl = {"name": s["name"], "description": s["description"]}
        if s["parameters"].get("properties"):
            decl["parameters"] = to_gemini_schema(s["parameters"])
        out.append(decl)
    return out


def respond(system, history, toolset, secrets=None, model=None):
    url = (f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT}/locations/{VERTEX_LOCATION}"
           f"/publishers/google/models/{model or TALK_MODEL}:generateContent")
    contents = [{"role": "model" if t["role"] == "assistant" else "user", "parts": [{"text": t["text"]}]}
                for t in history if t.get("text")]
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
            "generationConfig": {"temperature": 0.4}}
    decls = declarations(toolset.specs())
    if decls:
        body["tools"] = [{"functionDeclarations": decls}]

    for _ in range(MAX_STEPS):
        resp = requests.post(url, headers={"Authorization": f"Bearer {_token()}"}, json=body, timeout=240)
        if resp.status_code >= 400:
            raise RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
        candidate = (resp.json().get("candidates") or [{}])[0]
        content = candidate.get("content") or {"role": "model", "parts": []}
        parts = content.get("parts") or []
        calls = [p["functionCall"] for p in parts if p.get("functionCall")]
        if not calls:
            text = "".join(p.get("text", "") for p in parts if p.get("text") and not p.get("thought")).strip()
            return {"text": text or "(no answer)", "tools": toolset.log}
        body["contents"].append({"role": "model", "parts": parts})
        responses = []
        for call in calls:
            result = toolset.call(call.get("name", ""), call.get("args") or {})
            fr = {"name": call.get("name", ""), "response": result}
            if call.get("id"):
                fr["id"] = call["id"]
            responses.append({"functionResponse": fr})
        # Screenshots (the browser tool) go in the same turn, right after the results they belong to.
        responses += [{"inlineData": {"mimeType": i["mime"], "data": i["data"]}}
                      for i in getattr(toolset, "take_images", lambda: [])()]
        body["contents"].append({"role": "user", "parts": responses})
    return {"text": "I ran out of steps on that one. Ask me to continue.", "tools": toolset.log}


def describe_image(data, instruction):
    """A short description of one base64 JPEG, for a caller that can't see images (a live voice call)."""
    url = (f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT}/locations/{VERTEX_LOCATION}"
           f"/publishers/google/models/{VISION_MODEL}:generateContent")
    body = {"contents": [{"role": "user", "parts": [{"text": instruction + " Keep it under 120 words."},
                                                    {"inlineData": {"mimeType": "image/jpeg", "data": data}}]}],
            "generationConfig": {"temperature": 0.2}}
    try:
        resp = requests.post(url, headers={"Authorization": f"Bearer {_token()}"}, json=body, timeout=60)
        resp.raise_for_status()
        parts = ((resp.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip() or "(no description)"
    except Exception as exc:  # noqa: BLE001 - the rest of the result (text, elements) still helps
        return f"(couldn't describe the screenshot: {str(exc)[:150]})"


def ping(secrets=None):
    """One tiny call to confirm the model answers (used by the partner test)."""
    class _NoTools:
        log = []

        def specs(self):
            return []

    return respond("Reply with the single word: ready", [{"role": "user", "text": "ping"}], _NoTools())["text"]
