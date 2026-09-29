"""Chat Assistant: a project board that an AI keeps current from your Google Chat.

Routes
  GET  /                 the board (sign-in page when signed out)
  GET  /login            Google sign-in; ?connect=1 also grants the assistant's Chat/Calendar access
  GET  /oauth/callback   OAuth redirect target
  POST /logout
  GET  /api/state        tasks, activity, runs, settings, connection status
  GET  /api/progress     live progress of the running (or last) pass
  POST/PATCH/DELETE /api/tasks[/<id>]
  POST /api/actions/<id>/send | /dismiss
  PATCH /api/settings
  POST /api/run          run now (from the UI)
  POST /run              hourly run (Cloud Scheduler, X-Run-Token header)

Only OWNER_EMAIL can sign in. The owner's Google token lives in Secret Manager
(chat-assistant-user-token), readable only by this service's own service account.
"""

import hmac
import json
import os
import secrets as pysecrets
from datetime import timedelta

from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

import assistant
import knowledge
from google_apis import ASSISTANT_SCOPES, IDENTITY_SCOPES, GoogleClient, credentials_from_json
from store import AUTONOMY_CATEGORIES, make_secrets, make_store, utcnow_iso
from talk import PARTNERS, Talk

# Google may grant scopes in a different spelling/order than requested.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "manne@superhairpieces.com").lower()
OWNER_DOMAIN = OWNER_EMAIL.split("@")[-1]
OAUTH_CLIENT_SECRET_ID = os.environ.get("OAUTH_CLIENT_SECRET_ID", "chat-assistant-oauth-client")
USER_TOKEN_SECRET_ID = os.environ.get("USER_TOKEN_SECRET_ID", "chat-assistant-user-token")
RUN_TOKEN_SECRET_ID = os.environ.get("RUN_TOKEN_SECRET_ID", "chat-assistant-run-token")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
CSRF_HEADER = "X-Chat-Assistant"
LOCAL = os.environ.get("STORE_BACKEND") == "memory"
# Cloud Run names every deploy (K_REVISION); open pages reload themselves when it changes.
APP_VERSION = os.environ.get("K_REVISION") or "local"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
store = make_store()
secret_store = make_secrets()
app.secret_key = store.session_key()
knowledge.ensure_seeded(store)
app.config.update(
    SESSION_COOKIE_SECURE=not LOCAL,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)


def make_google():
    """The owner's Google client, or None before they've connected."""
    token = secret_store.get(USER_TOKEN_SECRET_ID)
    return GoogleClient(credentials_from_json(token)) if token else None


def make_llm():
    from llm import Gemini

    return Gemini()


# Tests swap these for fakes.
app.config["MAKE_GOOGLE"] = make_google
app.config["MAKE_LLM"] = make_llm


# -- access control ----------------------------------------------------------------

def signed_in():
    return session.get("email") == OWNER_EMAIL


@app.before_request
def guard():
    if request.path.startswith("/api/"):
        if not signed_in():
            return jsonify(error="Sign in first."), 401
        # CSRF: browsers can't add a custom header cross-site without a CORS preflight we never allow.
        if request.method != "GET" and request.headers.get(CSRF_HEADER) != "1":
            return jsonify(error="Missing request header."), 403


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


# -- sign-in -----------------------------------------------------------------------

def oauth_client_config():
    raw = secret_store.get(OAUTH_CLIENT_SECRET_ID)
    return json.loads(raw) if raw else None


def redirect_uri():
    return f"{PUBLIC_URL or request.host_url.rstrip('/')}/oauth/callback"


def make_flow(scopes, state=None):
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_config(oauth_client_config(), scopes=scopes, redirect_uri=redirect_uri(),
                                   state=state, autogenerate_code_verifier=True)


def login_page(error="", status=200):
    return render_template("login.html", error=error, owner=OWNER_EMAIL,
                           configured=bool(oauth_client_config()), redirect_uri=redirect_uri()), status


@app.get("/")
def index():
    if not signed_in():
        return login_page()
    return render_template("index.html", owner=OWNER_EMAIL, version=APP_VERSION)


