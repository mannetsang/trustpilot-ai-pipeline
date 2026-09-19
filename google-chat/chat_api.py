"""Google Chat API, authenticated as a Workspace user (not as a Chat app).

A Chat *app* only sees the spaces it has been added to. A *user* token sees
everything that user sees: every space, group chat and DM they are a member
of, with full history. This module holds the shared plumbing for the scripts
in this folder: load the user's OAuth token, keep it fresh, page through
results.

The token is the `authorized_user` JSON that `oauth_login.py` stores after a
one-time browser sign-in. It resolves like every other credential in this
repo: the GOOGLE_CHAT_USER_TOKEN environment variable first (local .env),
then Secret Manager (`google-chat-user-token`).

The scopes are read-only. Widening them means signing in again.
"""

import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.secrets import get_secret  # noqa: E402

GCP_PROJECT = "shp-ai-bot-2026"
TOKEN_SECRET_ID = "google-chat-user-token"
TOKEN_ENV_VAR = "GOOGLE_CHAT_USER_TOKEN"

SCOPES = [
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    # Needed to say *who* a DM or group chat is with: those spaces have no
    # display name, only members.
    "https://www.googleapis.com/auth/chat.memberships.readonly",
]

API_ROOT = "https://chat.googleapis.com/v1"


def load_user_credentials(token_json=None):
    """Build refreshed google-auth Credentials from the stored authorized_user JSON."""
    # Resolve the token before importing google-auth, so "no token yet" is
    # reported as such rather than as a missing package.
    raw = token_json or get_secret(TOKEN_SECRET_ID, env_var=TOKEN_ENV_VAR)

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    info = json.loads(raw)
    creds = Credentials.from_authorized_user_info(info, scopes=info.get("scopes") or SCOPES)
    if not creds.valid:
        creds.refresh(Request())
    return creds


class ChatClient:
    """Thin requests wrapper over the Chat REST API with a user token."""

    def __init__(self, credentials=None):
        self.creds = credentials or load_user_credentials()
        self.session = requests.Session()

    def _headers(self):
        from google.auth.transport.requests import Request

        if self.creds.expired or not self.creds.token:
            self.creds.refresh(Request())
        return {"Authorization": f"Bearer {self.creds.token}"}

    def get(self, path, **params):
        url = path if path.startswith("http") else f"{API_ROOT}/{path.lstrip('/')}"
        resp = self.session.get(url, headers=self._headers(), params=params, timeout=30)
        if resp.status_code >= 400:
            raise ChatApiError(resp.status_code, resp.text)
        return resp.json()

    def paginate(self, path, key, page_size=100, limit=None, **params):
        """Yield items under `key` across pages until exhausted or `limit` reached."""
        seen = 0
        token = None
        while True:
            page = self.get(path, pageSize=page_size, pageToken=token, **params)
            for item in page.get(key, []):
                yield item
                seen += 1
                if limit and seen >= limit:
                    return
            token = page.get("nextPageToken")
            if not token:
                return

    # -- convenience wrappers --------------------------------------------

    def list_spaces(self, space_type=None):
        """All spaces the user is in. Group chats and DMs appear once a message has been sent in them."""
        params = {}
        if space_type:
            params["filter"] = f'spaceType = "{space_type}"'
        return list(self.paginate("spaces", "spaces", **params))

    def list_members(self, space_name):
        return list(self.paginate(f"{space_name}/members", "memberships"))

    def list_messages(self, space_name, limit=50, newest_first=True, filter_expr=None):
        params = {"orderBy": "createTime desc" if newest_first else "createTime"}
        if filter_expr:
            params["filter"] = filter_expr
        return list(self.paginate(f"{space_name}/messages", "messages", page_size=min(limit, 1000), limit=limit, **params))


class ChatApiError(RuntimeError):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"Chat API HTTP {status}: {_error_message(body)}")


def _error_message(body):
    try:
        return json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return body[:300]


def describe_space(client, space):
    """Human-readable label: the display name, or the other members for DMs/group chats."""
    name = space.get("displayName")
    if name:
        return name
    try:
        members = client.list_members(space["name"])
    except ChatApiError:
        return "(members unavailable)"
    people = []
    for m in members:
        member = m.get("member") or {}
        if member.get("type") == "BOT":
            continue
        people.append(member.get("displayName") or member.get("name", "?"))
    return ", ".join(people) or "(no members)"
