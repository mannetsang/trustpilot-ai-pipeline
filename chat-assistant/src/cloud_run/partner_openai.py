"""ChatGPT as a partner: the OpenAI SDK with a function-calling loop.

The key is the existing CHATGPT_API_KEY secret in Secret Manager. The model is
the "ChatGPT model" setting in the app, falling back to OPENAI_MODEL.
"""

import json
import os

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")
API_KEY_SECRET = os.environ.get("OPENAI_KEY_SECRET", "CHATGPT_API_KEY")
MAX_STEPS = 24  # research chains many lookups: search, read, query, compare

LABEL = "ChatGPT"


def available(secrets):
    if not secrets.get(API_KEY_SECRET):
        return False, f"Secret {API_KEY_SECRET} is missing or the assistant can't read it"
    return True, ""


def respond(system, history, toolset, secrets=None, model=None):
    from openai import OpenAI

    client = OpenAI(api_key=secrets.get(API_KEY_SECRET))
    tools = [{"type": "function", "function": {"name": s["name"], "description": s["description"],
                                               "parameters": s["parameters"]}} for s in toolset.specs()]
    messages = [{"role": "system", "content": system}]
    messages += [{"role": t["role"], "content": t["text"]} for t in history if t.get("text")]

    for _ in range(MAX_STEPS):
        kwargs = {"model": model or OPENAI_MODEL, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        choice = client.chat.completions.create(**kwargs).choices[0]
        message = choice.message
        if not message.tool_calls:
            return {"text": (message.content or "(no answer)").strip(), "tools": toolset.log}
        messages.append(message.model_dump(exclude_none=True))
        for call in message.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except ValueError:
                result = {"error": "arguments were not valid JSON"}
            else:
                result = toolset.call(call.function.name, args)
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(result, ensure_ascii=False, default=str)})
    return {"text": "I ran out of steps on that one. Ask me to continue.", "tools": toolset.log}


def ping(secrets=None, model=None):
    class _NoTools:
        log = []

        def specs(self):
            return []

    return respond("Reply with the single word: ready", [{"role": "user", "text": "ping"}], _NoTools(), secrets,
                   model)["text"]
