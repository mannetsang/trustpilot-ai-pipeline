"""Chat Assistant: a project board that an AI keeps current from your Google Chat.

Routes
  GET  /                 the board (sign-in page when signed out)
  GET  /login            Google sign-in; ?connect=1 also grants the assistant's Chat/Calendar access
  GET  /oauth/callback   OAuth redirect target
  POST /logout
  GET  /api/state        tasks, activity, runs, settings, connection status
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
from google_apis import ASSISTANT_SCOPES, IDENTITY_SCOPES, GoogleClient, credentials_from_json
from store import make_secrets, make_store, utcnow_iso

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

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
store = make_store()
secret_store = make_secrets()
app.secret_key = store.session_key()
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
    return render_template("index.html", owner=OWNER_EMAIL)


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
    changes = {k: bool(body[k]) for k in ("auto_act", "read_bot_posts") if k in body}
    return jsonify(settings=store.update_settings(changes))


# -- running the assistant ---------------------------------------------------------

def do_run(trigger):
    if not store.acquire_run_lock():
        return {"error": "A run is already in progress."}, 409
    started = utcnow_iso()
    try:
        google = app.config["MAKE_GOOGLE"]()
        if not google:
            raise assistant.ReconnectNeeded("Google isn't connected yet.")
        summary = assistant.run(store, google, app.config["MAKE_LLM"]())
        summary["trigger"] = trigger
        store.add_run(summary)
        store.set_status({"last_run": summary, "connection_error": ""})
        return summary, 200
    except assistant.ReconnectNeeded as exc:
        failed = {"started_at": started, "finished_at": utcnow_iso(), "trigger": trigger, "error": str(exc)[:500]}
        store.add_run(failed)
        store.set_status({"last_run": failed, "connection_error": str(exc)[:500]})
        return failed, 503
    except Exception as exc:  # noqa: BLE001 - recorded, then surfaced to the caller
        failed = {"started_at": started, "finished_at": utcnow_iso(), "trigger": trigger, "error": str(exc)[:500]}
        store.add_run(failed)
        store.set_status({"last_run": failed})
        print(f"run failed: {exc!r}")
        return failed, 500
    finally:
        store.release_run_lock()


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


@app.get("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8080")), debug=LOCAL)