@app.get("/login")
def login():
    if not oauth_client_config():
        return login_page()
    connect = request.args.get("connect") == "1" or not secret_store.get(USER_TOKEN_SECRET_ID)
    flow = make_flow(ASSISTANT_SCOPES if connect else IDENTITY_SCOPES)
    extra = {"prompt": "consent", "access_type": "offline"} if connect else {"prompt": "select_account"}
    url, state = flow.authorization_url(login_hint=OWNER_EMAIL, hd=OWNER_DOMAIN, **extra)
    session["oauth"] = {"state": state, "verifier": flow.code_verifier, "connect": connect}
    return redirect(url)


@app.get("/oauth/callback")
def oauth_callback():
    pending = session.pop("oauth", None)
    if request.args.get("error"):
        return login_page(f"Google sign-in was cancelled ({request.args['error']}).", 400)
    if not pending or not hmac.compare_digest(request.args.get("state", ""), pending["state"]):
        return login_page("That sign-in link expired. Please try again.", 400)

    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    flow = make_flow(ASSISTANT_SCOPES if pending["connect"] else IDENTITY_SCOPES, state=pending["state"])
    flow.code_verifier = pending["verifier"]
    try:
        flow.fetch_token(code=request.args.get("code", ""))
        creds = flow.credentials
        claims = id_token.verify_oauth2_token(creds.id_token, Request(), flow.client_config["client_id"])
    except Exception as exc:  # noqa: BLE001 - shown to the user, who can retry
        return login_page(f"Sign-in failed: {exc}", 400)

    email = (claims.get("email") or "").lower()
    if email != OWNER_EMAIL or not claims.get("email_verified"):
        return login_page(f"{email or 'That account'} can't use this assistant. Sign in as {OWNER_EMAIL}.", 403)

    session.clear()
    session.permanent = True
    session["email"] = email

    if pending["connect"]:
        if not creds.refresh_token:
            return login_page("Google didn't return long-term access. Remove the app at "
                              "myaccount.google.com/permissions and connect again.", 400)
        granted = flow.oauth2session.token.get("scope") or []
        granted = set(granted.split() if isinstance(granted, str) else granted)
        missing = sorted(s.rsplit("/", 1)[-1] for s in ASSISTANT_SCOPES if s not in granted and s != "openid")
        secret_store.put(USER_TOKEN_SECRET_ID, creds.to_json())
        store.set_owner({"user": f"users/{claims['sub']}", "email": email})
        store.set_status({"connected_at": utcnow_iso(), "connection_error": "", "missing_scopes": missing})
    return redirect("/")


