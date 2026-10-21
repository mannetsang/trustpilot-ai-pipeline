"""
Thin client for the Bing Webmaster Tools JSON API.

Endpoint shape: https://ssl.bing.com/webmaster/api.svc/json/<Method>?apikey=KEY
Read methods are GET with query parameters; write methods are POST with a JSON
body whose keys are the .NET parameter names (siteUrl, feedUrl, urlList).
Successful responses wrap their payload in a top-level "d".

Why this and not IndexNow for the instant pings: IndexNow needs a key file at
the web root, and BigCommerce-hosted storefronts cannot serve one (see
indexnow.py and the README). SubmitUrlBatch needs no key file - only a verified
site - so it is the transport that actually works for these stores today.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://ssl.bing.com/webmaster/api.svc/json"
TIMEOUT = 60


class BingApiError(RuntimeError):
    def __init__(self, method, status, body):
        self.method = method
        self.status = status
        self.body = body
        super().__init__(f"{method} -> HTTP {status}: {body[:400]}")


class BingWebmaster:
    def __init__(self, api_key: str, timeout: int = TIMEOUT):
        self.api_key = api_key
        self.timeout = timeout

    # --- plumbing -------------------------------------------------------
    def _call(self, method: str, params: dict | None = None, body: dict | None = None):
        query = {"apikey": self.api_key, **(params or {})}
        url = f"{BASE}/{method}?" + urllib.parse.urlencode(query)

        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            raise BingApiError(method, exc.code, _redact(raw, self.api_key)) from None
        except urllib.error.URLError as exc:
            raise BingApiError(method, 0, str(exc.reason)) from None

        if not raw.strip():
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise BingApiError(method, status, _redact(raw, self.api_key)) from None
        return parsed.get("d", parsed) if isinstance(parsed, dict) else parsed

    # --- reads ----------------------------------------------------------
    def get_user_sites(self) -> list[dict]:
        """Every site on the account, with its verification state."""
        return self._call("GetUserSites") or []

    def get_feeds(self, site_url: str) -> list[dict]:
        """Sitemaps already submitted for a site."""
        return self._call("GetFeeds", params={"siteUrl": site_url}) or []

    def get_url_submission_quota(self, site_url: str) -> dict:
        return self._call("GetUrlSubmissionQuota", params={"siteUrl": site_url}) or {}

    # --- writes ---------------------------------------------------------
    def submit_feed(self, site_url: str, feed_url: str):
        """Register a sitemap. Idempotent: re-submitting an existing feed is a no-op."""
        return self._call("SubmitFeed", body={"siteUrl": site_url, "feedUrl": feed_url})

    def submit_url_batch(self, site_url: str, urls: list[str]):
        """Push URLs for immediate crawl. Counts against the daily quota."""
        return self._call("SubmitUrlBatch", body={"siteUrl": site_url, "urlList": urls})


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "<apikey>") if secret else text
