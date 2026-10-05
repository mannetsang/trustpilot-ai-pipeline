"""Gmail and Drive access as ap@superhairpieces.com.

Domain-wide delegation is not available, so no service account can act as a
Workspace user. Instead ap@ authorised an OAuth client in shp-ai-bot-2026 once
(OAuth Playground, offline access), and the resulting refresh token lets any
caller that can read the three secrets below act as ap@. The client id,
secret and refresh token travel together - a refresh token can only be
exchanged by the client that issued it.

| Secret Manager id                    | Env var (local .env)              |
|--------------------------------------|-----------------------------------|
| google-workspace-ap-client-id        | GOOGLE_WORKSPACE_AP_CLIENT_ID     |
| google-workspace-ap-client-secret    | GOOGLE_WORKSPACE_AP_CLIENT_SECRET |
| google-workspace-ap-refresh-token    | GOOGLE_WORKSPACE_AP_REFRESH_TOKEN |

Scopes granted: gmail.modify (read, label, draft, send; no permanent delete)
and drive. If ap@'s password changes or the app's access is removed, calls
fail with invalid_grant until a new refresh token is minted and added as a new
version of google-workspace-ap-refresh-token.

Only the standard library is used. Run this file directly to check the token:
    python lib/google_workspace.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.secrets import get_secret  # noqa: E402

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
DRIVE_API = "https://www.googleapis.com/drive/v3"
MAILBOX = "ap@superhairpieces.com"


class WorkspaceError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {json.dumps(body)[:800]}")
        self.status = status
        self.body = body


class WorkspaceClient:
    def __init__(self):
        self._client_id = get_secret("google-workspace-ap-client-id",
                                     env_var="GOOGLE_WORKSPACE_AP_CLIENT_ID")
        self._client_secret = get_secret("google-workspace-ap-client-secret",
                                         env_var="GOOGLE_WORKSPACE_AP_CLIENT_SECRET")
        self._refresh_token = get_secret("google-workspace-ap-refresh-token",
                                         env_var="GOOGLE_WORKSPACE_AP_REFRESH_TOKEN")
        self._access_token = None
        self._expires_at = 0.0

    # -- auth -------------------------------------------------------------
    def access_token(self):
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }).encode()
        request = urllib.request.Request(
            TOKEN_URL, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        status, body = _send(request)
        if status != 200:
            raise WorkspaceError(status, body)
        self._access_token = body["access_token"]
        self._expires_at = time.time() + float(body.get("expires_in", 3600))
        return self._access_token

    # -- HTTP -------------------------------------------------------------
    def request(self, method, url, params=None, payload=None):
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        headers = {"Authorization": f"Bearer {self.access_token()}"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        status, body = _send(urllib.request.Request(url, data=data, headers=headers, method=method))
        if status == 401:  # token revoked mid-run: mint a new one and retry once
            self._access_token = None
            headers["Authorization"] = f"Bearer {self.access_token()}"
            status, body = _send(urllib.request.Request(url, data=data, headers=headers, method=method))
        if status >= 400:
            raise WorkspaceError(status, body)
        return body

    def get(self, url, params=None):
        return self.request("GET", url, params=params)

    def paged(self, url, key, params=None, limit=None):
        """Yield items of a paged list endpoint, following nextPageToken."""
        params = dict(params or {})
        count = 0
        while True:
            body = self.get(url, params)
            for item in body.get(key, []):
                yield item
                count += 1
                if limit and count >= limit:
                    return
            token = body.get("nextPageToken")
            if not token:
                return
            params["pageToken"] = token

    # -- Gmail ------------------------------------------------------------
    def gmail_profile(self):
        return self.get(f"{GMAIL_API}/profile")

    def search_messages(self, query, limit=50):
        """Message stubs ({id, threadId}) matching a Gmail search query."""
        return list(self.paged(f"{GMAIL_API}/messages", "messages",
                               {"q": query, "maxResults": min(limit, 500)}, limit))

    def get_message(self, message_id, fmt="full"):
        return self.get(f"{GMAIL_API}/messages/{message_id}", {"format": fmt})

    def get_attachment(self, message_id, attachment_id):
        return self.get(f"{GMAIL_API}/messages/{message_id}/attachments/{attachment_id}")

    def list_labels(self):
        return self.get(f"{GMAIL_API}/labels").get("labels", [])

    def modify_labels(self, message_id, add=(), remove=()):
        return self.request("POST", f"{GMAIL_API}/messages/{message_id}/modify",
                            payload={"addLabelIds": list(add), "removeLabelIds": list(remove)})

    # -- Drive ------------------------------------------------------------
    def drive_about(self):
        return self.get(f"{DRIVE_API}/about", {"fields": "user,storageQuota"})

    def list_files(self, query=None, fields="id,name,mimeType,modifiedTime,parents", limit=100):
        params = {"pageSize": min(limit, 1000), "fields": f"nextPageToken,files({fields})",
                  "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if query:
            params["q"] = query
        return list(self.paged(f"{DRIVE_API}/files", "files", params, limit))

    def get_file(self, file_id, fields="id,name,mimeType,modifiedTime,parents,webViewLink"):
        return self.get(f"{DRIVE_API}/files/{file_id}",
                        {"fields": fields, "supportsAllDrives": "true"})


def _send(request):
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, {"raw": raw.decode("utf-8", "replace")}


if __name__ == "__main__":
    client = WorkspaceClient()
    profile = client.gmail_profile()
    print(f"Gmail: {profile['emailAddress']} ({profile['messagesTotal']} messages)")
    if profile["emailAddress"].lower() != MAILBOX:
        sys.exit(f"WARNING: token belongs to {profile['emailAddress']}, not {MAILBOX}")
    about = client.drive_about()
    print(f"Drive: {about['user']['emailAddress']}")
    for f in client.list_files(limit=5, fields="name,mimeType"):
        print(f"  - {f['name']} ({f['mimeType']})")
