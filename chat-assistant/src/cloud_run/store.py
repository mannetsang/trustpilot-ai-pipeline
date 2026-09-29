"""Persistence for the Chat assistant: Firestore for app data, Secret Manager for credentials.

Both have an in-memory twin with the same interface, selected with
STORE_BACKEND=memory, so the app and the hourly run can be exercised locally
and in tests without GCP.

Firestore layout (default database, collections prefixed chat_assistant_):
  chat_assistant/settings      auto_act flag and other toggles
  chat_assistant/state         per-space watermarks, run lock
  chat_assistant/config        generated session key
  chat_assistant_tasks/{id}    board cards
  chat_assistant_actions/{id}  replies sent, events created, suggestions
  chat_assistant_runs/{id}     one summary per hourly run
"""

import copy
import os
import secrets as pysecrets
import threading
import time
from datetime import datetime, timezone

GCP_PROJECT = os.environ.get("GCP_PROJECT", "shp-ai-bot-2026")

# What the assistant may do without asking, per category ("auto" or "ask").
# "Tiered" autonomy: internal work runs on its own; anything that reaches
# customers, money, staff or deletes data waits for the owner until loosened.
AUTONOMY_CATEGORIES = {
    "internal_chat": "Messages to colleagues in Google Chat",
    "calendar": "Calendar events and invites",
    "customer_messages": "Messages to customers or anyone outside the company",
    "money": "Refunds, prices, discounts, ad budgets, payments",
    "staff_hr": "Staff, schedules, hiring, HR",
    "delete_data": "Deleting or overwriting data",
}
DEFAULT_AUTONOMY = {
    "internal_chat": "auto", "calendar": "auto",
    "customer_messages": "ask", "money": "ask", "staff_hr": "ask", "delete_data": "ask",
}

DEFAULT_SETTINGS = {
    "auto_act": True,          # master switch: off = everything waits for approval
    "read_bot_posts": False,
    "autonomy": DEFAULT_AUTONOMY,
    "partners_enabled": True,  # let Claude and ChatGPT see company context
    "openai_model": "",        # empty = OPENAI_MODEL env default
}

