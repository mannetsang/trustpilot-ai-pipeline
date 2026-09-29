"""Claude as a partner: the Anthropic SDK with a manual tool-use loop.

Two ways to reach Claude, tried in order (CLAUDE_BACKEND=auto, the default):
the Claude API with the ANTHROPIC_API_KEY secret (Anthropic's own service, where
new features land first), then Google Vertex AI (AnthropicVertex, the service
account's credentials, billed through Google Cloud; needs Claude enabled in
Model Garden and Claude quota on the project). A way that can't serve at all
(no key, no credit, no quota) is skipped and the other one used; if neither
works, the error says what each one needs. Set CLAUDE_BACKEND=anthropic or
=vertex to use only one.

Refusal fallback is on: if the requested model declines, the request is re-run
on CLAUDE_FALLBACK_MODEL (client-side middleware on Vertex, server-side
`fallbacks` on the Claude API).
"""

import json
import os

GCP_PROJECT = os.environ.get("GCP_PROJECT", "shp-ai-bot-2026")
CLAUDE_BACKEND = os.environ.get("CLAUDE_BACKEND", "auto")  # auto | vertex | anthropic
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_FALLBACK_MODEL = os.environ.get("CLAUDE_FALLBACK_MODEL", "claude-opus-4-8")
CLAUDE_REGION = os.environ.get("CLAUDE_REGION", "global")
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")  # Opus 5.5's default, stated explicitly
API_KEY_SECRET = "ANTHROPIC_API_KEY"
MAX_STEPS = 24  # research chains many lookups: search, read, query, compare

LABEL = "Claude"
_clients = {}
_working = None  # the backend that last answered; tried first next time


class _Unusable(Exception):
    """This backend can't serve any request right now (as opposed to a problem with one request)."""


def available(secrets):
    if CLAUDE_BACKEND == "anthropic" and not _key(secrets):
        return False, f"Secret {API_KEY_SECRET} is missing or not readable"
    return True, ""


def _key(secrets):
    try:
        return secrets.get(API_KEY_SECRET) if secrets else None
    except Exception:  # noqa: BLE001 - no access to the secret counts as no key
        return None


def _backends():
    if CLAUDE_BACKEND in ("vertex", "anthropic"):
        return [CLAUDE_BACKEND]
    return sorted(["anthropic", "vertex"], key=lambda b: b != _working)  # stable: Claude API first


def _client(backend, secrets):
    if backend not in _clients:
        import anthropic

        if backend == "anthropic":
            key = _key(secrets)
            if not key:
                raise _Unusable(f"Claude API: there's no readable {API_KEY_SECRET} secret.")
            _clients[backend] = anthropic.Anthropic(api_key=key)
        else:
            _clients[backend] = anthropic.AnthropicVertex(
                project_id=GCP_PROJECT, region=CLAUDE_REGION,
                middleware=[anthropic.BetaRefusalFallbackMiddleware([{"model": CLAUDE_FALLBACK_MODEL}])])
    return _clients[backend]


def _why_unusable(backend, exc):
    """A plain reason when exc means the backend can't serve at all; None for an ordinary request error."""
    import anthropic

    text = str(exc)
    if backend == "vertex":
        if isinstance(exc, anthropic.RateLimitError) and "Quota exceeded" in text:
            return ("Vertex AI: Google Cloud hasn't given this project Claude quota (request an increase for "
                    "global_online_prediction_requests_per_base_model, base model anthropic-claude-opus, "
                    "under IAM & Admin > Quotas).")
        if isinstance(exc, anthropic.NotFoundError):
            return f"Vertex AI: {CLAUDE_MODEL} isn't enabled in Model Garden for this project."
        if isinstance(exc, anthropic.PermissionDeniedError):
            return "Vertex AI: the app's service account isn't allowed to use it."
        return None
    if isinstance(exc, anthropic.BadRequestError) and "credit balance" in text:
        return "Claude API: the Anthropic account behind ANTHROPIC_API_KEY is out of credit (Plans & Billing)."
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return "Claude API: ANTHROPIC_API_KEY was rejected."
    return None


def _create(secrets, **kwargs):
    global _working
    problems = []
    for backend in _backends():
        extras = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if backend == "anthropic" else {}
        try:
            response = _client(backend, secrets).beta.messages.create(**kwargs, **extras)
        except _Unusable as exc:
            problems.append(str(exc))
            continue
        except Exception as exc:
            why = _why_unusable(backend, exc)
            if why is None:
                raise
            problems.append(why)
            continue
        _working = backend
        return response
    raise RuntimeError("Claude can't be reached. " + " ".join(problems))


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
                      "messages": messages, "output_config": {"effort": CLAUDE_EFFORT}}
            if tools:
                kwargs["tools"] = tools
            response = _create(secrets, **kwargs)

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
