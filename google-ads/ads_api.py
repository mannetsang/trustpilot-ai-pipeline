"""Thin REST client for the Google Ads API, shared by the scripts in this folder.

Authentication is Application Default Credentials in every environment, the
same as google-merchant/:

- GitHub Actions: the GCP_SA_KEY service account (google-github-actions/auth).
- Claude cloud sessions: GOOGLE_APPLICATION_CREDENTIALS written by the hook.
- Your machine: `gcloud auth application-default login` with the adwords scope
  (see README.md; the OAuth client then belongs to gcloud, so Google may refuse
  it under the project-based access model).

Whichever identity that resolves to must be a user on the Google Ads account,
or on a manager account above it, with at least Read only access. Since
2026-09-09 Google grants API access to the Cloud project that issued the OAuth
credentials, so there is no developer token, no OAuth client secret, and
nothing for Secret Manager to hold. Customer ids are not secrets.

Requests carry `login-customer-id` when the caller reaches a client account
through a manager account, which is how every GAQL request in this repo runs.
"""

import json
import os
import sys
import time

import google.auth
from google.auth.transport.requests import AuthorizedSession

# Keep imports for lib/ working whether we're run from the repo root or here.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.secrets import load_dotenv  # noqa: E402

API_VERSION = "v25"
ENDPOINT = "https://googleads.googleapis.com"
SCOPES = ["https://www.googleapis.com/auth/adwords"]
GCP_PROJECT = "shp-ai-bot-2026"

# The "Super Hair Pieces" manager account. Client accounts are linked under
# it; the service account is a Standard user on it, so one login covers them.
DEFAULT_MANAGER_ID = "4233688880"

# Money comes back in micros. These metrics are documented as micros without
# carrying the suffix in their name.
_MICRO_METRICS = {
    "metrics.averageCpc", "metrics.averageCpm", "metrics.averageCpv", "metrics.averageCpe",
    "metrics.costPerConversion", "metrics.costPerAllConversions", "metrics.costPerCurrentModelAttributedConversion",
    "metrics.averageCost", "metrics.activeViewCpm",
}
_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRY_CODES = {"RESOURCE_EXHAUSTED", "INTERNAL", "UNAVAILABLE", "DEADLINE_EXCEEDED", "TRANSIENT_ERROR"}


class AdsApiError(RuntimeError):
    """A Google Ads API failure with the pieces worth showing a human."""

    def __init__(self, http_status, code, message, request_id=None, field=None):
        super().__init__(f"{code}: {message}" + (f" (field {field})" if field else ""))
        self.http_status = http_status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.field = field


def _parse_error(http_status, body):
    """Reduce an error body to (code, message, request_id, field)."""
    err = None
    if isinstance(body, list):
        body = next((b for b in body if isinstance(b, dict) and "error" in b), body[0] if body else {})
    if isinstance(body, dict):
        err = body.get("error")
    if not isinstance(err, dict):
        return f"HTTP_{http_status}", str(body)[:500], None, None
    code, message, request_id, field = err.get("status", f"HTTP_{http_status}"), err.get("message", ""), None, None
    for detail in err.get("details", []) or []:
        request_id = detail.get("requestId") or request_id
        for e in detail.get("errors", []) or []:
            ec = e.get("errorCode") or {}
            if ec:
                kind, value = next(iter(ec.items()))
                code = f"{kind}.{value}"
            message = e.get("message") or message
            path = (e.get("location") or {}).get("fieldPathElements") or []
            if path:
                field = ".".join(p.get("fieldName", "") for p in path)
            break
    return code, message, request_id, field