KB_KINDS = ("projects", "facts", "questions", "systems", "spaces")


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    def __init__(self):
        self._docs = {}  # (collection, id) -> dict
        self._lock = threading.Lock()

    # -- generic primitives -------------------------------------------------
    def _get(self, coll, doc_id):
        with self._lock:
            doc = self._docs.get((coll, doc_id))
            return copy.deepcopy(doc) if doc is not None else None

    def _set(self, coll, doc_id, data, merge=False):
        with self._lock:
            if merge and (coll, doc_id) in self._docs:
                self._docs[(coll, doc_id)].update(copy.deepcopy(data))
            else:
                self._docs[(coll, doc_id)] = copy.deepcopy(data)

    def _delete(self, coll, doc_id):
        with self._lock:
            self._docs.pop((coll, doc_id), None)

    def _list(self, coll):
        with self._lock:
            return [dict(copy.deepcopy(v), id=k[1]) for k, v in self._docs.items() if k[0] == coll]

    def _try_lock(self, seconds):
        with self._lock:
            state = self._docs.setdefault(("chat_assistant", "state"), {})
            if state.get("lock_until", 0) > time.time():
                return False
            state["lock_until"] = time.time() + seconds
            return True

    # -- shared API (implemented once, on top of the primitives) -----------
    def get_settings(self):
        stored = self._get("chat_assistant", "settings") or {}
        merged = {**DEFAULT_SETTINGS, **stored}
        merged["autonomy"] = {**DEFAULT_AUTONOMY, **(stored.get("autonomy") or {})}
        return merged

    # -- knowledge base: projects, facts, questions, systems, spaces ----------
    def list_items(self, kind):
        assert kind in KB_KINDS, kind
        return sorted(self._list(f"chat_assistant_{kind}"), key=lambda d: d.get("updated_at") or d.get("created_at", ""),
                      reverse=True)

    def get_item(self, kind, item_id):
        assert kind in KB_KINDS, kind
        doc = self._get(f"chat_assistant_{kind}", item_id)
        return dict(doc, id=item_id) if doc else None

    def save_item(self, kind, item_id, data):
        assert kind in KB_KINDS, kind
        self._set(f"chat_assistant_{kind}", item_id, {k: v for k, v in data.items() if k != "id"}, merge=True)
        return self.get_item(kind, item_id)

    def delete_item(self, kind, item_id):
        assert kind in KB_KINDS, kind
        self._delete(f"chat_assistant_{kind}", item_id)

    # -- conversations with the assistant and its partners ---------------------
    def get_talk(self, partner):
        return (self._get("chat_assistant_talk", partner) or {}).get("turns", [])

    def append_talk(self, partner, turns, keep=60):
        history = (self.get_talk(partner) + list(turns))[-keep:]
        self._set("chat_assistant_talk", partner, {"turns": history, "updated_at": utcnow_iso()})
        return history

    def clear_talk(self, partner):
        self._delete("chat_assistant_talk", partner)

    def get_flag(self, name):
        return (self._get("chat_assistant", "flags") or {}).get(name)

    def set_flag(self, name, value):
        self._set("chat_assistant", "flags", {name: value}, merge=True)

    def update_settings(self, changes):
        self._set("chat_assistant", "settings", changes, merge=True)
        return self.get_settings()

    def get_watermarks(self):
        return (self._get("chat_assistant", "state") or {}).get("watermarks", {})

    def set_watermarks(self, watermarks):
        current = self.get_watermarks()
        current.update(watermarks)
        self._set("chat_assistant", "state", {"watermarks": current}, merge=True)

    def acquire_run_lock(self, seconds=3300):
        return self._try_lock(seconds)

    def release_run_lock(self):
        self._set("chat_assistant", "state", {"lock_until": 0}, merge=True)

    def get_owner(self):
        """{"user": "users/<id>", "email": ...} of the person who connected Google, or None."""
        return self._get("chat_assistant", "owner")

    def set_owner(self, owner):
        self._set("chat_assistant", "owner", owner)

    def set_status(self, changes):
        """Connection and last-run status shown in the UI header."""
        self._set("chat_assistant", "status", changes, merge=True)

    def get_status(self):
        return self._get("chat_assistant", "status") or {}

    def set_progress(self, state):
        """The running (or last) pass's live progress; replaced wholesale on every update."""
        self._set("chat_assistant", "progress", state)

    def get_progress(self):
        return self._get("chat_assistant", "progress") or {}

    def session_key(self):
        config = self._get("chat_assistant", "config") or {}
        if not config.get("session_key"):
            config["session_key"] = pysecrets.token_hex(32)
            self._set("chat_assistant", "config", config, merge=True)
        return config["session_key"]

    def list_tasks(self):
        return sorted(self._list("chat_assistant_tasks"), key=lambda t: t.get("created_at", ""), reverse=True)

    def get_task(self, task_id):
        doc = self._get("chat_assistant_tasks", task_id)
        return dict(doc, id=task_id) if doc else None

    def save_task(self, task_id, data):
        data = {k: v for k, v in data.items() if k != "id"}
        self._set("chat_assistant_tasks", task_id, data, merge=True)
        return self.get_task(task_id)

    def delete_task(self, task_id):
        self._delete("chat_assistant_tasks", task_id)

    def list_actions(self, limit=200):
        items = sorted(self._list("chat_assistant_actions"), key=lambda a: a.get("created_at", ""), reverse=True)
        return items[:limit]

    def get_action(self, action_id):
        doc = self._get("chat_assistant_actions", action_id)
        return dict(doc, id=action_id) if doc else None

    def save_action(self, action_id, data):
        data = {k: v for k, v in data.items() if k != "id"}
        self._set("chat_assistant_actions", action_id, data, merge=True)
        return self.get_action(action_id)

    def add_run(self, summary):
        run_id = summary.get("started_at", utcnow_iso()).replace(":", "").replace(".", "")
        self._set("chat_assistant_runs", run_id, summary)
        return run_id

    def list_runs(self, limit=20):
        return sorted(self._list("chat_assistant_runs"), key=lambda r: r.get("started_at", ""), reverse=True)[:limit]


