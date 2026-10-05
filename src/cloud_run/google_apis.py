"""Google Chat, People (directory) and Calendar calls, made as the signed-in owner.

Everything here runs with the owner's own OAuth token, so it can only see and
do what the owner could in the Chat and Calendar UIs.

With user auth the Chat API returns people as bare ids (`users/123`), never
names. Chat user ids are People API person ids, so `Directory` loads the
Workspace directory once per run and resolves ids to names and emails.
"""

import json

import requests

CHAT = "https://chat.googleapis.com/v1"
PEOPLE = "https://people.googleapis.com/v1"
CALENDAR = "https://www.googleapis.com/calendar/v3"

IDENTITY_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
ASSISTANT_SCOPES = IDENTITY_SCOPES + [
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.messages.create",
    "https://www.googleapis.com/auth/chat.spaces.create",    # start a direct message with a colleague (after Manne's OK)
    "https://www.googleapis.com/auth/chat.memberships.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/directory.readonly",
]
# Google tools the partners use through integrations.py (Access tab: Gmail, Drive/Sheets, Analytics, Search Console).
GOOGLE_TOOL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",        # drafts, sent only after Manne's OK
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/content",               # Merchant Center (changes need Manne's OK)
]
ASSISTANT_SCOPES = ASSISTANT_SCOPES + GOOGLE_TOOL_SCOPES


class GoogleApiError(RuntimeError):
    def __init__(self, status, body, url=""):
        self.status = status
        self.body = body
        try:
            message = json.loads(body)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = (body or "")[:300]
        super().__init__(f"HTTP {status} from {url.split('?')[0]}: {message}")


def credentials_from_json(token_json):
    from google.oauth2.credentials import Credentials

    info = json.loads(token_json)
    if info.get("expiry"):
        # Sign-in (google-auth-oauthlib) gives a time-zone-aware expiry, which to_json() writes as "...+00:00Z";
        # google-auth reads only the naive "...Z" form and compares it with a naive clock.
        info["expiry"] = info["expiry"].replace("+00:00", "")
    return Credentials.from_authorized_user_info(info, scopes=info.get("scopes"))


def naive_utc_expiry(creds):
    """google-auth compares expiry with a naive UTC clock; sign-in sets an aware one ("can't compare
    offset-naive and offset-aware datetimes"). Call before using or saving fresh sign-in credentials."""
    from datetime import timezone

    expiry = getattr(creds, "expiry", None)
    if expiry is not None and expiry.tzinfo is not None:
        creds.expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
    return creds


