"""
A small REST client for the Google Search Console API.

Endpoints, exactly as the companion Postman collection uses them:

    GET  /webmasters/v3/sites
    GET  /webmasters/v3/sites/{siteUrl}
    GET  /webmasters/v3/sites/{siteUrl}/sitemaps
    POST /webmasters/v3/sites/{siteUrl}/searchAnalytics/query
    POST /v1/urlInspection/index:inspect

`siteUrl` sits in the *path*, so it is percent-encoded with nothing left safe:
`https://superhairpieces.ca/` becomes `https%3A%2F%2Fsuperhairpieces.ca%2F`. An
un-encoded value is the usual cause of a 404 here.

Auth, in the order tried by connect():

1. `GOOGLE_SEARCH_CONSOLE_ACCESS_TOKEN` - an hour-long token pasted in from the
   OAuth Playground. Fastest way to see real data without storing anything.
2. A user OAuth refresh token (see config.oauth_triple). The granted scope is
   checked at mint time, because the one failure mode worth designing against is
   a token that mints fine and 403s on every call after it.
3. Application Default Credentials - the claude-sessions service account, which
   is how everything else in this repo reaches Google. It needs adding to each
   property under Settings -> Users and permissions; until then sites.list
   answers `{}` and every property reads as "no access" rather than failing.

Only reads. The scope requested is webmasters.readonly, so a bug here cannot
submit or delete a sitemap.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from config import SCOPE_READONLY, oauth_triple

BASE = "https://searchconsole.googleapis.com"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TIMEOUT = 60
RETRY_STATUS = {429, 500, 502, 503, 504}


class SearchConsoleError(RuntimeError):
    pass


class AccessDenied(SearchConsoleError):
    """The credential is valid but may not see this property."""


def encode_site(site_url: str) -> str:
    """Percent-encode a property for the URL path: no character stays safe."""
    return urllib.parse.quote(site_url, safe="")


def _request(method, url, token, body=None, attempts=5):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            if exc.code in RETRY_STATUS and attempt < attempts - 1:
                time.sleep(min(2 ** attempt, 30) + 0.1 * attempt)
                continue
            if exc.code in (401, 403):
                raise AccessDenied(f"HTTP {exc.code} on {method} {url.split('?')[0]}: {detail}") from None
            raise SearchConsoleError(f"HTTP {exc.code} on {method} {url.split('?')[0]}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
                continue
            raise SearchConsoleError(f"{method} {url.split('?')[0]} unreachable: {exc}") from None
    raise SearchConsoleError(f"{method} {url} kept failing")


def mint_access_token(client_id, client_secret, refresh_token):
    """Exchange a refresh token, and refuse one that was granted the wrong scope.

    The Business Profile refresh token is the live example: it returns 200 here
    with a usable-looking token, then 403s on every Search Console call. Reading
    the `scope` the token endpoint echoes back turns that into one clear error.
    """
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise AccessDenied(f"refresh token rejected (HTTP {exc.code}): {detail}") from None
    granted = payload.get("scope", "")
    if "webmasters" not in granted:
        raise AccessDenied(
            "this refresh token carries no webmasters scope - it was granted "
            f"'{granted or '(none reported)'}'. Search Console needs its own token minted "
            "with https://www.googleapis.com/auth/webmasters.readonly; the Business "
            "Profile token (business.manage) mints fine here and then 403s on every call."
        )
    return payload["access_token"]


def adc_access_token():
    """An access token for the service account, scoped to Search Console."""
    import google.auth
    import google.auth.transport.requests

    creds, _ = google.auth.default(scopes=[SCOPE_READONLY])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


class SearchConsole:
    """Read-only client. `how` records which credential route it is using."""

    def __init__(self, token, how):
        self.token = token
        self.how = how

    @classmethod
    def connect(cls, access_token=None):
        token = access_token or os.environ.get("GOOGLE_SEARCH_CONSOLE_ACCESS_TOKEN")
        if token:
            return cls(token.strip(), "pasted access token")
        triple = oauth_triple()
        if triple:
            return cls(mint_access_token(*triple), "user OAuth refresh token")
        return cls(adc_access_token(), "service account (ADC)")

    # ---------------------------------------------------------------- sites
    def sites(self):
        """Every property this credential can see. `{}` when it can see none."""
        return _request("GET", f"{BASE}/webmasters/v3/sites", self.token).get("siteEntry", [])

    def site(self, site_url):
        return _request("GET", f"{BASE}/webmasters/v3/sites/{encode_site(site_url)}", self.token)

    # ------------------------------------------------------------- sitemaps
    def sitemaps(self, site_url, sitemap_index=None):
        url = f"{BASE}/webmasters/v3/sites/{encode_site(site_url)}/sitemaps"
        if sitemap_index:
            url += "?" + urllib.parse.urlencode({"sitemapIndex": sitemap_index})
        return _request("GET", url, self.token).get("sitemap", [])

    # ------------------------------------------------------ search analytics
    def query(self, site_url, start_date, end_date, dimensions=None, search_type=None,
              data_state=None, aggregation_type=None, filters=None, row_limit=25000,
              max_rows=None, with_metadata=False):
        """One searchAnalytics.query, paged until the rows run out.

        The API returns at most 25,000 rows per call and pages with startRow, so
        a dimension with more values than that needs several calls. A short page
        means the end: there is no next-page token to follow. rowLimit is always
        sent, because leaving it out caps the response at 1,000 rows silently.

        with_metadata returns (rows, metadata); metadata carries
        first_incomplete_date, the day from which figures are still provisional.
        """
        url = f"{BASE}/webmasters/v3/sites/{encode_site(site_url)}/searchAnalytics/query"
        body = {"startDate": start_date, "endDate": end_date, "rowLimit": min(row_limit, 25000)}
        if dimensions:
            body["dimensions"] = list(dimensions)
        if search_type:
            body["type"] = search_type
        if data_state:
            body["dataState"] = data_state
        if aggregation_type:
            body["aggregationType"] = aggregation_type
        if filters:
            body["dimensionFilterGroups"] = filters

        rows, start_row, meta = [], 0, {}
        while True:
            body["startRow"] = start_row
            payload = _request("POST", url, self.token, body)
            meta = payload.get("metadata", meta)
            page = payload.get("rows", [])   # a past-the-end page omits rows entirely
            rows.extend(page)
            if len(page) < body["rowLimit"] or (max_rows and len(rows) >= max_rows):
                break
            start_row += len(page)
        rows = rows[:max_rows] if max_rows else rows
        return (rows, meta) if with_metadata else rows

    # ------------------------------------------------------- url inspection
    def inspect(self, site_url, inspection_url, language_code="en-US"):
        """Index status for one URL. Quota is per property and small - 2,000 a day."""
        return _request("POST", f"{BASE}/v1/urlInspection/index:inspect", self.token, {
            "inspectionUrl": inspection_url,
            "siteUrl": site_url,
            "languageCode": language_code,
        }).get("inspectionResult", {})