def _number(value):
    """int64 fields arrive as strings; turn metric strings into numbers."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return int(value) if value.lstrip("-").isdigit() else float(value)
        except ValueError:
            return value
    return value


def flatten(row, prefix="", out=None):
    """{"campaign": {"name": "x"}, "metrics": {"clicks": "3"}} -> {"campaign.name": "x", "metrics.clicks": 3}.

    Metric values become numbers; *Micros fields become currency units under
    the key without the suffix (campaignBudget.amountMicros -> campaignBudget.amount).
    Repeated fields (lists) are kept as they are; resourceName fields are dropped.
    """
    if out is None:
        out = {}
    for key, value in row.items():
        path = f"{prefix}.{key}" if prefix else key
        if key == "resourceName":
            continue  # pure noise in a report row; ids and names carry the identity
        if isinstance(value, dict):
            flatten(value, path, out)
        elif path.endswith("Micros"):
            out[path[: -len("Micros")]] = _number(value) / 1e6 if value is not None else None
        elif path in _MICRO_METRICS:
            out[path] = _number(value) / 1e6 if value is not None else None
        elif path.startswith("metrics."):
            out[path] = _number(value)
        else:
            out[path] = value
    return out


class GoogleAds:
    """Minimal GAQL client over REST (searchStream), with retries."""

    def __init__(self, login_customer_id=None, version=API_VERSION, session=None):
        load_dotenv()
        self.credentials, _ = google.auth.default(scopes=SCOPES)
        self.session = session or AuthorizedSession(self.credentials)
        self.login_customer_id = str(login_customer_id).replace("-", "") if login_customer_id else None
        self.version = version
        self.calls = 0

    # -- identity -----------------------------------------------------------------
    def identity(self):
        return getattr(self.credentials, "service_account_email", None) or "the current Application Default Credentials identity"

    # -- requests -----------------------------------------------------------------
    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.login_customer_id:
            headers["login-customer-id"] = self.login_customer_id
        return headers

    def _request(self, method, path, payload=None, attempts=5, timeout=180):
        url = f"{ENDPOINT}/{self.version}/{path}"
        for attempt in range(attempts):
            self.calls += 1
            resp = self.session.request(method, url, headers=self._headers(), data=json.dumps(payload) if payload is not None else None, timeout=timeout)
            try:
                body = resp.json()
            except ValueError:
                body = {"error": {"status": f"HTTP_{resp.status_code}", "message": resp.text[:500]}}
            if resp.status_code < 400:
                return body
            code, message, request_id, field = _parse_error(resp.status_code, body)
            transient = resp.status_code in _RETRY_STATUS or any(c in code for c in _RETRY_CODES)
            if transient and attempt < attempts - 1:
                wait = min(60, 5 * 2 ** attempt)
                print(f"  {code}: retrying in {wait}s", file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            raise AdsApiError(resp.status_code, code, message, request_id, field)
        raise AdsApiError(0, "RETRIES_EXHAUSTED", url)

    def list_accessible_customers(self):
        """Customer ids the credential is a direct user on (managers included)."""
        body = self._request("GET", "customers:listAccessibleCustomers")
        return [name.split("/")[-1] for name in body.get("resourceNames", [])]

    def search_stream(self, customer_id, query):
        """Run a GAQL query; return the raw result rows (nested dicts)."""
        body = self._request("POST", f"customers/{str(customer_id).replace('-', '')}/googleAds:searchStream", {"query": query})
        rows = []
        for chunk in body if isinstance(body, list) else [body]:
            rows.extend(chunk.get("results", []))
        return rows

    def search(self, customer_id, query):
        """Run a GAQL query; return flattened rows (see flatten())."""
        return [flatten(r) for r in self.search_stream(customer_id, query)]


def explain_api_error(exc, identity="the calling identity"):
    """Turn the common setup failures into the next step."""
    text = str(exc)
    if "NOT_ADS_USER" in text:
        return (f"{identity} is not a user on any Google Ads account. In Google Ads go to Admin -> Access and security, "
                "add that email under Users (add its domain under Security -> Allowed domains first).")
    if "USER_PERMISSION_DENIED" in text:
        return (f"{identity} has no access to this customer. Add it as a user on the account or on the manager account above it, "
                "and pass that manager id as --login-customer-id.")
    if "DEVELOPER_TOKEN" in text or "NOT_ADS_USER" in text:
        return text + " (Since 2026-09-09 access is granted to the Cloud project; check the Google Ads API page of the project in the Cloud console.)"
    return text
