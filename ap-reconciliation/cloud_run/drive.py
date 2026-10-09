"""Google Drive access to the AP mailbox's Invoice folder.

The folder lives in ap@superhairpieces.com's My Drive, so the service reads it
as that user: a user OAuth refresh token (Secret Manager
`google-workspace-ap-*`, same mechanism as the Business Profile scripts). When
those three values are not configured it falls back to Application Default
Credentials, which works if the folder is shared with the runtime service
account instead.

Only the standard Drive v3 REST endpoints are used, through `requests`.
"""

import json
import threading
import time

import requests

FILES = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"
FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
FILE_FIELDS = "id,name,mimeType,modifiedTime,size,webViewLink,shortcutDetails"
EXPORT_AS_PDF = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.drawing",
}


class DriveError(RuntimeError):
    pass


class Drive:
    def __init__(self, client_id="", client_secret="", refresh_token="", scopes=None):
        self.user_oauth = bool(client_id and client_secret and refresh_token)
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.scopes = scopes or ["https://www.googleapis.com/auth/drive"]
        self._token, self._expiry = None, 0.0
        self._creds = None
        self._lock = threading.Lock()

    @property
    def identity(self):
        return "ap user OAuth" if self.user_oauth else "application default credentials"

    # -- auth ---------------------------------------------------------------
    def token(self, force=False):
        with self._lock:
            if self.user_oauth:
                if self._token and not force and time.time() < self._expiry:
                    return self._token
                r = requests.post(TOKEN_URL, data={
                    "grant_type": "refresh_token", "refresh_token": self.refresh_token,
                    "client_id": self.client_id, "client_secret": self.client_secret}, timeout=20)
                if r.status_code != 200:
                    raise DriveError(f"AP token refresh failed: {r.status_code} {r.text[:200]}")
                data = r.json()
                self._token = data["access_token"]
                self._expiry = time.time() + int(data.get("expires_in", 3600)) - 60
                return self._token
            import google.auth
            import google.auth.transport.requests
            if self._creds is None:
                self._creds, _ = google.auth.default(scopes=self.scopes)
            if force or not self._creds.valid:
                self._creds.refresh(google.auth.transport.requests.Request())
            return self._creds.token

    def _request(self, method, url, params=None, timeout=120, **kwargs):
        headers = {"Authorization": f"Bearer {self.token()}"}
        headers.update(kwargs.pop("headers", {}))
        r = requests.request(method, url, params=params, headers=headers, timeout=timeout, **kwargs)
        if r.status_code == 401:
            headers["Authorization"] = f"Bearer {self.token(force=True)}"
            r = requests.request(method, url, params=params, headers=headers, timeout=timeout, **kwargs)
        if r.status_code >= 400:
            raise DriveError(f"Drive {method} {url.split('/v3/')[-1][:60]} -> {r.status_code} {r.text[:300]}")
        return r

    # -- reading ------------------------------------------------------------
    def get(self, file_id):
        return self._request("GET", f"{FILES}/{file_id}", {"fields": FILE_FIELDS, "supportsAllDrives": "true"}).json()

    def list_children(self, folder_id):
        """Every non-trashed child of a folder, shortcuts resolved to their targets."""
        out, params = [], {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "pageSize": 1000, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
            "orderBy": "name",
        }
        while True:
            body = self._request("GET", FILES, params).json()
            for f in body.get("files", []):
                if f.get("mimeType") == SHORTCUT:
                    target = (f.get("shortcutDetails") or {}).get("targetId")
                    if not target:
                        continue
                    try:
                        resolved = self.get(target)
                    except DriveError:
                        continue
                    resolved["name"] = f.get("name") or resolved.get("name")
                    f = resolved
                out.append(f)
            if not body.get("nextPageToken"):
                return out
            params["pageToken"] = body["nextPageToken"]

    def download(self, file):
        """(bytes, mime type). Google Docs/Sheets/Slides are exported as PDF."""
        mime = file.get("mimeType", "")
        if mime in EXPORT_AS_PDF:
            r = self._request("GET", f"{FILES}/{file['id']}/export", {"mimeType": "application/pdf"}, timeout=300)
            return r.content, "application/pdf"
        r = self._request("GET", f"{FILES}/{file['id']}", {"alt": "media", "supportsAllDrives": "true"}, timeout=300)
        return r.content, mime

    # -- writing (optional: keep a copy of each uploaded statement) ----------
    def find_child_folder(self, parent_id, name):
        safe = name.replace("'", "\\'")
        params = {"q": f"'{parent_id}' in parents and name = '{safe}' and mimeType = '{FOLDER}' and trashed = false",
                  "fields": "files(id,name)", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        files = self._request("GET", FILES, params).json().get("files", [])
        return files[0]["id"] if files else None

    def ensure_folder(self, parent_id, name):
        found = self.find_child_folder(parent_id, name)
        if found:
            return found
        body = {"name": name, "mimeType": FOLDER, "parents": [parent_id]}
        r = self._request("POST", FILES, {"supportsAllDrives": "true", "fields": "id"}, json=body)
        return r.json()["id"]

    def upload(self, parent_id, name, data, mime_type):
        meta = json.dumps({"name": name, "parents": [parent_id]})
        files = {
            "metadata": ("metadata", meta, "application/json; charset=UTF-8"),
            "file": (name, data, mime_type or "application/octet-stream"),
        }
        r = self._request("POST", UPLOAD, {"uploadType": "multipart", "supportsAllDrives": "true",
                                           "fields": "id,name,webViewLink"}, files=files, timeout=300)
        return r.json()
