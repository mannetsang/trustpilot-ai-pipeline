"""Claude as a partner: the Anthropic SDK with a manual tool-use loop.

Default backend is Claude on Google Vertex AI (AnthropicVertex), authenticated
with the service account's Application Default Credentials: no Anthropic key,
billed through Google Cloud. Claude must be enabled in Vertex AI Model Garden.
Set CLAUDE_BACKEND=anthropic to use the Claude API with the ANTHROPIC_API_KEY
secret instead.

Refusal fallback is on: if the requested model declines, the request is re-run
on CLAUDE_FALLBACK_MODEL (client-side middleware on Vertex, server-side
`fallbacks` on the Claude API).
"""

import json
import os

GCP_PROJECT = os.environ.get("GCP_PROJECT", "shp-ai-bot-2026")
CLAUDE_BACKEND = os.environ.get("CLAUDE_BACKEND", "vertex")  # vertex | anthropic
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_FALLBACK_MODEL = os.environ.get("CLAUDE_FALLBACK_MODEL", "claude-opus-4-8")
CLAUDE_REGION = os.environ.get("CLAUDE_REGION", "global")
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")  # Opus 5.5's default, stated explicitly
API_KEY_SECRET = "ANTHROPIC_API_KEY"
MAX_STEPS = 10

LABEL = "Claude"
_client = None


def available(secrets):
    if CLAUDE_BACKEND == "anthropic" and not secrets.get(API_KEY_SECRET):
        return False, f"Secret {API_KEY_SECRET} is missing or not readable"
    return True, ""


def client(secrets):
    global _client
    if _client is None:
        import anthropic

        if CLAUDE_BACKEND == "anthropic":
            _client = anthropic.Anthropic(api_key=secrets.get(API_KEY_SECRET))
        else:
            _client = anthropic.AnthropicVertex(
                project_id=GCP_PROJECT, region=CLAUDE_REGION,
                middleware=[anthropic.BetaRefusalFallbackMiddleware([{"model": CLAUDE_FALLBACK_MODEL}])])
    return _client


def _request_extras():
    if CLAUDE_BACKEND == "anthropic":
        return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    return {}


def _echoable(content):
    """Blocks to send back next turn. After a fallback boundary, only its text survives from before it."""
    last = max((i for i, b in enumerate(content) if b.type == "fallback"), default=-1)
    if last < 0:
        return content
    return [b for i, b in enumerate(content) if i > last or b.type == "text"]


def _text(content):
    return "".join(b.text for b in content if b.type == "text").strip()


def respond(system, history, toolset, secrets=None, model=None):
    import anthropic

    tools = [{"name": s["name"], "description": s["description"], "input_schema": s["parameters"]}
             for s in toolset.specs()]
    messages = [{"role": t["role"], "content": t["text"]} for t in history if t.get("text")]
    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    state = anthropic.BetaFallbackState()  # one per conversation: pins follow-ups to the model that accepted
    with state:
        for _ in range(MAX_STEPS):
            kwargs = {"model": model or CLAUDE_MODEL, "max_tokens": 16000, "system": system_blocks,
                      "messages": messages, "output_config": {"effort": CLAUDE_EFFORT}, **_request_extras()}
            if tools:
                kwargs["tools"] = tools
            response = client(secrets).beta.messages.create(**kwargs)

            if response.stop_reason == "refusal":
                return {"text": "Claude declined that request.", "tools": toolset.log}
            content = _echoable(response.content)
            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": content})
                continue
            tool_uses = [b for b in content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                suffix = " (cut off: the answer hit the length limit)" if response.stop_reason == "max_tokens" else ""
                return {"text": (_text(content) or "(no answer)") + suffix, "tools": toolset.log}

            messages.append({"role": "assistant", "content": content})
            results = []
            for block in tool_uses:
                result = toolset.call(block.name, block.input if isinstance(block.input, dict) else {})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(result, ensure_ascii=False, default=str),
                                "is_error": "error" in result})
            messages.append({"role": "user", "content": results})  # all results in one message
    return {"text": "I ran out of steps on that one. Ask me to continue.", "tools": toolset.log}


def ping(secrets=None):
    class _NoTools:
        log = []

        def specs(self):
            return []

    return respond("Reply with the single word: ready", [{"role": "user", "text": "ping"}], _NoTools(), secrets)["text"]