class FirestoreStore(MemoryStore):
    """Same API as MemoryStore; the primitives go to Firestore instead of a dict."""

    def __init__(self):
        from google.cloud import firestore

        self._fs = firestore
        self._db = firestore.Client(project=GCP_PROJECT)

    def _get(self, coll, doc_id):
        snap = self._db.collection(coll).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def _set(self, coll, doc_id, data, merge=False):
        self._db.collection(coll).document(doc_id).set(data, merge=merge)

    def _delete(self, coll, doc_id):
        self._db.collection(coll).document(doc_id).delete()

    def _list(self, coll):
        return [dict(s.to_dict(), id=s.id) for s in self._db.collection(coll).stream()]

    def list_actions(self, limit=200):
        q = (self._db.collection("chat_assistant_actions")
             .order_by("created_at", direction=self._fs.Query.DESCENDING).limit(limit))
        return [dict(s.to_dict(), id=s.id) for s in q.stream()]

    def list_runs(self, limit=20):
        q = (self._db.collection("chat_assistant_runs")
             .order_by("started_at", direction=self._fs.Query.DESCENDING).limit(limit))
        return [dict(s.to_dict(), id=s.id) for s in q.stream()]

    def session_key(self):
        # create() fails if the doc exists, so instances starting together agree on one key.
        from google.api_core import exceptions

        ref = self._db.collection("chat_assistant").document("config")
        try:
            ref.create({"session_key": pysecrets.token_hex(32)})
        except exceptions.AlreadyExists:
            pass
        return ref.get().to_dict()["session_key"]

    def set_watermarks(self, watermarks):
        # Merge so concurrent space results don't clobber each other.
        ref = self._db.collection("chat_assistant").document("state")
        ref.set({"watermarks": watermarks}, merge=True)

    def _try_lock(self, seconds):
        ref = self._db.collection("chat_assistant").document("state")
        transaction = self._db.transaction()

        @self._fs.transactional
        def take(tx):
            snap = ref.get(transaction=tx)
            state = snap.to_dict() if snap.exists else {}
            if state.get("lock_until", 0) > time.time():
                return False
            tx.set(ref, {"lock_until": time.time() + seconds}, merge=True)
            return True

        return take(transaction)


# -- credentials ----------------------------------------------------------------

class MemorySecrets:
    def __init__(self, initial=None):
        self._values = dict(initial or {})

    def get(self, secret_id):
        return self._values.get(secret_id)

    def put(self, secret_id, payload):
        self._values[secret_id] = payload

    def invalidate(self, secret_id=None):
        pass


class SecretManagerSecrets:
    """Reads (cached) and writes secrets; a write disables the previous versions."""

    CACHE_SECONDS = 300

    def __init__(self):
        from google.cloud import secretmanager

        self._client = secretmanager.SecretManagerServiceClient()
        self._cache = {}

    def _path(self, secret_id):
        return f"projects/{GCP_PROJECT}/secrets/{secret_id}"

    def get(self, secret_id):
        hit = self._cache.get(secret_id)
        if hit and hit[1] > time.time():
            return hit[0]
        from google.api_core import exceptions

        try:
            resp = self._client.access_secret_version(request={"name": f"{self._path(secret_id)}/versions/latest"})
            value = resp.payload.data.decode("utf-8").rstrip("\n")
        except (exceptions.NotFound, exceptions.FailedPrecondition):
            value = None  # secret missing, or no enabled version yet
        self._cache[secret_id] = (value, time.time() + self.CACHE_SECONDS)
        return value

    def put(self, secret_id, payload):
        parent = self._path(secret_id)
        new = self._client.add_secret_version(request={"parent": parent, "payload": {"data": payload.encode("utf-8")}})
        # Old tokens shouldn't linger: disable every other enabled version.
        for version in self._client.list_secret_versions(request={"parent": parent, "filter": "state:ENABLED"}):
            if version.name != new.name:
                self._client.disable_secret_version(request={"name": version.name})
        self._cache[secret_id] = (payload, time.time() + self.CACHE_SECONDS)

    def invalidate(self, secret_id=None):
        if secret_id:
            self._cache.pop(secret_id, None)
        else:
            self._cache.clear()


def make_store():
    return MemoryStore() if os.environ.get("STORE_BACKEND") == "memory" else FirestoreStore()


def make_secrets():
    return MemorySecrets() if os.environ.get("STORE_BACKEND") == "memory" else SecretManagerSecrets()