class GoogleClient:
    """requests wrapper that keeps the owner's access token fresh."""

    def __init__(self, credentials):
        import threading

        self.creds = credentials
        self._refresh_lock = threading.Lock()  # the hourly run calls from several threads

    def _auth(self):
        from google.auth.transport.requests import Request

        with self._refresh_lock:
            if not self.creds.valid:
                self.creds.refresh(Request())
            return {"Authorization": f"Bearer {self.creds.token}"}

    def auth_header(self):
        """For integrations.py: Google APIs called with Manne's own sign-in."""
        return self._auth()

    def request(self, method, url, params=None, body=None):
        resp = requests.request(method, url, headers=self._auth(), params=params, json=body, timeout=60)
        if resp.status_code >= 400:
            raise GoogleApiError(resp.status_code, resp.text, url)
        return resp.json() if resp.content else {}

    def paginate(self, url, key, params=None, limit=None):
        params = dict(params or {})
        seen = 0
        while True:
            page = self.request("GET", url, params=params)
            for item in page.get(key, []):
                yield item
                seen += 1
                if limit and seen >= limit:
                    return
            token = page.get("nextPageToken")
            if not token:
                return
            params["pageToken"] = token

    # -- Chat -----------------------------------------------------------------
    def list_spaces(self):
        return list(self.paginate(f"{CHAT}/spaces", "spaces", {"pageSize": 1000}))

    def list_members(self, space_name):
        return list(self.paginate(f"{CHAT}/{space_name}/members", "memberships", {"pageSize": 1000}))

    def chat_user_id(self, space_name, email):
        """The Chat id (users/123) of a member, looked up by email alias in a space they're in."""
        membership = self.request("GET", f"{CHAT}/{space_name}/members/users/{email}")
        return (membership.get("member") or {}).get("name", "")

    def list_messages_since(self, space_name, since_iso, limit=300):
        """Messages created after since_iso, oldest first (the API default order)."""
        params = {"pageSize": 1000, "filter": f'createTime > "{since_iso}"'}
        return list(self.paginate(f"{CHAT}/{space_name}/messages", "messages", params, limit=limit))

    def list_messages_before(self, space_name, start_iso, end_iso, limit):
        """The `limit` most recent messages in (start, end), returned oldest first."""
        params = {"pageSize": limit, "orderBy": "createTime desc",
                  "filter": f'createTime > "{start_iso}" AND createTime < "{end_iso}"'}
        page = self.request("GET", f"{CHAT}/{space_name}/messages", params=params)
        return list(reversed(page.get("messages", [])))

    def find_direct_message(self, user):
        """The direct message with a person (users/<email or id>), or None if there isn't one yet."""
        try:
            return self.request("GET", f"{CHAT}/spaces:findDirectMessage", params={"name": user})
        except GoogleApiError as exc:
            if exc.status == 404:
                return None
            raise

    def start_direct_message(self, user):
        """Opens a direct message with a person (needs chat.spaces.create). Returns the new space."""
        return self.request("POST", f"{CHAT}/spaces:setup", body={
            "space": {"spaceType": "DIRECT_MESSAGE"},
            "memberships": [{"member": {"name": user, "type": "HUMAN"}}]})

    def reply_in_thread(self, space_name, thread_name, text):
        body = {"text": text}
        params = {}
        if thread_name:
            body["thread"] = {"name": thread_name}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        return self.request("POST", f"{CHAT}/{space_name}/messages", params=params, body=body)

    # -- Calendar -------------------------------------------------------------
    def create_event(self, title, start_iso, end_iso, time_zone, attendee_emails, description=""):
        body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start_iso, "timeZone": time_zone},
            "end": {"dateTime": end_iso, "timeZone": time_zone},
            "attendees": [{"email": e} for e in attendee_emails],
        }
        return self.request("POST", f"{CALENDAR}/calendars/primary/events",
                            params={"sendUpdates": "all"}, body=body)

    # -- Directory ------------------------------------------------------------
    def load_directory(self):
        people = self.paginate(f"{PEOPLE}/people:listDirectoryPeople", "people", {
            "readMask": "names,emailAddresses",
            "sources": "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
            "pageSize": 1000,
        })
        return Directory.from_people(people)


class Directory:
    """Chat user id -> {name, email}, for everyone in the Workspace directory."""

    def __init__(self, entries=None):
        self.entries = entries or {}  # "users/123" -> {"name":..., "email":...}

    @classmethod
    def from_people(cls, people):
        entries = {}
        for p in people:
            pid = p.get("resourceName", "").split("/")[-1]
            if not pid:
                continue
            names = p.get("names") or [{}]
            emails = p.get("emailAddresses") or [{}]
            entries[f"users/{pid}"] = {
                "name": names[0].get("displayName") or emails[0].get("value") or pid,
                "email": (emails[0].get("value") or "").lower(),
            }
        return cls(entries)

    def name(self, user_name):
        entry = self.entries.get(user_name)
        return entry["name"] if entry else f"{user_name} (outside the directory)"

    def email(self, user_name):
        entry = self.entries.get(user_name)
        return entry["email"] if entry else ""

    def find(self, query):
        """People matching a name or email, best tier first: exact email, exact name, first name (or the part
        before @), then any part of the name. Each: {"user", "name", "email"}."""
        q = " ".join((query or "").lower().split())
        if not q:
            return []
        people = [{"user": u, "name": e.get("name", ""), "email": e.get("email", "")} for u, e in self.entries.items()]
        tiers = (lambda p: p["email"] == q,
                 lambda p: p["name"].lower() == q,
                 lambda p: p["name"].lower().split()[:1] == [q] or p["email"].split("@")[0] == q,
                 lambda p: q in p["name"].lower())
        for test in tiers:
            found = [p for p in people if test(p)]
            if found:
                return found
        return []

    def known_emails(self):
        return {e["email"] for e in self.entries.values() if e.get("email")}