@app.post("/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


# -- board API ---------------------------------------------------------------------

TASK_FIELDS = {"title", "detail", "owner", "owner_is_me", "due", "priority", "status"}


def _clean_task(body):
    fields = {k: body[k] for k in TASK_FIELDS if k in body}
    if "title" in fields:
        fields["title"] = str(fields["title"]).strip()[:200]
    if fields.get("status") not in (None, "todo", "in_progress", "done"):
        fields.pop("status")
    if fields.get("priority") not in (None, "high", "medium", "low"):
        fields.pop("priority")
    for k in ("detail", "owner", "due"):
        if k in fields:
            fields[k] = str(fields[k] or "")[:2000 if k == "detail" else 100]
    if "owner_is_me" in fields:
        fields["owner_is_me"] = bool(fields["owner_is_me"])
    return fields


@app.get("/api/state")
def api_state():
    status = store.get_status()
    return jsonify(
        version=APP_VERSION,
        owner=OWNER_EMAIL,
        connected=bool(secret_store.get(USER_TOKEN_SECRET_ID)),
        status=status,
        settings=store.get_settings(),
        limits={"act_confidence": assistant.ACT_CONFIDENCE, "max_actions": assistant.MAX_ACTIONS_PER_RUN,
                "act_max_age_hours": assistant.ACT_MAX_AGE_HOURS},
        tasks=store.list_tasks(),
        actions=store.list_actions(150),
        runs=store.list_runs(12),
    )


@app.post("/api/tasks")
def api_create_task():
    fields = _clean_task(request.get_json(force=True) or {})
    if not fields.get("title"):
        return jsonify(error="A task needs a title."), 400
    now = utcnow_iso()
    task = store.save_task(pysecrets.token_hex(10), {
        "detail": "", "owner": "", "owner_is_me": True, "due": "", "priority": "medium", "status": "todo",
        **fields, "origin": "manual", "user_edited": True, "created_at": now, "updated_at": now,
    })
    return jsonify(task=task)


@app.patch("/api/tasks/<task_id>")
def api_update_task(task_id):
    if not store.get_task(task_id):
        return jsonify(error="No such task."), 404
    fields = _clean_task(request.get_json(force=True) or {})
    if "title" in fields and not fields["title"]:
        return jsonify(error="A task needs a title."), 400
    task = store.save_task(task_id, {**fields, "user_edited": True, "updated_at": utcnow_iso()})
    return jsonify(task=task)


@app.delete("/api/tasks/<task_id>")
def api_delete_task(task_id):
    store.delete_task(task_id)
    return jsonify(ok=True)


@app.post("/api/actions/<action_id>/send")
def api_send_action(action_id):
    action = store.get_action(action_id)
    if not action or action.get("status") not in ("suggested", "failed"):
        return jsonify(error="That suggestion is no longer waiting."), 409
    body = request.get_json(silent=True) or {}
    if action["type"] == "chat_reply" and body.get("reply_text"):
        action["reply_text"] = str(body["reply_text"]).strip()[:4000]
    google = app.config["MAKE_GOOGLE"]()
    if not google:
        return jsonify(error="Connect Google first."), 409
    outcome = assistant.execute_action(google, {k: v for k, v in action.items() if k != "id"})
    outcome["sent_by"] = "you"
    return jsonify(action=store.save_action(action_id, outcome))


@app.post("/api/actions/<action_id>/dismiss")
def api_dismiss_action(action_id):
    if not store.get_action(action_id):
        return jsonify(error="No such suggestion."), 404
    return jsonify(action=store.save_action(action_id, {"status": "dismissed", "executed_at": utcnow_iso()}))


@app.patch("/api/settings")
def api_settings():
    body = request.get_json(force=True) or {}
    changes = {k: bool(body[k]) for k in ("auto_act", "read_bot_posts", "partners_enabled") if k in body}
    if isinstance(body.get("autonomy"), dict):
        current = store.get_settings()["autonomy"]
        changes["autonomy"] = {**current, **{k: v for k, v in body["autonomy"].items()
                                             if k in AUTONOMY_CATEGORIES and v in ("auto", "ask")}}
    if "openai_model" in body:
        changes["openai_model"] = str(body["openai_model"] or "").strip()[:60]
    return jsonify(settings=store.update_settings(changes))


# -- knowledge base, interview, access ------------------------------------------------

def talk_service():
    return Talk(store, secret_store, app.config["MAKE_GOOGLE"], OWNER_EMAIL)


@app.get("/api/knowledge")
def api_knowledge():
    service = talk_service()
    return jsonify(
        projects=store.list_items("projects"),
        questions=store.list_items("questions"),
        systems=store.list_items("systems"),
        facts=store.list_items("facts")[:200],
        autonomy_categories=[[k, v] for k, v in AUTONOMY_CATEGORIES.items()],  # lists keep their order in JSON
        partners=[{"name": name, "label": module.LABEL, "available": ok, "detail": why}
                  for name, module in PARTNERS.items() for ok, why in [service.partner_status(name)]],
    )


@app.post("/api/questions/<question_id>/answer")
def api_answer_question(question_id):
    q = store.get_item("questions", question_id)
    if not q:
        return jsonify(error="No such question."), 404
    answer = str((request.get_json(force=True) or {}).get("answer", "")).strip()
    if not answer:
        return jsonify(error="Type an answer first."), 400
    now = utcnow_iso()
    store.save_item("questions", question_id, {"status": "answered", "answer": answer[:8000], "answered_at": now,
                                               "updated_at": now})
    knowledge.add_fact(store, f"Q: {q['question']} A: {answer}", "general", q.get("project_id", ""), "owner's answer")
    try:
        digest = talk_service().digest_answer(q, answer)
        note = digest["text"]
        tools = digest.get("tools", [])
    except Exception as exc:  # noqa: BLE001 - the answer is saved either way
        note, tools = f"Saved your answer, but couldn't process it further: {exc}", []
    store.save_item("questions", question_id, {"digest": note[:1000]})
    return jsonify(question=store.get_item("questions", question_id), note=note, tools=tools)


@app.post("/api/questions/<question_id>/dismiss")
def api_dismiss_question(question_id):
    if not store.get_item("questions", question_id):
        return jsonify(error="No such question."), 404
    return jsonify(question=store.save_item("questions", question_id, {"status": "dismissed",
                                                                       "updated_at": utcnow_iso()}))


@app.post("/api/questions")
def api_add_question():
    body = request.get_json(force=True) or {}
    q = knowledge.add_question(store, body.get("question", ""), body.get("why", ""), body.get("project_id", ""),
                               body.get("priority", 2), source="you")
    return (jsonify(question=q), 200) if q else (jsonify(error="Type a question."), 400)


PROJECT_FIELDS = ("name", "company", "goal", "owner", "status", "deadline", "summary", "next_steps")


@app.post("/api/projects")
def api_create_project():
    body = request.get_json(force=True) or {}
    project = knowledge.save_project(store, {k: body.get(k) for k in PROJECT_FIELDS}, source="you")
    return (jsonify(project=project), 200) if project else (jsonify(error="A project needs a name."), 400)


@app.patch("/api/projects/<project_id>")
def api_update_project(project_id):
    if not store.get_item("projects", project_id):
        return jsonify(error="No such project."), 404
    body = request.get_json(force=True) or {}
    changes = {k: str(body[k])[:2000] for k in PROJECT_FIELDS if k in body}
    return jsonify(project=store.save_item("projects", project_id, {**changes, "updated_at": utcnow_iso()}))


@app.delete("/api/projects/<project_id>")
def api_delete_project(project_id):
    store.delete_item("projects", project_id)
    return jsonify(ok=True)


@app.patch("/api/systems/<system_id>")
def api_update_system(system_id):
    if not store.get_item("systems", system_id):
        return jsonify(error="No such system."), 404
    body = request.get_json(force=True) or {}
    changes = {}
    if body.get("status") in ("connected", "available", "needed", "requested", "not_used"):
        changes["status"] = body["status"]
    if "notes" in body:
        changes["notes"] = str(body["notes"])[:1000]
    return jsonify(system=store.save_item("systems", system_id, {**changes, "updated_at": utcnow_iso()}))


# -- talking to the assistant and its partners -----------------------------------------

@app.get("/api/talk/<partner>")
def api_talk_history(partner):
    if partner not in PARTNERS:
        return jsonify(error="No such partner."), 404
    return jsonify(turns=store.get_talk(partner))


@app.post("/api/talk/<partner>")
def api_talk(partner):
    if partner not in PARTNERS:
        return jsonify(error="No such partner."), 404
    message = str((request.get_json(force=True) or {}).get("message", "")).strip()
    if not message:
        return jsonify(error="Say something first."), 400
    try:
        reply = talk_service().ask(partner, message)
    except Exception as exc:  # noqa: BLE001 - shown in the conversation
        import traceback

        print(f"talk[{partner}] failed: {exc!r}\n{traceback.format_exc()}")
        return jsonify(error=str(exc)[:500]), 502
    return jsonify(reply=reply)


@app.delete("/api/talk/<partner>")
def api_talk_clear(partner):
    if partner not in PARTNERS:
        return jsonify(error="No such partner."), 404
    store.clear_talk(partner)
    return jsonify(ok=True)


@app.post("/api/partners/test")
def api_test_partners():
    return jsonify(results=talk_service().test_partners())


# -- live voice ----------------------------------------------------------------------

def _voice_origin_ok():
    """Only the app's own page may open the voice socket (blocks cross-site WebSocket use of the cookie).

    Compares host names: behind Cloud Run's proxy a WebSocket upgrade can arrive without the
    forwarded https scheme, which made the full-URL comparison fail for the app's own page.
    """
    from urllib.parse import urlsplit

    origin_host = urlsplit(request.headers.get("Origin", "")).netloc.lower()
    expected = urlsplit(PUBLIC_URL).netloc.lower() if PUBLIC_URL else request.host.lower()
    return bool(origin_host) and origin_host == expected


def voice_ws(ws):
    import json as _json

    import voice
    from talk import system_prompt

    if not signed_in():
        ws.send(_json.dumps({"type": "error", "message": "Sign in to the app first."}))
        return
    if not _voice_origin_ok():
        print(f"voice: origin {request.headers.get('Origin')!r} != {PUBLIC_URL or request.host_url!r}")
        ws.send(_json.dumps({"type": "error", "message": "Open the app from its own address to use voice."}))
        return
    partner = request.args.get("partner", "assistant")
    if partner not in PARTNERS:
        ws.send(_json.dumps({"type": "error", "message": "No such partner."}))
        return
    try:
        label = PARTNERS[partner].LABEL if partner != "assistant" else "your assistant"
        ws.send(_json.dumps({"type": "status", "text": f"Preparing {label}…"}))
        service = talk_service()
        if partner == "assistant":
            toolset = service.toolset("assistant", voice=True)
            recent = [t for t in store.get_talk("assistant") if t.get("text")][-8:]
            history = "\n".join(f"{'Manne' if t['role'] == 'user' else 'You'}: {t['text'][:500]}" for t in recent)
            instruction = system_prompt(store, "assistant", voice=True, owner_email=OWNER_EMAIL)
            if history:
                instruction += "\n\nRECENT CONVERSATION (continue from here)\n" + history
            save_as = "assistant"
        else:
            # Gemini Live listens and speaks; the partner's own model answers (see voice.RelayTools).
            ok, why = service.partner_status(partner)
            if not ok:
                ws.send(_json.dumps({"type": "error", "message": f"{label} isn't available: {why}"}))
                return
            toolset = voice.RelayTools(partner, label, lambda m: service.ask(partner, m, voice=True)["text"])
            instruction = voice.relay_instruction(label, toolset.name, knowledge.vocabulary(store))
            save_as = None
        bridge = voice.VoiceBridge(ws, store, toolset, instruction, connect=app.config.get("VOICE_CONNECT"),
                                   save_as=save_as,
                                   end_silence_ms=voice.RELAY_END_SILENCE_MS if partner != "assistant" else None)
    except Exception as exc:  # noqa: BLE001 - without this the socket would stay open and silent
        import traceback

        print(f"voice setup failed: {exc!r}\n{traceback.format_exc()}")
        ws.send(_json.dumps({"type": "error", "message": f"Voice couldn't start: {str(exc)[:300]}"}))
        return
    bridge.run()


try:
    from flask_sock import Sock

    Sock(app).route("/ws/voice")(voice_ws)
except ImportError:  # the voice route needs flask-sock; everything else works without it
    pass


# -- running the assistant ---------------------------------------------------------

def do_run(trigger):
    if not store.acquire_run_lock():
        return {"error": "A run is already in progress."}, 409
    started = utcnow_iso()
    progress = assistant.Progress(store, trigger)
    try:
        google = app.config["MAKE_GOOGLE"]()
        if not google:
            raise assistant.ReconnectNeeded("Google isn't connected yet.")
        summary = assistant.run(store, google, app.config["MAKE_LLM"](), progress=progress)
        summary["trigger"] = trigger
        store.add_run(summary)
        store.set_status({"last_run": summary, "connection_error": ""})
        progress.finish()
        return summary, 200
    except assistant.ReconnectNeeded as exc:
        failed = {"started_at": started, "finished_at": utcnow_iso(), "trigger": trigger, "error": str(exc)[:500]}
        store.add_run(failed)
        store.set_status({"last_run": failed, "connection_error": str(exc)[:500]})
        progress.finish(str(exc))
        return failed, 503
    except Exception as exc:  # noqa: BLE001 - recorded, then surfaced to the caller
        import traceback

        trace = traceback.format_exc()
        print(f"run failed: {exc!r}\n{trace}")
        failed = {"started_at": started, "finished_at": utcnow_iso(), "trigger": trigger,
                  "error": assistant.error_text(exc, 500),  # says where, even in the header's one line
                  "trace": trace[-2000:]}  # shown under Activity -> Runs, so a failure explains itself
        store.add_run(failed)
        store.set_status({"last_run": failed})
        progress.finish(failed["error"])
        return failed, 500
    finally:
        store.release_run_lock()


@app.get("/api/progress")
def api_progress():
    """Small and cheap, so the page can poll it every couple of seconds during a run."""
    return jsonify(store.get_progress())


@app.post("/api/run")
def api_run():
    body, status = do_run("manual")
    return jsonify(body), status


@app.post("/run")
def scheduled_run():
    expected = secret_store.get(RUN_TOKEN_SECRET_ID) or ""
    given = request.headers.get("X-Run-Token", "")
    if not expected or not hmac.compare_digest(given, expected):
        return jsonify(error="forbidden"), 403
    body, status = do_run("schedule")
    return jsonify(body), status


# Not /healthz: Cloud Run reserves paths ending in "z" and never forwards them.
@app.get("/health")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8080")), debug=LOCAL)
