"""Shared client for the Google Business Profile scripts in this folder.

Authentication is a user OAuth refresh token, not Application Default
Credentials: the Business Profile APIs act as a Google *user* who manages the
listings, and no service account is a manager on them. The refresh token was
minted by a developer-owned OAuth client (not one in shp-ai-bot-2026), so all
three values below travel together - a refresh token can only be exchanged by
the client that issued it.

Credentials resolve through lib/secrets.py: the environment variable first (a
gitignored .env locally), then Secret Manager on shp-ai-bot-2026.

| Secret Manager id                      | Env var                               |
|----------------------------------------|---------------------------------------|
| google-business-profile-client-id      | GOOGLE_BUSINESS_PROFILE_CLIENT_ID     |
| google-business-profile-client-secret  | GOOGLE_BUSINESS_PROFILE_CLIENT_SECRET |
| google-business-profile-refresh-token  | GOOGLE_BUSINESS_PROFILE_REFRESH_TOKEN |

Access tokens last one hour and are minted on demand; nothing stores them.
Only the standard library is used, so no venv is needed for a local run.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Keep imports for lib/ working whether we're run from the repo root or here.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.secrets import get_secret  # noqa: E402

TOKEN_URL = "https://oauth2.googleapis.com/token"
ACCOUNTS_API = "https://mybusinessaccountmanagement.googleapis.com/v1"
BUSINESS_INFO_API = "https://mybusinessbusinessinformation.googleapis.com/v1"
SCOPE = "https://www.googleapis.com/auth/business.manage"

# The one account the refresh token can see (a personal account, not an
# organisation). Not a secret; listed here so scripts need no lookup.
DEFAULT_ACCOUNT = "accounts/111445610944292236883"

LOCATION_READ_MASK = ",".join([
    "name", "title", "storefrontAddress", "phoneNumbers", "websiteUri",
    "metadata", "openInfo", "categories", "regularHours",
])


class GbpError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {json.dumps(body)[:800]}")
        self.status = status
        self.body = body


class GbpClient:
    def __init__(self):
        self._client_id = get_secret("google-business-profile-client-id",
                                     env_var="GOOGLE_BUSINESS_PROFILE_CLIENT_ID")
        self._client_secret = get_secret("google-business-profile-client-secret",
                                         env_var="GOOGLE_BUSINESS_PROFILE_CLIENT_SECRET")
        self._refresh_token = get_secret("google-business-profile-refresh-token",
                                         env_var="GOOGLE_BUSINESS_PROFILE_REFRESH_TOKEN")
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
            raise GbpError(status, body)
        self._access_token = body["access_token"]
        self._expires_at = time.time() + float(body.get("expires_in", 3600))
        return self._access_token

    # -- HTTP -------------------------------------------------------------
    def request(self, method, url, params=None, payload=None):
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
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
            raise GbpError(status, body)
        return body

    def get(self, url, params=None):
        return self.request("GET", url, params=params)

    def paged(self, url, key, params=None):
        """Yield items of a paged list endpoint, following nextPageToken."""
        params = dict(params or {})
        while True:
            body = self.get(url, params)
            yield from body.get(key, [])
            token = body.get("nextPageToken")
            if not token:
                return
            params["pageToken"] = token

    # -- Business Profile -------------------------------------------------
    def list_accounts(self):
        return list(self.paged(f"{ACCOUNTS_API}/accounts", "accounts"))

    def list_locations(self, account=DEFAULT_ACCOUNT, read_mask=LOCATION_READ_MASK):
        return list(self.paged(f"{BUSINESS_INFO_API}/{account}/locations", "locations",
                               {"readMask": read_mask, "pageSize": 100}))

    def get_location(self, location, read_mask=LOCATION_READ_MASK):
        """location: 'locations/<id>'."""
        return self.get(f"{BUSINESS_INFO_API}/{location}", {"readMask": read_mask})


def _send(request, timeout=60):
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def format_address(location):
    address = location.get("storefrontAddress") or {}
    parts = list(address.get("addressLines", []))
    parts += [address.get("locality"), address.get("administrativeArea"),
              address.get("postalCode"), address.get("regionCode")]
    return ", ".join(p for p in parts if p)
